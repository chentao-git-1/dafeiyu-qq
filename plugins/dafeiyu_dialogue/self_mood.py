"""Ground the character's feelings in the original QQ exchange or FishWorld receipt."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import re
import sqlite3
import time

from astrbot.api import logger
from dafeiyu.dialogue.catalog import MemoryCatalog
from dafeiyu.dialogue.privacy_cache import privacy_cutoff
from dafeiyu.memory.interaction import SENSITIVE

TURN = "dafeiyu_self_mood_turn"
PROPOSAL = "dafeiyu_self_mood_proposal"
WORLD_KINDS = {"event": "鱼世界通知", "world_go": "出行回执", "world_do": "行动回执", "world_say": "发言回执"}
BEIJING = timezone(timedelta(hours=8))


def mark(kind, value):
    return hashlib.sha256((kind + "\0" + value).encode()).hexdigest()[:24]


def fish_instance(plugin):
    meta = plugin.context.get_registered_star("astrbot_plugin_fishworld")
    return getattr(meta,"star_cls",None) if getattr(meta,"activated",False) else None


def chat_source(origin, actor, mid):
    return mark("chat", "\0".join((origin, str(actor), str(mid))))


def excerpt_digest(source, human, excerpt):
    # Editing the surrounding sentence can reverse the meaning of an unchanged excerpt.
    return mark("excerpt/" + source, human + "\0" + excerpt)


def read_chat_sources(root, origin, actor, private, message_ids):
    """Read only still-visible human messages, following the same privacy cutoff as the catalogue."""
    data = root / "plugin_data"
    cutoff = privacy_cutoff(data / "dafeiyu_dialogue/learning.db", origin)
    now = time.time()
    params = [origin, max(cutoff, now - 30*86400), now, *message_ids]
    placeholders = ",".join("?" for _ in message_ids)
    sql = f"SELECT qq,message_id,human FROM exchanges WHERE origin=? AND at BETWEEN ? AND ? AND message_id IN ({placeholders})"
    if private:
        sql += " AND qq=?"
        params.append(actor)
    rows = MemoryCatalog._read(data / "dafeiyu_natural_chat/interaction.db", sql, params)
    return {chat_source(origin, r["qq"], r["message_id"]): r["human"] for r in rows
            if isinstance(r["human"], str) and not SENSITIVE.search(r["human"])}


async def describe(plugin, event, view):
    feelings = view.get("feelings", [])
    ids = {f["cause"]["mid"] for f in feelings if f.get("cause", {}).get("kind") == "chat"}
    messages = {}
    if ids and not event.get_extra("fishworld_wake_id") and getattr(plugin, "root", None):
        try:
            messages = await asyncio.to_thread(read_chat_sources, plugin.root, event.unified_msg_origin,
                                               str(event.get_sender_id()), event.is_private_chat(), sorted(ids))
        except (OSError, sqlite3.Error, ValueError):
            pass
    details, available, evidence = [], {}, {}
    for f in feelings:
        info = {"感受": f["tone"], "强度": f["strength"]}
        cause = f.get("cause", {})
        kind = cause.get("kind")
        excerpt = ""
        label = ""
        if kind == "chat":
            human = messages.get(f["source"], "")
            excerpt = human[cause["start"]:cause["start"] + cause["size"]]
            if excerpt and excerpt_digest(f["source"], human, excerpt) == cause["digest"]:
                label = "本会话当时的原话"
        elif kind in WORLD_KINDS:
            label, excerpt = WORLD_KINDS[kind], cause["text"]
        if label:
            key = f"f{len(details)+1}"
            available[key] = (f["source"], f["tone"])
            info["编号"] = key
            info[label] = excerpt
            info["记录时间"] = datetime.fromtimestamp(f["at"], BEIJING).strftime("%m-%d %H:%M")
            evidence_id = "mood:" + key
            info["evidence_id"] = evidence_id
            evidence[evidence_id] = {"kind": "memory", "data": {
                "source": label, "excerpt": excerpt, "recorded_at": f["at"], "current_turn": False}}
        details.append(info)
    return details, available, evidence


async def validated_evidence(plugin, event, audience=""):
    """Re-open offered excerpts only for the original audience and still-bound character."""
    context = event.get_extra(TURN) or {}
    offered = context.get("evidence", {})
    if not offered or (audience and audience != event.unified_msg_origin):
        return {}
    fish = fish_instance(plugin)
    if (event.unified_msg_origin != context["origin"] or not fish
            or fish.mood_scope(event) != context["scope"]):
        raise ValueError("Mood evidence binding changed")
    _, _, current = await describe(plugin, event, context["offered_view"])
    if current != offered or fish_instance(plugin) is not fish or fish.mood_scope(event) != context["scope"]:
        raise ValueError("Mood evidence was withdrawn or corrected")
    return current


async def prepare(plugin, event, human):
    fish = fish_instance(plugin)
    scope = fish.mood_scope(event) if hasattr(fish,"mood_scope") else None
    view = fish.read_self_mood(event,scope) if scope else None
    if view is None:
        return None
    message = event.message_obj
    message_id = str(getattr(message, "message_id", "") or "")
    sources, offered = {}, {}
    raw = getattr(message, "raw_message", None)
    stamp = raw.get("time") if isinstance(raw, dict) else getattr(raw, "time", None)
    if type(stamp) not in (int, float):
        stamp = getattr(message, "timestamp", None)
    human = "" if event.get_extra("fishworld_wake_id") else human.strip()
    identity = "\0".join((event.unified_msg_origin, str(event.get_sender_id()), message_id))
    if message_id and human and not human.startswith("/") and type(stamp) in (int, float):
        sources["chat"] = (chat_source(event.unified_msg_origin, event.get_sender_id(), message_id), stamp, human)
        offered["chat"] = "当前说话人的原话"
    notifications = []
    verified_wake = (event.get_extra("fishworld_wake_id") and message_id.startswith("fishworld_")
                     and fish.follow_scope(event) and not event.get_extra("fishworld_manual_wake"))
    events = (event.get_extra("fishworld_mood_events", []) or []) if verified_wake else []
    for index, row in enumerate(events[:6], 1):
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["id"]) or type(row.get("received")) not in (int, float)
                or not isinstance(row.get("text"), str) or not row["text"].strip()):
            continue
        key = f"event{index}"
        sources[key] = (mark("world event", row["id"]), row["received"], row["text"])
        notifications.append(row["id"])
        offered[key] = row["text"][:70]
    details, available, evidence = await describe(plugin, event, view) if message_id else ([], {}, {})
    if message_id:
        turn = mark("wake events","/".join(sorted(notifications))) if notifications else mark("turn",identity)
        event.set_extra(TURN, {"scope": scope, "turn": turn, "sources": sources, "mid": message_id,
                               "available": available, "tone": view["tone"], "evidence": evidence,
                               "origin": event.unified_msg_origin, "offered_view": view})
    packet = {"听澜自己的心情": view["score"], "感受": details or view["tone"], "本轮来源": offered}
    if evidence:
        packet["往事引用类型"] = "memory"
    return packet


def record_action(event, name, args, text):
    context = event.get_extra(TURN)
    if (context and text and not text.lstrip().lower().startswith(("[e_", "error:"))
            and not re.search(r"没有执行|未执行|未发送|没有这个地点", text[:120])):
        request_id = args.get("request_id") or context["turn"]
        key = mark("action/"+name,str(request_id))
        context["sources"][name] = (key, time.time(), text[:1500])


def grounded_change(context, choice, legacy):
    if not isinstance(choice, dict):
        return None
    key, quote = choice.get("source"), choice.get("quote")
    source = context["sources"].get(key) if isinstance(key,str) else None
    if not source or not isinstance(quote, str) or not 2 <= len(quote.strip()) <= (100 if legacy else 64):
        return None
    quote = quote.strip()
    start = source[2].find(quote)
    if start < 0 or not quote.isprintable() or SENSITIVE.search(source[2]):
        return None
    quote = quote[:64]
    delta = choice.get("delta")
    if type(delta) is not int or (legacy and delta == 0 and choice.get("tone") == context["tone"]):
        return None
    if key == "chat":
        cause = {"kind": "chat", "mid": context["mid"], "start": start, "size": len(quote),
                 "digest": excerpt_digest(source[0], source[2], quote)}
    else:
        cause = {"kind": "event" if key.startswith("event") else key, "text": quote}
    resolves = choice.get("settles")
    return {"source": source[0], "at": source[1], "tone": choice.get("tone"), "delta": delta,
            "strength": choice.get("strength", max(1, abs(delta))), "span": choice.get("span", "中"), "cause": cause,
            "settles": context["available"].get(resolves) if isinstance(resolves, str) else None}


async def commit(plugin, event):
    context = event.get_extra(TURN)
    proposal = event.get_extra(PROPOSAL)
    if not context or not isinstance(proposal, dict):
        return
    legacy = "feelings" not in proposal
    choices = [proposal] if legacy else proposal["feelings"]
    if not isinstance(choices, list) or not 1 <= len(choices) <= 3:
        return
    try:
        changes = [change for item in choices if (change := grounded_change(context, item, legacy)) is not None]
        write = getattr(fish_instance(plugin), "update_feelings", None)
        if changes and write:
            await write(event, context["scope"], context["turn"], changes)
    except Exception as error:
        # Optional state should never replace an already valid QQ reply on write failure.
        logger.warning("Self mood update unavailable (%s)", type(error).__name__)
