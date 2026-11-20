"""Check a promised errand against the world clock, the requested transport and shop hours."""
import asyncio
from datetime import datetime,timedelta
import json
import re
import time

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from .route_receipts import current_route, fish_tool
from .world_activity import raw_text
from .world_observation import SHANGHAI, binding_scope


TIME=re.compile(r"今晚|今天|晚上|今儿|关门|来得及|赶得上|几点|最晚|吃完|饭后|待会|再拖|办完|去办|赶过去")
REPAIR=re.compile(r"补(?:个|后)?胎|修(?:后胎|车|车胎|自行车)|车行|推车|推着(?:车|去)")
PROMISE=re.compile(r"一定|肯定|保证|准能|来得及|赶得上|办完|能修好|都能办|不耽误|不拖了.{0,6}办|今晚就办")
RECONSIDER=re.compile(r"没法|不能|未必|不一定|不敢|赶不上|来不及|没把握|补不成|办不了|去不了|正好.{0,5}关门|不保证|说不准|别保证|已经关门")
FOOD_WAIT=re.compile(r"吃完|吃饭后|饭后|等.{0,8}饭|饭好|饭熟|先吃|先扒|吃过再|煮.{0,5}再")
SHOP=re.compile(r"([\u3400-\u9fff]{2,12}(?:服装店|修车铺|书店|餐厅|超市|商场|车行|菜市场))")
LINE=re.compile(r"^·\s*(.+?)\s+([\w-]+)（[^）]*）(.*)$")
CLOCK=r"(?:[01]\d|2[0-3]):[0-5]\d"
HOURS=re.compile(r"（("+CLOCK+r")\s*[–—~-]\s*("+CLOCK+r")）")
CLOSE=re.compile(r"(?:营业|开门|开)到\s*("+CLOCK+r"|24:00)")


def subject(text):
    if not isinstance(text,str) or not TIME.search(text):
        return None
    if re.search(r"买(?:了|过)(?:什么|多少|啥)",text) and not re.search(r"关门|最晚|赶得上|来得及",text):
        return None
    if REPAIR.search(text):
        return {"query":"补胎","name":"车行" if "铁马" not in text and "菜市场" not in text else ("铁马修车铺" if "铁马" in text else "菜市场"),
                "mode":"推着车走","after_meal":bool(FOOD_WAIT.search(text))}
    m=SHOP.search(text)
    if not m:
        return None
    name=re.split(r"去|在|到|趟",m[1])[-1]
    if len(name)<2 or len(name)>20:
        return None
    mode=next((v for k,v in (("推车","推着车走"),("公交","公交"),("地铁","地铁"),("打车","打车"),("骑车","骑车")) if k in text),"步行")
    return {"query":name,"name":name,"mode":mode,"after_meal":bool(FOOD_WAIT.search(text))}


def recent_errand(items,turn):
    eligible=sorted((i for i in items if i.source=="confirmed_exchange" and i.origin==turn.origin and i.owner==turn.actor
                     and i.visible_to(turn) and 0<=turn.observed_at-i.recorded_at<6*3600),key=lambda i:i.recorded_at,reverse=True)
    for item in eligible:
        try:
            pair=json.loads(item.text)
        except (TypeError,ValueError):
            continue
        h,r=pair.get("human",""),pair.get("reply","")
        if not isinstance(h,str) or not isinstance(r,str) or "鱼的世界后台" in h:
            continue
        if REPAIR.search(h) and re.search(r"不去了|不用去|不用修|改天|改明天|明天再",h):
            return None
        if TIME.search(h) and not re.search(r"明天再|改天|不办了|没答应",r) and (s:=subject(h+" "+r)):
            return s
    return None


async def read_world(f,name,args):
    if name not in ("world_find","world_history","world_now"):
        return ""
    tool=fish_tool(f.get("tools"),name)
    if not tool or binding_scope(f["context"],f["event"])!=f["scope"]:
        return ""
    try:
        result=await asyncio.wait_for(tool.call(ContextWrapper(context=None,tool_call_timeout=4),**args),4.5)
        if not getattr(result,"isError",True) and binding_scope(f["context"],f["event"])==f["scope"]:
            return raw_text(result)
    except Exception as error:
        logger.info("[world-followthrough] %s unavailable (%s)",name,type(error).__name__)
    return ""


def minutes(clock):
    hour,minute=map(int,clock.split(":"))
    return hour*60+minute


def estimate(raw,s,view,now):
    local=datetime.fromtimestamp(now,SHANGHAI)
    depart=now
    if s["after_meal"]:
        for task in (view or {}).get("activities",[]):
            if re.search(r"饭|吃|煮|焖|煎|烤|炒|粥|面",task["name"]):
                try:
                    t=datetime.fromisoformat(task["until"])
                    if t.tzinfo:depart=max(depart,t.timestamp())
                except (ValueError,TypeError):
                    pass
    for line in raw.splitlines():
        m=LINE.match(line)
        if not m or m[1]!=s["name"]:
            continue
        routes=m[3]
        duration=re.search(re.escape(s["mode"])+r"\s*(\d+)\s*分钟",routes)
        close=CLOSE.search(routes); bounds=HOURS.search(routes)
        info={"地点":m[1],"走法":s["mode"],"状态":"待核实"}
        if not duration or not (close or bounds):
            return info
        finish=close[1] if close else bounds[2]
        closing=minutes(finish)
        midnight=local.replace(hour=0,minute=0,second=0,microsecond=0)
        day_end=midnight+timedelta(minutes=closing)
        if bounds and minutes(bounds[1])>closing and local.hour*60+local.minute>=minutes(bounds[1]):
            day_end+=timedelta(days=1)
        if not bounds and local>day_end:
            return dict(info,关门=finish,状态="营业日期待核实")
        # World durations are in whole minutes. A fraction of a minute cannot
        # turn arrival at closing into an achievable deadline.
        leave=datetime.fromtimestamp(depart,SHANGHAI).replace(second=0,microsecond=0)
        eta=leave+timedelta(minutes=int(duration[1]))
        margin=int((day_end-eta).total_seconds()//60)
        status="到时已关门" if margin<0 else "正好关门" if margin==0 else "有到店时间"
        if bounds and minutes(bounds[1])<=closing and eta.date()==midnight.date() and eta.hour*60+eta.minute<minutes(bounds[1]):
            status="到时尚未营业"
        return dict(info,状态=status,分钟=int(duration[1]),关门=finish,最早出发=leave.strftime("%H:%M"),
                    最早到店=eta.strftime("%H:%M"),余量分钟=margin,说明="按当前出发地点，未计尚未开始的吃饭、拍照或办理时间")
    return {"地点":s["name"],"走法":s["mode"],"状态":"待核实"}


async def get_budget(plugin,event,f,s):
    if not s:
        return None
    view=f.get("view") or await current_route(plugin.context,event,f["scope"],details=True)
    f["view"]=view
    cache=getattr(plugin,"_outing_cache",None)
    if cache is None:
        cache=plugin._outing_cache={}
    stable=view and view["kind"]!="trip" and s["mode"] in ("推着车走","步行")
    key=(f["scope"],s["query"],(view or {}).get("text"),datetime.now(SHANGHAI).date().isoformat())
    saved=cache.get(key) if stable else None
    if not saved or time.monotonic()-saved[0]>300:
        raw=await read_world(f,"world_find",{"query":s["query"]})
        if stable and raw:
            if len(cache)>=16:cache.clear()
            cache[key]=(time.monotonic(),raw)
    else:
        raw=saved[1]
    budget=estimate(raw,s,view,time.time())
    if not view or (view.get("kind")=="trip" and s["mode"]=="推着车走"):
        budget={"地点":s["name"],"走法":s["mode"],"状态":"等到车所在的出发点再核对"}
    f.update(outing=s,budget=budget,find_result=raw)
    return budget


async def timed_wording(plugin,event,f,text):
    if not PROMISE.search(text):
        return text
    s=subject(text) or (f.get("outing") if REPAIR.search(text) or TIME.search(text) else None)
    if not s:
        return text
    budget=f.get("budget")
    if (not budget or not f.get("outing") or s["name"]!=f["outing"]["name"]
            or (s["after_meal"] and not f["outing"]["after_meal"])):
        budget=await get_budget(plugin,event,f,s)
    elif f.get("find_result") and f.get("view") and (f["view"]["kind"]!="trip" or s["mode"]!="推着车走"):
        budget=estimate(f["find_result"],f["outing"],f["view"],time.time())
    if not budget or budget["状态"]=="有到店时间":
        return text
    if budget["状态"] in ("正好关门","到时已关门","到时尚未营业"):
        reason=(budget["走法"]+"去"+budget["地点"]+"要"+str(budget["分钟"])+"分钟，最早"+budget["最早到店"]+
                "到，"+budget["关门"]+"关门。") if budget["状态"]!="到时尚未营业" else "按这个走法，到店时还没营业。"
    else:
        reason="从出发点到店的时间和营业时间还没核实，今晚办完我说不准。"
    sentences=re.findall(r"[^。！？!?\n]+[。！？!?\n]*|[。！？!?\n]+",text)
    # When she already explained why she will miss closing, keep her own
    # explanation. Drop only the incompatible promise, without repeating the route.
    already_explained=bool(RECONSIDER.search(text) and any(
        ("关门" in part or "打烊" in part) and re.search(r"[零〇一二两三四五六七八九十\d].{0,3}(?:点|:|：|分)",part)
        for part in sentences))
    out=[];inserted=already_explained
    for sentence in sentences:
        later_day=re.search(r"明天|改天|后天|下次|另找|另一家",sentence)
        about_errand=REPAIR.search(sentence) or s["name"] in sentence or re.search(r"今晚|今天|办完|办好|赶得上|来得及",sentence)
        conditional=re.search(r"(?:要是|如果|除非).{0,30}(?:早点|开着|不关|还开|明天|换一家)|不可能.{0,6}(?:赶得上|来得及)",sentence)
        if about_errand and PROMISE.search(sentence) and not RECONSIDER.search(sentence) and not later_day and not conditional:
            if not inserted:out.append(reason);inserted=True
        else:out.append(sentence)
    return "".join(out)


def accepted_plan(event,f,body):
    """Let an accepted feasible errand use the ordinary private plan store."""
    budget=f.get("budget") or {}
    s=f.get("outing") or {}
    if (not f.get("human") or not subject(f["human"]+" "+body) or not TIME.search(f["human"])
            or not PROMISE.search(body) or RECONSIDER.search(body) or budget.get("状态")!="有到店时间"
            or not event.get_extra("fishworld_plan_scope") or not s):
        return None
    now=datetime.now(SHANGHAI)
    day=now.replace(hour=0,minute=0,second=0,microsecond=0)
    leave_by=day+timedelta(minutes=minutes(budget["关门"])-budget["分钟"])
    start=now.replace(second=0,microsecond=0)+timedelta(minutes=1)
    if s["after_meal"]:
        permitted=day+timedelta(minutes=minutes(budget["最早出发"]))
        start=max(start,permitted)
    if not start<leave_by:
        return None
    title=s["name"]+"的"+("补胎" if s["query"]=="补胎" else "到店安排")
    old=(event.get_extra("fishworld_plan_scope") or {}).get("plans",[])
    if any(p.get("title")==title for p in old):
        return None
    return {"op":"save","title":title,"intent":"想按约"+s["mode"]+"去"+s["name"]+
            "，出发前复核营业与路程。","start":start.strftime("%Y-%m-%d %H:%M"),
            "until":leave_by.strftime("%Y-%m-%d %H:%M")}
