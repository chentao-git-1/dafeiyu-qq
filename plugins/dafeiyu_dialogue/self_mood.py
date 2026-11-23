"""Offer current-turn sources for the character's own mood. Quotes stay on the event."""
import hashlib
import re
import time

from astrbot.api import logger

TURN = "dafeiyu_self_mood_turn"
PROPOSAL = "dafeiyu_self_mood_proposal"


def mark(kind, value):
    return hashlib.sha256((kind + "\0" + value).encode()).hexdigest()[:24]


def fish_instance(plugin):
    meta = plugin.context.get_registered_star("astrbot_plugin_fishworld")
    return getattr(meta,"star_cls",None) if getattr(meta,"activated",False) else None


def prepare(plugin, event, human):
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
        sources["chat"] = (mark("chat", identity), stamp, human)
        offered["chat"] = "当前说话人的原话"
    notifications = []
    for index, row in enumerate(event.get_extra("fishworld_mood_events", []) or [], 1):
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["id"]) or type(row.get("received")) not in (int, float)
                or not isinstance(row.get("text"), str) or not row["text"].strip()):
            continue
        key = f"event{index}"
        sources[key] = (mark("world event", row["id"]), row["received"], row["text"])
        notifications.append(row["id"])
        offered[key] = row["text"][:70]
    if message_id:
        turn = mark("wake events","/".join(sorted(notifications))) if notifications else mark("turn",identity)
        event.set_extra(TURN,{"scope":scope,"turn":turn,"sources":sources})
    return {"听澜自己的心情":view["score"], "感受":view["tone"], "本轮来源":offered}


def record_action(event, name, args, text):
    context = event.get_extra(TURN)
    if context and text and not text.lstrip().lower().startswith(("[e_", "error:")):
        request_id = args.get("request_id") or context["turn"]
        key = mark("action/"+name,str(request_id))
        context["sources"][name] = (key, time.time(), text[:1500])


async def commit(plugin, event):
    context = event.get_extra(TURN)
    choice = event.get_extra(PROPOSAL)
    if not context or not isinstance(choice,dict):
        return
    key, quote = choice.get("source"), choice.get("quote")
    source = context["sources"].get(key) if isinstance(key,str) else None
    if not source or not isinstance(quote, str) or not 2 <= len(quote.strip()) <= 100 or quote.strip() not in source[2]:
        return
    try:
        write = getattr(fish_instance(plugin), "update_self_mood", None)
        if write:
            await write(event,context["scope"],context["turn"],source[0],source[1],choice.get("delta"),choice.get("tone"))
    except Exception as error:
        # Optional state should never replace an already valid QQ reply on write failure.
        logger.warning("Self mood update unavailable (%s)", type(error).__name__)
