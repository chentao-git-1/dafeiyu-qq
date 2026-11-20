"""Tie a private FishWorld route decision to the trip that the world actually reports."""
import copy
from datetime import datetime
import hashlib
import json
import re
import time

import aiohttp
from astrbot.api import logger

from .world_observation import binding_scope


ROUTE = "dafeiyu_private_route"
VIEW_URL = "https://api.yuyuworld.app/v1/state"
MODES = {"walk", "bus", "subway", "taxi", "bike", "ebike"}
TRAVEL = re.compile(r"回去|回来|回家|回楼下|回到楼下|返回|返程|往回|回头走|往家走|折回|折返|掉头|调头|改道|改去|推车|把车推|车推着|推着车|去车行")
BIKE = re.compile(r"自行车|单车|推车|把车推|车推|后胎|瘪胎|取车")
REPORT = re.compile(r"[?？]|了吗|了没|有没有|是不是|咋样|怎么|到哪|在哪|为什么|能不能|能否|要不要|打算|我想知道")
NO_ORDER = re.compile(r"不(?:要|用|必|是)|别|先不|不许|假如|如果|假设|比如|举例|你说过|你刚才说|我说过|告诉她|转告|发给|帮我写")
DIRECT = re.compile(r"^(?:(?:那就|那你|那|嗯+|好[的啊]?|我让你|你|听澜|请你?|先|现在|赶紧|赶快|快点?|麻烦你|还是|直接|就)[，,、！!\s]*)*"
                    r"(?:回去|回来|回家|回楼下|返回|返程|折回|折返|掉头|调头|改道|改去|往回|回头走|往家走|把车推|去推车|去把车推|推车去|推着车去|去吧车推着)")


def route_mode(human):
    if not isinstance(human, str) or len(human) > 180 or not TRAVEL.search(human):
        return ""
    if re.search(r"返回.{0,7}(?:页面|上页|上一页|结果|参数|数据|列表|文本|代码|字符串|json|主页|菜单|文件)",human,re.I):
        return ""
    if any(c in human for c in '“”「」『』"\x60') or re.search(r"(?:我|她|他)(?:自己|现在|刚刚|刚才|准备|先|要)?(?:回去|回来|返回|回家|推车)", human):
        return ""
    if re.search(r"假如|如果|假设|比如|举例|转告|发给|帮我写|(?:他|她|你|我)(?:刚才)?说[，,:：]",human):
        return "status"
    parts = [s.strip() for s in re.split(r"[，,；;。]",human) if TRAVEL.search(s)]
    clause = parts[-1] if parts else human.strip()
    if NO_ORDER.search(clause) or REPORT.search(clause):
        return "status"
    return "decision" if DIRECT.match(clause) else "status"


def fish_tool(toolset, name):
    tool = toolset.get_tool(name) if toolset else None
    return tool if getattr(tool, "mcp_server_name", None) == "fish-world" and getattr(tool, "active", False) else None


def latest_role(messages):
    for msg in reversed(messages or []):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role in ("user", "assistant", "tool"):
            return role
    return None


def route_tools(kwargs):
    """On the first step of an explicit direction change, offer a real move or a read-only choice."""
    contexts = kwargs.get("contexts")
    if (not getattr(contexts, "first_route_choice", False) or latest_role(contexts) != "user"
            or kwargs.get("tool_calls_result")):
        return kwargs
    original = kwargs.get("func_tool")
    move = fish_tool(original, "world_go")
    view = fish_tool(original, "world_now")
    if not move or not view:
        return kwargs
    chosen = copy.copy(original)
    chosen.tools = [t for name in ("world_go", "world_now", "world_find", "world_guide")
                    if (t := fish_tool(original, name)) is not None]
    return dict(kwargs, func_tool=chosen, tool_choice="required")


def state_fields(data, now):
    if not isinstance(data, dict) or not isinstance(data.get("where"), dict):
        return None
    where = data["where"]
    try:
        observed = datetime.fromisoformat(data["time"])
        if observed.tzinfo is None or not -10 <= now - observed.timestamp() <= 100:
            return None
    except (ValueError, KeyError, TypeError, OverflowError):
        return None
    def label(obj, key):
        return str(obj.get(key) or "")[:100] if isinstance(obj, dict) else ""
    return {"time": data["time"], "kind": label(where, "kind"), "text": label(where, "text"),
            "to": label(where.get("to"), "id"), "to_name": label(where.get("to"), "name"),
            "at": label(where.get("at"), "id"), "home": where.get("home") is True,
            "arrive": label(where, "arrive"), "carry": label(where.get("carry"), "name")}


async def current_route(context, event, scope, *, details=False):
    if not scope or binding_scope(context, event) != scope:
        return None
    fish = context.get_registered_star("astrbot_plugin_fishworld").star_cls
    key = str(fish.config.get("watch_key") or "")
    session = getattr(fish, "session", None)
    if not key.startswith("fwr_") or session is None:
        return None
    try:
        async with session.get(VIEW_URL, headers={"Authorization": "Bearer " + key}, allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=3)) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)
        if binding_scope(context, event) == scope:
            state = state_fields(data, time.time())
            if state and details:
                outfit = data.get("outfit") or {}
                if isinstance(outfit,dict):
                    state["outfit"] = {k:str(outfit.get(k) or "")[:150] for k in ("id","name","look")}
                tasks = [data.get("doing"), *(data.get("side") or [])]
                state["activities"] = [{k:str(task.get(k) or "")[:100] for k in ("name","since","until")}
                                       for task in tasks if isinstance(task,dict) and task.get("name")]
            return state
    except Exception as error:
        logger.info("[world-route] state unavailable (%s)", type(error).__name__)
    return None


async def prepare_route(plugin, event, request, source):
    if event.get_extra("fishworld_wake_id") or event.get_extra("cron_job") or event.get_extra("cron_payload"):
        return False
    human = source["text"]
    previous = str(source.get("previous_text") or "")[:250]
    mode = route_mode(human)
    if not mode and re.fullmatch(r"(?:嗯+|好|好的|可以|行)?[，, ]*(?:去吧|去呀|出发吧|好)[。！!呀啊]*",human.strip()) and TRAVEL.search(previous):
        mode = "decision" if "去" in human or "出发" in human else "status"
    if not mode:
        return False
    scope = binding_scope(plugin.context, event)
    if not scope or not fish_tool(request.func_tool, "world_go"):
        return False
    check = event.get_extra(ROUTE)
    if check is None:
        bike_needed = bool(BIKE.search(human) or (TRAVEL.search(human) and BIKE.search(previous)))
        if bike_needed and fish_tool(request.func_tool, "world_now") and not event.get_extra("dafeiyu_prefetched_world_now"):
            event.set_extra("dafeiyu_current_world_detail", True)
            event.set_extra("dafeiyu_detail_is_bicycle", True)
            await plugin.prefetch_world_now(event, request)
        raw = event.get_extra("dafeiyu_prefetched_world_now") or ""
        bike = next((line for line in raw.splitlines() if line.startswith("自行车：")), "")
        view = await current_route(plugin.context, event, scope)
        if re.fullmatch(r"(?:你)?(?:请)?返回[呀啊吧啦。！!\s]*", human.strip()) and not (TRAVEL.search(previous) or BIKE.search(previous)):
            mode = "clarify"
        check = {"scope": scope, "mode": mode, "view": view,
                 "bike": bike, "attempt": None, "pickup": False,
                 "home_target": bool(re.search(r"回家|回楼下|回到家|回到楼下|返回家", human)
                                     or (bike_needed and re.search(r"停在(?:你|我)?家楼下", bike)
                                         and re.search(r"回去|折回|折返|返回|掉头|往回|去推车|车推着去|推着车去|把车推", human)))}
        if binding_scope(plugin.context, event) != scope:
            return False
        event.set_extra(ROUTE, check)
        if mode == "decision":
            plugin.review_stats["route_decision_requests"] += 1
    if check["scope"] != scope:
        return False
    already = check["home_target"] and at_goal(check["view"] or {},"home")
    request._dafeiyu_route_decision = check["mode"] == "decision" and not already
    packet = {"mode": check["mode"], "当前路线": check["view"], "本次看到的自行车位置": check["bike"],
              "返程目标": "home（回家取车，人和车的移动分别核对）" if check["home_target"] else "按本人的原话、实际来路及核实的地点判断，目的地不清时可问",
              "已经在目标路线或地点": bool(already)}
    if check["mode"] == "clarify":
        packet["待澄清"] = "这句没有明确要返回哪里；可先说现有去向，问清目的地。"
    request.system_prompt += ("\n<!-- BEGIN_DAFEIYU_ROUTE_TURN -->\n"
        + json.dumps(packet,ensure_ascii=False).replace("<","\\u003c") + "\n<!-- END_DAFEIYU_ROUTE_TURN -->")
    return True


def set_request_id(event, tool, args):
    check = event.get_extra(ROUTE)
    if (not check or getattr(tool,"mcp_server_name",None) != "fish-world" or tool.name != "world_go"
            or args.get("by") not in MODES or not args.get("place")):
        return
    mid = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
    if mid:
        op = [*check["scope"], mid, args["place"], args["by"], bool(args.get("alone")), bool(args.get("treat"))]
        args["request_id"] = "qq-route-" + hashlib.sha256(json.dumps(op,ensure_ascii=False).encode()).hexdigest()[:32]
    check["attempt"] = {"place":str(args["place"]), "by":args["by"], "outcome":"unconfirmed", "awaiting":True}


async def note_route(plugin,event,tool,args,result):
    check = event.get_extra(ROUTE)
    if not check or getattr(tool,"mcp_server_name",None) != "fish-world" or binding_scope(plugin.context,event) != check["scope"]:
        return
    if tool.name not in ("world_go","world_do","world_now"):
        return
    parts = result.get("content") if isinstance(result,dict) else getattr(result,"content",None)
    error = bool(result.get("isError")) if isinstance(result,dict) else bool(getattr(result,"isError",False))
    lines = []
    for part in parts if isinstance(parts,list) else ():
        kind = part.get("type") if isinstance(part,dict) else getattr(part,"type",None)
        value = part.get("text") if isinstance(part,dict) else getattr(part,"text",None)
        if kind == "text" and isinstance(value,str):
            lines.append(value)
    report = result if isinstance(result,str) else "\n".join(lines)
    if tool.name == "world_go" and args.get("by") in MODES and args.get("place"):
        denied = bool(error or re.search(r"不能改道|无法改道|暂时不能|改道失败|没有这个地点|不能出发",report))
        pending = bool(re.search(r"下一分钟出发|已安排.{0,25}出发|将在下一站下车",report))
        check["attempt"] = {"place":str(args["place"]), "by":args["by"],
                            "outcome":"denied" if denied else "pending" if pending else "unconfirmed"}
        # Even an HTTP-successful tool response can describe a refusal or a queued departure.
        check["view"] = await current_route(plugin.context, event, check["scope"])
        plugin.review_stats["route_changes_seen" if at_goal(check["view"] or {},str(args["place"])) else "route_changes_unconfirmed"] += 1
    if tool.name == "world_now" and not error:
        bike = next((line for line in report.splitlines() if line.startswith("自行车：")), "")
        if bike:
            check["bike"] = bike
            check["pickup"] = bool(re.search(r"正在推|正推着|你推着", bike))
    elif tool.name in ("world_go","world_do") and not error:
        if re.search(r"(?:^|\n)你(?:已经|已|正|正在)?(?:推着(?:自行车|单车)|把(?:自行车|单车)推(?:了出来|出来了))",report):
            check["pickup"] = True


RETURNING = re.compile(r"(?:折回去|折返|掉[过了]?头|调[过了]?头|改道|转回去)(?:了|啦|喽)|"
                      r"(?:已经|正在|正|在|刚刚|刚)(?:往|朝|向)(?:回|家|楼下|.{1,8}楼下).{0,5}(?:走|赶|去|返)|"
                      r"往回走了|(?:已经|正在|我在|我正)回(?:去|来|家)(?:了|的路上)|回程路上|正往回走|在回家的路上|"
                      r"(?:路线|行程|方向)(?:已经|刚)?(?:改好|换好|改回|调好)了|(?:已经|刚)掉头|掉头往回|回去了")
ARRIVED = re.compile(r"(?:已经|刚|我)?(?:到家|回到家|回到楼下|到(?:了)?(?:家|小区)?楼下)(?:了|啦|喽)|(?:已经|刚)到了")
NEAR = re.compile(r"快到了|马上就?到(?:了|家|楼下)|就要到(?:了|家|楼下)")
PICKED = re.compile(r"(?:已经|正|我在|刚把|车都|车已经|我已经把).{0,8}推(?:着|出来|出楼)|"
                    r"车推出来了?|推着(?:它|车|自行车|瘪胎)(?:在|走着|往)|车(?:在|已经在)我手里")
ACCEPT_NOW = re.compile(r"(?:这就|现在就|马上就?|立刻|这会儿就).{0,6}(?:折回|折返|回去|返回|回家|掉头|往回走)")
PROSPECTIVE = re.compile(r"(?:还没|尚未|无法|没能|不能|不想|不打算|等到|如果|假如|等我|等到了|到时候|到了再|准备|打算|刚才说|我说早了)")


def nearby(view, now):
    try:
        stamp = datetime.fromisoformat(view["arrive"])
        return stamp.tzinfo is not None and 0 <= stamp.timestamp() - now <= 300
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def at_goal(view, target):
    if not target or not view.get("kind"):
        return False
    if view["kind"] == "trip":
        return view.get("to") == target or (target == "home" and view.get("to_name") == "家")
    return view.get("at") == target or (target == "home" and bool(view.get("home")))


def status_phrase(view):
    desc = view.get("text")
    if not desc:
        return "我还确认不了现在的路线"
    if desc.startswith("在"):
        return "我现在" + desc
    if view.get("kind") == "trip":
        return "我现在还在" + desc
    return "我现在的位置是" + desc


async def refresh_if_due(plugin, event, check):
    view = check.get("view") or {}
    now = time.time()
    if now - check.get("last_delivery_read", 0) < 1:
        return
    attempt = check.get("attempt") or {}
    due = bool(attempt.get("awaiting"))
    if view:
        try:
            due = due or now - datetime.fromisoformat(view["time"]).timestamp() > 95
            if view.get("kind") == "trip" and view.get("arrive"):
                due = due or datetime.fromisoformat(view["arrive"]).timestamp() <= now
        except (TypeError,ValueError,KeyError):
            due = True
    if due:
        check["view"] = await current_route(plugin.context,event,check["scope"])
        check["last_delivery_read"] = now
        attempt["awaiting"] = False


def route_wording(text, check, now=None, *, final=True):
    if not check or not isinstance(text,str) or not text:
        return text
    now = time.time() if now is None else now
    view = check.get("view") or {}
    attempt = check.get("attempt") or {}
    target = "home" if check["home_target"] else attempt.get("place")
    matched = at_goal(view,target)
    heading, arrived = matched and view.get("kind") == "trip", matched and view.get("kind") != "trip"
    if attempt and not at_goal(view,attempt["place"]) and (final or re.search(r"改|走|路|回|推|到|掉头|出发|返",text)):
        outcome = attempt.get("outcome")
        report = ("没改成" if outcome == "denied" else "改道已经安排，还在等路线更新" if outcome == "pending"
                  else "我还没确认改道成功")
        return report + "，" + status_phrase(view) + "。" if view.get("text") else report + "。"
    parts = re.findall(r"[^，,。！？!?；;\n]+[，,。！？!?；;\n]*|[，,。！？!?；;\n]+", text)
    bad = set()
    for index, part in enumerate(parts):
        literal = re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"', "", part).strip()
        if PROSPECTIVE.search(literal):
            continue
        if ((RETURNING.search(literal) and not heading and not arrived)
                or (check["mode"] == "decision" and ACCEPT_NOW.search(literal) and not heading and not arrived)
                or (ARRIVED.search(literal) and not arrived)
                or (NEAR.search(literal) and not arrived and not (heading and nearby(view,now)))
                or (PICKED.search(literal) and not check.get("pickup"))):
            bad.add(index)
    if not bad:
        if (final and check["mode"] == "decision" and not matched and not attempt
                and re.fullmatch(r"(?:嗯+|好|行|知道了|收到|可以)(?:的|呀|吧|啊)?[。！!，,\s]*(?:这就走|这就去|马上走)?[。！!\s]*",text.strip())):
            return status_phrase(view) + "，还没确认改道。"
        return text
    status = status_phrase(view)
    if any(PICKED.search(parts[i]) for i in bad) and not check.get("pickup"):
        status += "，还没确认把车推出来"
    first = min(bad)
    return "".join((status+"。" if i==first else "") if i in bad else p for i,p in enumerate(parts)).rstrip("，,")
