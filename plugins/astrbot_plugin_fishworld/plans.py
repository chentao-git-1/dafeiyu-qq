"""Select FishWorld reminders using the fish's own plans and observed notifications."""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from datetime import datetime, timedelta
from typing import Any

from .core import BEIJING, COMPLETION_KINDS as SIGNAL_KINDS

SIGNAL_HOLD = 15 * 60
MISSED_HOLD = 48 * 3600
MAX_PLANS = 32
DATE_TIME = "%Y-%m-%d %H:%M"
PLAN_POLICY = (
    '鱼世界安排只记录你自己确实想在以后接着做的事。根据本轮北京时间，'
    '可在已有最终回复 JSON 中加可选 fishworld_plan 字段，例如'
    '{"op":"save","title":"去找朋友","intent":"傍晚想去找朋友","start":"YYYY-MM-DD 17:30","until":"YYYY-MM-DD 19:00"}。'
    '具体时刻只写 start；一个时段写 start 和 until。'
    '等实际活动完成或到达时，改用 event 字段，值为 {"kind":"done","label":"当前活动名"} 或 '
    '{"kind":"arrive","label":"实际目的地"}，标签逐字取本轮世界工具所见，until 可写最迟有效时间。'
    '调整同一件事可附已有 id；办妥或主动放下时可写 {"op":"clear","id":"已有安排编号"}。'
    '没明确打算就省略该字段。安排是以前的想法，按现状决定是否继续。普通聊天按当前话题取用。'
)
ACTIVITY_TOOLS = {"world_now", "world_go", "world_do", "world_say"}
FIELDS = {"id", "title", "intent", "opened", "due", "until", "event", "created"}


def normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def short_text(value: Any, length: int) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected plan text")
    one_line = " ".join(value.split())
    if not one_line or len(one_line) > length:
        raise ValueError("Invalid plan text length")
    return one_line


def parse_time(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("Expected Beijing time")
    local = datetime.strptime(value, DATE_TIME)
    if local.strftime(DATE_TIME) != value:
        raise ValueError("Expected exact local date and minute")
    return local.replace(tzinfo=BEIJING).timestamp()


def local_minute(value: float) -> str:
    return datetime.fromtimestamp(value, BEIJING).strftime(DATE_TIME)


def validate_saved(plans: Any) -> None:
    if not isinstance(plans, list) or len(plans) > MAX_PLANS:
        raise ValueError("Invalid FishWorld plans")
    ids = set()
    for row in plans:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise ValueError("Invalid FishWorld plan")
        identity = row["id"]
        if not isinstance(identity, str) or len(identity) != 12 or identity in ids:
            raise ValueError("Invalid FishWorld plan ID")
        ids.add(identity)
        short_text(row["title"], 60)
        short_text(row["intent"], 160)
        for key in ("opened", "due", "until", "created"):
            value = row[key]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Invalid FishWorld plan time")
        if not row["created"] <= row["opened"] <= row["due"] < row["until"]:
            raise ValueError("Invalid FishWorld plan window")
        event = row["event"]
        if event is not None:
            if not isinstance(event, dict) or set(event) != {"kind", "label"} or event["kind"] not in SIGNAL_KINDS:
                raise ValueError("Invalid FishWorld plan signal")
            short_text(event["label"], 60)


def choose_minute(key: str, opened: float, until: float) -> float:
    """Pick once, with a stable choice across retries and restarts."""
    count = max(1, math.ceil((until - opened) / 60))
    value = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    return opened + (value % count) * 60


def fallback_due(now: float, key_hash: str) -> bool:
    today = datetime.fromtimestamp(now, BEIJING)
    start = today.replace(hour=14, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=5)
    return now >= choose_minute(today.date().isoformat() + key_hash, start.timestamp(), end.timestamp())


def has_world_activity(evidence: list[dict]) -> bool:
    return any(row.get("complete") and row.get("tool") in ACTIVITY_TOOLS for row in evidence)


def build_plan(change: dict, now: float, observed_at: float, evidence: list[dict], identity: str = "") -> dict:
    title = short_text(change.get("title"), 60)
    intent = short_text(change.get("intent"), 160)
    identity = identity or hashlib.sha256(normalize(title).encode()).hexdigest()[:12]
    event = change.get("event")
    until = parse_time(change["until"]) if "until" in change else None
    if event is not None:
        if "start" in change or not isinstance(event, dict) or event.get("kind") not in SIGNAL_KINDS:
            raise ValueError("Invalid plan signal")
        label = short_text(event.get("label"), 60)
        if not any(row.get("complete") and row.get("tool") in ACTIVITY_TOOLS
                   and normalize(label) in normalize(row.get("result") or "") for row in evidence):
            raise ValueError("No current world evidence for this signal")
        event = {"kind": event["kind"], "label": label}
        opened = due = observed_at
        until = until if until is not None else now + 86400
    else:
        start = parse_time(change.get("start"))
        if start <= now:
            raise ValueError("The proposed start has already passed")
        opened = max(now, start)
        due = opened if until is None else choose_minute(identity, opened, until)
        until = until if until is not None else opened + 30 * 60
    if until <= max(due, now) or until - opened > 7 * 86400:
        raise ValueError("Invalid FishWorld plan window")
    return dict(id=identity, title=title, intent=intent, opened=opened, due=due,
                until=until, event=event, created=observed_at)


def signal_label(kind: str, text: str) -> str:
    text = text.strip()
    if kind == "arrive":
        for marker in ("到了", "到达了", "到达"):
            before, sep, label = text.rpartition(marker)
            if sep:
                return normalize(label)
        return ""
    head, sep, _ = text.replace("：", ":", 1).partition(":")
    if sep:
        return normalize(head)
    for suffix in ("结束了", "完成了"):
        if text.endswith(suffix):
            return normalize(text.removesuffix(suffix))
    return ""


def matches_signal(plan: dict, row: dict) -> bool:
    event = plan["event"]
    item = row["item"]
    stamp = item.get("t")
    return bool(event and event["kind"] == item["kind"] and type(stamp) is int
                and plan["created"] <= stamp / 1000 < plan["until"]
                and signal_label(item["kind"], item["text"]) == normalize(event["label"]))


def retain_signal(plans: list[dict], row: dict, now: float) -> bool:
    return row["item"]["kind"] in SIGNAL_KINDS and (
        now - row["received"] < SIGNAL_HOLD
        or any(plan["until"] > now and matches_signal(plan, row) for plan in plans)
    )


def select_plans(plans: list[dict], signals: list[dict], now: float, joining: bool,
                 *, wake_kinds=(), wake_rows=()):
    """Join open windows to an eligible wake even before their chosen minute."""
    eligible = []
    matched = {}
    should_wake = joining
    if wake_kinds:
        # Plans may join the selected wake but cannot take another completion from the queue.
        selected_ids = {row["id"] for row in wake_rows}
        signals = [row for row in signals
                   if row["item"]["kind"] not in wake_kinds or row["id"] in selected_ids]
    for plan in plans:
        if not plan["opened"] <= now < plan["until"]:
            continue
        if plan["event"] is None:
            eligible.append(plan)
            should_wake = should_wake or plan["due"] <= now
            continue
        signal = next((row for row in signals if matches_signal(plan, row)), None)
        if signal:
            eligible.append(plan)
            matched[signal["id"]] = signal
            should_wake = True
    return (eligible, list(matched.values())) if should_wake else ([], [])


def missed_plans(plans: list[dict], now: float):
    return [row for row in plans if row["until"] <= now < row["until"] + MISSED_HOLD]


def plan_context(now: float, plans: list[dict], due: list[dict], missed: list[dict], *, structured=True) -> str:
    rows = []
    for status, items in (("到时提醒", due), ("时段已过", missed), ("自己的待办", plans)):
        for plan in items:
            rows.append(dict(id=plan["id"], title=plan["title"], intent=plan["intent"],
                             state=status, start=local_minute(plan["opened"]),
                             until=local_minute(plan["until"]), event=plan["event"]))
    data = json.dumps(dict(beijing=local_minute(now), plans=rows), ensure_ascii=False).replace("<", "\\u003c")
    policy = PLAN_POLICY if structured else "以下是你自己以前留下的打算和时间状态，按当前情况自行决定。"
    return "\n<!-- BEGIN_DAFEIYU_WORLD_PLAN -->\n" + policy + "\n" + data + "\n<!-- END_DAFEIYU_WORLD_PLAN -->"


def apply_change(plans: list[dict], change: dict, offered: list[dict], now: float,
                 observed_at: float, evidence: list[dict]) -> list[dict]:
    offered_by_id = {row["id"]: row for row in offered}
    current = {row["id"]: row for row in plans}
    identity = change.get("id", "")
    if not isinstance(identity, str) or (identity and identity not in offered_by_id):
        return plans
    if change.get("op") == "clear":
        if identity in current and current[identity] == offered_by_id.get(identity):
            return [row for row in plans if row["id"] != identity]
        return plans
    if change.get("op") != "save":
        return plans
    saved = build_plan(change, now, observed_at, evidence, identity)
    identity = saved["id"]
    prior = current.get(identity)
    if prior is not None and prior != offered_by_id.get(identity):
        return plans
    if (prior is not None and prior["event"] and prior["event"] == saved["event"]
            and prior["until"] > now and not change.get("id") and "until" not in change):
        return plans
    if prior is None and len(plans) >= MAX_PLANS:
        return plans
    return [row for row in plans if row["id"] != identity] + [saved]
