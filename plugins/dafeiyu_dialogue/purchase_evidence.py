"""Dated FishWorld purchase receipts for one private question."""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta

from astrbot.api import logger

from .world_observation import SHANGHAI, binding_scope, visit_actions


CHECK = "dafeiyu_purchase_check"
PAGE = re.compile(r"(?m)^——\s*更早的[：:]\s*before=([\w.-]{1,24})\s*$")
ROW = re.compile(r"^([0-2]\d:[0-5]\d)\s+(.+)$")
ARRIVAL = re.compile(r"^到达：([^。（，、\n]{1,30})")
BOUGHT = re.compile(r"^(?:买了\s*|购入[：:\s]+)([^，。；\n]{1,80})")
FAILED = re.compile(r"^(?:交易取消|订单取消|未成交|购买失败)[：:]\s*\S")
QUESTION = re.compile(r"[?？]|什么|啥|哪|几|多少|是不|有没|没有|到底|所以|真的|了[吗没吧]|过没|对吧|没买|买没买|[吗吧]$")
FUTURE = re.compile(r"帮我买|替我买|要不要买|想不想买|想买|打算买|建议|推荐|应该买|该买|怎么买|哪里买|明天|假如|假设|编个故事")
RELATIVE = re.compile(r"那(?:里|儿|家|边|次|趟)|刚才|所以|到底|真没|还是没|买没买|那(?!天)|^又?没买")
LOCATION = re.compile(r"在|那(?:家|儿|里|边|趟)|店|行|市场|商场|超市|摊")
TIMED_PURCHASE = re.compile(r"买|购物|购入|下单|付款|消费|花钱|一(?:分|毛|贝)钱?(?:也|都)?没花")


def asks_purchase(human):
    if not isinstance(human, str) or not (verb := TIMED_PURCHASE.search(human)) or not QUESTION.search(human):
        return False
    if FUTURE.search(human):
        return False
    subject = re.sub(r"(?:给|替|跟)(?:我|她|他|你)(?:们)?", "", human[:verb.start()])
    people = re.findall(r"[你我他她]|店主|师傅|老板", subject)
    return not people or people[-1] == "你"


def question_day(human, now):
    day = datetime.fromtimestamp(now, SHANGHAI).date()
    literal = re.search(r"\d{4}-\d{2}-\d{2}", human)
    if literal:
        try:
            return datetime.strptime(literal[0], "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    if any(term in human for term in ("上周", "上个月", "以前", "去年", "上星期")):
        return None
    return (day - timedelta(days=2 if "前天" in human else 1 if "昨天" in human else 0)).isoformat()


def match_place(text, names):
    exact = [name for name in names if name in text]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    # Accept e.g. 服装店 for 织岸服装店 only if the suffix identifies a single place.
    matches = {name for name in names if any(name[-n:] in text for n in range(2, len(name)))}
    return matches.pop() if len(matches) == 1 else None


def project(pages, human, previous, hint, day, complete):
    lines = [line for page in pages for line in page.splitlines() if ROW.match(line)]
    arrivals = []
    for line in lines:
        stamp, detail = ROW.match(line).groups()
        m = ARRIVAL.match(detail)
        if m:
            arrivals.append((stamp, m[1].strip()))
    names = {name for _, name in arrivals}
    if hint:
        names.add(hint)
    place = match_place(human, names)
    if not place and RELATIVE.search(human) and not re.search(r"(?:[\u3400-\u9fff]{2,}店|车行|市场)", human):
        place = match_place(previous, names)
    daily = not place and not LOCATION.search(human) and any(x in human for x in ("今天", "昨天", "前天", "当天", "一共"))
    all_visits = bool(place and re.search(r"今天|昨天|前天|当天|一共|总共", human)
                      and not re.search(r"这趟|那趟|刚才|刚刚|上次|最后一次", human))
    own = []
    anchors = list(dict.fromkeys(stamp for stamp, name in arrivals if name == place))
    time_given = re.search(r"(?<!\d)([0-2]?\d:[0-5]\d)(?!\d)", human)
    if time_given:
        anchors = [stamp for stamp in anchors if stamp == time_given[1].zfill(5)]
    if anchors:
        timeline = "\n".join(lines)
        for anchor in anchors if all_visits else anchors[-1:]:
            own.extend(visit_actions(timeline, place, anchor))
    elif daily:
        own = [line for line in lines if BOUGHT.match(ROW.match(line)[2])]
    receipts = []
    for line in own:
        record = ROW.match(line)
        if record and (m := BOUGHT.match(record[2])):
            receipts.append((line, m[1].strip()))
    return {"date": day, "place": place, "arrival": anchors[-1] if anchors else None, "daily": daily, "all_visits": all_visits,
            "read": bool(pages), "complete": complete,
            "receipts": list(dict.fromkeys(line for line, _ in receipts)),
            "failed": [line for line in own if FAILED.match(ROW.match(line)[2])],
            "items": list(dict.fromkeys(item for _, item in receipts)),
            "actions": [line for line in own if re.match(r"^\d\d:\d\d\s+(?:开始|完成|结束)：", line)][:4]}


def material(check):
    fields = {k: check[k] for k in ("date", "place", "arrival", "receipts", "failed", "actions")}
    fields["购物核对结果"] = "查到这些明确购买" if check["receipts"] else "未能确认是否购买"
    if not check["read"]:
        fields["检索范围"] = "没有取得可用的当天记录"
    elif check["all_visits"] or check["daily"]:
        fields["检索范围"] = "当天全部已返回记录" if check["complete"] else "当天已读到的一部分记录"
    elif check["arrival"]:
        fields["检索范围"] = "所列到达时间开始的这次到访"
    fields["含义"] = "记录只支持列出的交易。是否买过其他东西仍未知。回答时把这份不确定性留在话里。"
    if check.get("evidence_id"):
        fields["evidence_id"] = check["evidence_id"]
    return fields


async def prepare_purchase(plugin, event, request, human, tool, scope):
    if not scope or not asks_purchase(human):
        return False
    saved = event.get_extra(CHECK)
    if saved is None:
        from astrbot.core.agent.run_context import ContextWrapper
        now = time.time()
        day = question_day(human, now)
        previous = next((m["content"] for m in reversed(getattr(request, "contexts", None) or [])
                         if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)), "")
        # A real prior question can identify "那趟". Earlier assistant prose is not a receipt.
        if not asks_purchase(previous):
            previous = ""
        if previous and RELATIVE.search(human) and not re.search(r"今|昨|前天|\d{4}-\d{2}-\d{2}", human):
            day = question_day(previous, now)
        hint = plugin.world_observation.mentioned_place(scope, human, now) if day == question_day("", now) else None
        pages = []
        cursor = None
        cursors = set()
        saved = project([], human, previous, hint, day, False)
        if day and tool and getattr(tool, "active", False) and getattr(tool, "mcp_server_name", None) == "fish-world":
            try:
                async with asyncio.timeout(8):
                    for _ in range(4):
                        args = {"date": day, "limit": 30}
                        if cursor:
                            args["before"] = cursor
                        result = await tool.call(ContextWrapper(context=None, tool_call_timeout=4), **args)
                        if result.isError or binding_scope(plugin.context, event) != scope:
                            break
                        text = "\n".join(part.text for part in result.content if part.type == "text")
                        if not text.strip():
                            break
                        pages.insert(0, text)
                        match = PAGE.search(text)
                        saved = project(pages, human, previous, hint, day, not match)
                        if (saved["arrival"] and not saved["all_visits"]) or not match or match[1] in cursors:
                            break
                        cursor = match[1]
                        cursors.add(cursor)
            except Exception as error:
                logger.info("[world-purchases] history unavailable (%s)", type(error).__name__)
        if binding_scope(plugin.context, event) != scope:
            return False
        saved["scope"] = scope
        saved["negative_question"] = bool(re.search(r"(?:没(?:有)?|未).{0,5}买|空手|没(?:有)?花", human))
        saved["simple_question"] = len(re.findall(r"[?？]+", human)) <= 1
        event.set_extra(CHECK, saved)
        evidence = [*saved["receipts"], *saved["failed"], *saved["actions"]]
        if evidence:
            # Store precisely the verified, dated excerpt shown to the model.
            rows = event.get_extra("dafeiyu_dialogue_tool_evidence", [])
            if len(rows) < 8:
                saved["evidence_id"] = "tool:" + str(len(rows))
                rows.append({"tool": "world_history", "complete": True, "excerpt": True,
                             "result": "\n".join([str(day), *evidence])})
                event.set_extra("dafeiyu_dialogue_tool_evidence", rows)
    if saved.get("scope") != scope:
        return False
    request.system_prompt += ("\n<!-- BEGIN_DAFEIYU_PLACE_HISTORY -->\n" +
                              json.dumps(material(saved), ensure_ascii=False).replace("<", "\\u003c") +
                              "\n<!-- END_DAFEIYU_PLACE_HISTORY -->")
    return True


QUOTES = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"')
CLAUSE = re.compile(r"([^，,。！？!?；;\n]+)([，,。！？!?；;\n]*)|([，,。！？!?；;\n]+)")
DENIAL = re.compile(
    r"(?:没(?:有)?|并无|不曾|未曾|从未|并未|未|压根不)(?:真的|实际|另外|额外|为此|再|去){0,3}"
    r"(?:(?:在|从|给).{1,16}?)?(?:再|去|掏钱|花钱)?(?:买|入手|购入|购买|消费|付款|下单|付过款|花钱|花(?:过|了)?(?:一分|一毛|一贝|半分|半毛|半贝|任何钱)|掏钱)"
    r"|(?:零|没有(?:发生)?(?:任何|一笔)?)消费|分文(?:未|没)花|一(?:分|毛|贝)钱?(?:都|也)?(?:没|未)花"
    r"|(?:没有|无)(?:发生)?任何(?:一笔)?交易|(?:哪有|怎么可能)(?:在.{1,12}?)?买"
    r"|(?:空着?手|两手空空)(?:而归|回来|回去|离开|回家)|(?:什么|啥)(?:东西)?(?:也|都)?没(?:带走|往回拿)"
)
BARE = re.compile(r"^(?:我|那趟|那次|当时|这次|所以|嗯|嗯嗯|确实|真的|肯定|就是|还真|应该|好像|当然|的确)*"
                  r"(?:没有|没|没呢|没有过|没这回事|一件也没有)(?:啊|呀|呢|哦|的|吧|啦|了)*$")
AGREE = re.compile(r"^(?:嗯+|对(?:的|呀|啊)?|是(?:的|啊|呀)|没错|肯定|确实|当然)[啊呀哦呢的]*$")
UNCERTAIN = re.compile(r"买没买|买过没有|有没有买|不(?:能|敢|好)(?:说|断定|确定|确认)|"
                       r"不确定|说不准|说不清|不得而知|记不清|记不准|无法确认|不能证明|查不(?:到|出)|"
                       r"没(?:有)?(?:查到|找到|看到|能确认|法确认)|没(?:有)?(?:这笔|相关|那次)?(?:购买|购物|付款|交易)?记录")
OTHER = re.compile(r"(?:你|他|她|别人|爸爸|师傅|店主)(?:自己|本人|当时|那天|今天|也|都|确实|根本|就|还|肯定|真的|应该|大概|可能)*$")
NOT_PROVEN = re.compile(r"(?:说不准|不确定|不敢说|不能说(?:成)?|不能断定|无法确定|没有证据(?:说明|证明)|"
                        r"不(?:等于|代表|意味着|是说|能证明|能推出)|不足以(?:说明|证明))"
                        r"(?:我|到底|当时|是否|是不是|真的|那天|肯定)*$")


def unsupported(clause, allow_bare, negative_question):
    clause = clause.strip()
    # A conclusion after "所以" is a separate assertion even if it follows a hedge.
    for part in re.split(r"(?:所以|因此|也就是说|其实|但(?:是)?|反正)", clause):
        part = part.strip()
        if allow_bare and (BARE.fullmatch(part) or (negative_question and AGREE.fullmatch(part))):
            return True
        for match in DENIAL.finditer(part):
            before, after = part[:match.start()], part[match.end():]
            if (OTHER.search(before) or NOT_PROVEN.search(before)
                    or ("有没有买" in part or "买没买" in part) and UNCERTAIN.search(part)):
                continue
            if re.search(r"(?:记不清|说不准|不能确定|我不确定)", after):
                continue
            if re.search(r"(?:这句|那句|这话|的说法)?(?:是我|是|我)?(?:讲岔了|说错了|不准确|没有依据|太绝对)", after):
                continue
            if re.search(r"(?:记录|小票|凭证|账单|证据)", after) and not re.search(r"就是|肯定|确定", before):
                continue
            return True
    return False


def supported_answer(check):
    if check["items"]:
        goods = "、".join(check["items"][:5])
        return "我能确认买了" + goods + "，其他的我说不准"
    if check["failed"]:
        outcome = ROW.match(check["failed"][0])[2]
        return "能确认的是这笔记录写着“" + outcome + "”，其他购买我说不准"
    if check["read"] and (check["arrival"] or check["daily"]):
        return "我没查到那次的购买记录，买没买我说不准"
    return "那次买没买东西，我现在说不准"


def calibrate(text, check):
    if not check or not isinstance(text, str) or not text:
        return text
    masked = QUOTES.sub(lambda m: " " * len(m[0]), text)
    parts = list(CLAUSE.finditer(text))
    bad = set()
    bare = check.get("simple_question", True)
    for index, match in enumerate(parts):
        if not match[1]:
            continue
        clause = masked[match.start(1):match.end(1)].strip()
        if unsupported(clause, bare, check.get("negative_question", False)):
            bad.add(index)
        elif not (AGREE.fullmatch(clause) or clause in {"所以", "我想想", "那趟", "那次", check.get("place")}):
            bare = False
    if not bad:
        return text
    replacement = supported_answer(check)
    if not check["items"] and not check["failed"]:
        kept = " ".join(masked[m.start():m.end()] for i, m in enumerate(parts) if i not in bad)
        if re.search(r"(?:买没买|有没有买).{0,10}(?:说不准|不确定|记不清)", kept):
            replacement = ""
        elif re.search(r"没(?:有)?(?:查到|找到|看到).{0,12}(?:购买|消费|交易)?记录", kept):
            replacement = "买没买我说不准"
    parts_out = []
    first = min(bad)
    for index, match in enumerate(parts):
        if index not in bad:
            parts_out.append(match[0])
        elif index == first and replacement:
            parts_out.append(replacement + (match[2] or ""))
    return re.sub(r"[，,；;]+$", "。", "".join(parts_out))
