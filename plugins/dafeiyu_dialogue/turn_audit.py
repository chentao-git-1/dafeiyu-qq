"""Content-free first-request checks for genuine autonomous FishWorld turns."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import re
from weakref import WeakValueDictionary

from astrbot.api import logger
from dafeiyu.dialogue.generation import BEGIN, END, POLICY


EVENT_KEY = "dafeiyu_world_turn_audit"
_turns = WeakValueDictionary()
_attempt = ContextVar("dafeiyu_world_wire_attempt", default=None)
_contract = BEGIN + "\n" + POLICY + "\n" + END
_turn_data = "\n\n【大肥鱼本轮资料】\n"
_end_data = "\n【资料结束】"


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).digest()


@dataclass
class Turn:
    tag: str
    expected: dict = field(default_factory=dict)
    mood_mode: str = "missing"
    payloads: int = 0
    first_confirmed: bool = False
    repairs: int = 0
    shape: str = "none"
    finished: bool = False


def arm(event, nonce):
    wake = event.get_extra("fishworld_wake_id")
    if (not isinstance(wake, str) or not re.fullmatch("[a-f0-9]{32}", wake)
            or not re.fullmatch("fishworld_[a-f0-9]{32}", str(getattr(getattr(event, "message_obj", None), "message_id", "")))
            or any(event.get_extra(flag) for flag in ("fishworld_manual_test", "fishworld_manual_wake", "cron_job", "cron_payload"))):
        return
    turn = event.get_extra(EVENT_KEY)
    if turn is None:
        turn = Turn(_digest(wake).hex()[:12])
        event.set_extra(EVENT_KEY, turn)
        logger.info("[world-turn] turn=%s phase=start bound=%d", turn.tag, bool(nonce))
    if nonce and not turn.finished:
        _turns[nonce] = turn


def expect(event, name, data):
    turn = event.get_extra(EVENT_KEY)
    if turn and not turn.finished:
        body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        turn.expected[name] = _digest(body.replace("<", "\\u003c"))
        if name == "SELF_MOOD" and isinstance(data, dict):
            tone = data.get("感受")
            if isinstance(tone, list) and tone:
                turn.mood_mode = "layered"
            elif tone == "平静" and data.get("听澜自己的心情") == 50:
                turn.mood_mode = "baseline"
            else:
                turn.mood_mode = "scalar"


@contextmanager
def provider_attempt(nonce):
    """A nested auxiliary request must never inherit its parent's primary-turn audit."""
    turn = _turns.get(nonce)
    if turn and turn.finished:
        turn = None
    first = turn is not None and turn.payloads == 0
    token = _attempt.set(turn)
    try:
        yield
        # If the adapter retried internally we cannot attribute success to attempt one.
        if first and turn.payloads == 1:
            turn.first_confirmed = True
    finally:
        _attempt.reset(token)


def _text_parts(content):
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                yield part["text"]


def _matches(tail, name, expected):
    if expected is None:
        return False
    begin = f"<!-- BEGIN_DAFEIYU_{name} -->\n"
    end = f"\n<!-- END_DAFEIYU_{name} -->"
    if tail.count(begin) != 1:
        return False
    body, separator, _ = tail.partition(begin)[2].partition(end)
    return bool(separator) and _digest(body) == expected


def observe_payload(payload, extra):
    """Sample the adapter's SDK-bound body after provider-specific overrides."""
    turn = _attempt.get()
    if turn is None or turn.finished:
        return
    turn.payloads += 1
    if turn.payloads != 1:
        return
    messages = extra.get("messages", payload.get("messages"))
    rows = messages if isinstance(messages, list) else []
    contract = any(_contract in part for row in rows if isinstance(row, dict) and row.get("role") == "system"
                   for part in _text_parts(row.get("content")))
    last_user = next((row for row in reversed(rows) if isinstance(row, dict) and row.get("role") == "user"), {})
    tail = ""
    for part in _text_parts(last_user.get("content")):
        _, start, remainder = part.rpartition(_turn_data)
        sections, end, _ = remainder.partition(_end_data)
        if start and end:
            tail = sections
    fmt = extra.get("response_format", payload.get("response_format"))
    json_mode = isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema")
    mood = _matches(tail, "SELF_MOOD", turn.expected.get("SELF_MOOD"))
    world = _matches(tail, "WORLD_PREFETCH", turn.expected.get("WORLD_PREFETCH"))
    logger.info("[world-turn] turn=%s phase=wire contract=%d json=%d mood=%d world=%d prepared_mood=%d prepared_world=%d mood_mode=%s",
                turn.tag, contract, json_mode, mood, world, "SELF_MOOD" in turn.expected, "WORLD_PREFETCH" in turn.expected,
                turn.mood_mode if mood else "missing")


def format_recovery(event):
    turn = event.get_extra(EVENT_KEY)
    if turn and not turn.finished:
        turn.repairs += 1


def failed_shape(event, text, error):
    turn = event.get_extra(EVENT_KEY)
    if not turn or turn.finished or turn.shape != "none":
        return
    value = text.lstrip() if isinstance(text, str) else ""
    if not value:
        turn.shape = "empty"
    elif isinstance(error, json.JSONDecodeError):
        turn.shape = "invalid_json" if value.startswith(("{", "[", "```")) else "plain_text"
    else:
        turn.shape = "schema"


def finish(event):
    turn = event.get_extra(EVENT_KEY)
    if turn and not turn.finished:
        turn.finished = True
        logger.info("[world-turn] turn=%s phase=end payloads=%d first_confirmed=%d format_calls=%d shape=%s valid=%d quiet=%d",
                    turn.tag, turn.payloads, turn.first_confirmed, turn.repairs, turn.shape,
                    bool(event.get_extra("dafeiyu_generation_valid")), bool(event.get_extra("dafeiyu_world_quiet")))
