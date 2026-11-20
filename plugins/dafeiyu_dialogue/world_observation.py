"""Keep a small, dated excerpt of a genuine FishWorld observation for the same chat."""
from datetime import datetime, timedelta, timezone
import hashlib
import re


SHANGHAI = timezone(timedelta(hours=8))
WORLD_TIME = re.compile(r"(\d{2}-\d{2})\s+\S+\s+(\d{2}:\d{2})")
TRANSFER = re.compile(r"^\d{2}:\d{2}\s+(?:买了|购入|卖了|收到物品|获得|拿到|捡到|获赠|赠送|领取)")
CURRENT_DETAIL = re.compile(r"(?:穿|戴)(?:着|的|了|的是)?(?:什么|啥|哪件|哪套|哪一|哪条)|"
                            r"(?:自行车|单车|车子).{0,5}(?:在哪|停哪|停在什么地方)")
VISIT_QUERY = re.compile(r"[?？]|什么|啥|怎么|怎样|多少|哪|谁|几次|有没|了吗|了没|是吗|真的|说清楚|记错")
DAY_QUERY = re.compile(r"(?:今天|今早|刚才|刚刚|上午|下午|中午).{0,12}(?:买|做|去)|"
                       r"(?:买|做|去)(?:了|过|的)?(?:什么|啥|哪)")


def asks_current_detail(text):
    return (isinstance(text, str) and not any(word in text for word in ("昨天", "那天", "上次", "之前", "明天", "打算"))
            and not re.search(r"我(?:现在|今天)?(?:该|要|应该)?[穿戴]", text) and bool(CURRENT_DETAIL.search(text)))


def place_record(result):
    entries = result.splitlines()
    # Arrival prose describes the scene and its people, not the visitor's actions.
    observed = [line.partition("。")[0] if re.match(r"^\d{2}:\d{2}\s+到达：", line) else line
                for line in entries if not re.match(r"^\d{2}:\d{2}\s+在.{1,40}，看到了 ", line)]
    return {
        "result": "\n".join(observed),
        "本次地点检索里直接登记的物品交易或领取": [line for line in entries if TRANSFER.match(line)],
        "范围": "未列出某物只说明此次检索没有相应依据，不能断定没有带过它。",
    }


def current_field(result, field):
    lines = result.splitlines()
    if not lines or not WORLD_TIME.fullmatch(lines[0].partition(" · ")[0].strip()):
        return result
    matches = [line for line in lines[1:] if line.startswith(field + "：")]
    return "\n".join([lines[0].partition(" · ")[0], matches[0]]) if len(matches) == 1 else result


def visit_actions(text, place, arrival):
    """Keep own actions after this exact recorded arrival and before leaving."""
    active = False
    out = []
    for line in text.splitlines():
        stamp, sep, item = line.strip().partition(" ")
        if not sep or not re.fullmatch(r"\d{2}:\d{2}", stamp):
            continue
        item = item.strip()
        if item.startswith("到达："):
            if active:
                break
            active = stamp == arrival and item[3:].partition("。")[0] == place
        elif active:
            if item.startswith(("出发：", "改主意了，", "回到家", "拐进")):
                break
            if (item.startswith(("开始：", "完成：", "结束：", "买了", "购入", "交易取消：", "订单取消：",
                                 "购买失败：", "未成交：", "获得", "收到物品", "卖了"))
                    and len(item) <= 130):
                out.append(stamp + "  " + item)
    return out


def binding_scope(context, event):
    if not event.is_private_chat():
        return None
    meta = context.get_registered_star("astrbot_plugin_fishworld")
    fish = getattr(meta, "star_cls", None) if getattr(meta, "activated", False) else None
    if not fish or not fish.config.get("enabled", True):
        return None
    state = getattr(getattr(fish, "store", None), "data", None) or {}
    binding = state.get("binding") or {}
    if (not state.get("key_hash") or binding.get("group_id")
            or binding.get("umo") != event.unified_msg_origin
            or binding.get("sender_id") != str(event.get_sender_id())):
        return None
    return binding["umo"], state["key_hash"]


class WorldObservation:
    def __init__(self):
        self.scope = self.day = self.clock = self.row = None

    def remember(self, scope, text, now):
        if not scope or not isinstance(text, str) or not text:
            return
        lines = text.splitlines()
        stamp = lines[0].partition(" · ")[0].strip()
        clock = WORLD_TIME.fullmatch(stamp)
        day = datetime.fromtimestamp(now, SHANGHAI).date()
        if not clock or clock[1] != day.strftime("%m-%d"):
            return
        if self.scope == scope and self.day == day and clock[2] < self.clock:
            return
        facts = []
        for line in lines[1:]:
            label, sep, value = line.strip().partition("：")
            if sep and label in ("穿着", "自行车") and 0 < len(value) <= 180:
                facts.append(label + sep + value)
            elif sep and label == "今天到现在":
                parts = [part.strip() for part in value.split("；")]
                facts.extend(part for part in parts if part.startswith(("到过", "做过", "买过")) and len(part) <= 280)
        if facts:
            result = "\n".join([stamp, *facts])
            signature = hashlib.sha256(result.encode()).hexdigest()[:20]
            self.scope, self.day, self.clock = scope, day, clock[2]
            self.row = dict(id="world_seen:" + signature, tool="world_now", result=result, current_turn=False)

    def for_history(self, scope, now, existing):
        if not self.row or scope != self.scope or datetime.fromtimestamp(now, SHANGHAI).date() != self.day:
            return None
        stamp = self.row["result"].splitlines()[0]
        if any(row.get("tool") == "world_now" and row.get("result", "").startswith(stamp) for row in existing):
            return None
        return self.row.copy()

    def mentioned_place(self, scope, human, now):
        row = self.for_history(scope, now, [])
        if not row or not isinstance(human, str) or not VISIT_QUERY.search(human):
            return None
        for line in row["result"].splitlines():
            if line.startswith("到过 "):
                names = (item.strip() for item in line[3:].split("、"))
                return max((name for name in names if 2 <= len(name) <= 20 and name in human),
                           key=len, default=None)
        return None
