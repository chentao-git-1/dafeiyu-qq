"""Read the resident's recorded wardrobe changes without consulting private chat."""
import asyncio
import re
from datetime import date

from astrbot.core.agent.run_context import ContextWrapper


CLOCK = r"(?:[01]\d|2[0-3]):[0-5]\d"
ROW = re.compile(rf"^({CLOCK})\s+(.+)$")
CHANGE = re.compile(r"^(?:换了衣服[：:]\s*|换上了[：:]?\s*)(.*)$")
CURSOR = re.compile(r"^——\s*更早的[：:]\s*before=([\w.-]{1,24})\s*$", re.MULTILINE)
PRIVATE = re.compile(r"私[信聊]|留言|给你发|发给|好友|说[：:]|对话|聊天|短信|邮箱|验证码|https?://|"
                     r"sk-|\bfwr?_|[#@]|(?<!\d)1[3-9]\d{9}(?!\d)")
LABEL = re.compile(r"[\w\u3400-\u9fff 、，,。.·（）()／/＋+\-]{1,120}")
DAY = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
MAX_PAGES = 12


def recorded_outfit(rows, at, scene):
    changes = [(index, minute, match[1].strip())
               for index, (minute, detail) in enumerate(rows) if minute <= at
               and (match := CHANGE.fullmatch(detail))]
    if not changes:
        return ""
    if changes[-1][1] == at:
        # An earlier and a later activity can share the same displayed minute.
        positions = [index for index, (minute, detail) in enumerate(rows)
                     if minute == at and scene == detail]
        if len(positions) != 1:
            return ""
        changes = [change for change in changes if change[0] <= positions[0]]
    if not changes:
        return ""
    value = changes[-1][2]
    return value if LABEL.fullmatch(value) and not PRIVATE.search(value) else ""


async def outfit_at(context, day, at, scene_row):
    if not isinstance(day, str) or not isinstance(at, str) or not re.fullmatch(CLOCK, at):
        return ""
    try:
        if date.fromisoformat(day).isoformat() != day:
            return ""
    except ValueError:
        return ""

    scene = str(scene_row or "").strip()
    timed = ROW.fullmatch(scene)
    if timed:
        if timed[1] != at:
            return ""
        scene = timed[2].strip()
    try:
        manager = context.get_llm_tool_manager()
        tool = manager.get_func("world_history")
        if not (tool and tool.active and tool.mcp_server_name == "fish-world"):
            return ""
        args = {"date": day, "limit": 30}
        rows = []
        seen = set()
        for _ in range(MAX_PAGES):
            result = await asyncio.wait_for(tool.call(ContextWrapper(context=None, tool_call_timeout=5), **args), 6)
            if result.isError:
                return ""
            text = "\n".join(part.text for part in result.content if part.type == "text")
            lines = text.splitlines()
            # Some servers include a dated page heading. Reject a conflicting day.
            if any(stamp != day for line in lines if not ROW.fullmatch(line.strip()) for stamp in DAY.findall(line)):
                return ""
            page = [(match[1], match[2].strip()) for line in lines
                    if (match := ROW.fullmatch(line.strip()))]
            if not page or any(a[0] > b[0] for a, b in zip(page, page[1:])) or (rows and page[-1][0] > rows[0][0]):
                return ""
            rows = page + rows
            cursor = CURSOR.search(text)
            if not cursor or any(minute < at and CHANGE.fullmatch(detail) for minute, detail in page):
                return recorded_outfit(rows, at, scene)
            if cursor[1] in seen:
                return ""
            seen.add(cursor[1])
            args["before"] = cursor[1]
    except Exception:
        # A cover can still depict the scene when the world is unreachable.
        return ""
    return ""
