"""Use private delivered replies as evidence of what was said, not of a shop transaction."""
from datetime import datetime
import json
import re

from dafeiyu.dialogue.retrieval import terms
from .world_observation import SHANGHAI


QUOTE = "dafeiyu_spoken_price"
PAST = re.compile(r"之前|以前|上次|上回|刚才|先前|不是.{0,6}(?:问|说|报)|(?:问|说|报)过")
PRICE = re.compile(r"问|报|价|多少|几贝|几块")
AMOUNT = re.compile(r"\d+(?:\.\d+)?\s*(?:贝|元|块)")
FILLER = re.compile(r"之前|以前|上次|上回|刚才|先前|不是|是不是|当时|已经|记不记得|还记得|"
                    r"多少钱|价格|报价|多少|你|我|还|不|就|又|问|说|报|了|过|的|是|吗|么|呀|呢|啊|[^\w\u3400-\u9fff]")
NO_TOPIC = terms("问过价了 当时那次 多少钱 报过价了 当时说的 店里问的 你之前不是问了吗 买东西了")


def prior_price(human, items, turn):
    if turn.is_group or not isinstance(human,str) or not (PAST.search(human) and PRICE.search(human)):
        return None
    if not re.search(r"吗|嘛|呢|[?？]|多少|几贝|记得|不是",human):
        return None
    eligible = sorted((r for r in items if r.source == "confirmed_exchange" and r.owner == turn.actor
                       and r.origin == turn.origin and r.visible_to(turn) and r.recorded_at <= turn.observed_at),
                      key=lambda r:r.recorded_at, reverse=True)
    rows = []
    for item in eligible:
        try:
            reply = json.loads(item.text)["reply"]
        except (KeyError, ValueError, TypeError):
            continue
        if isinstance(reply,str):
            rows.append((item,reply))
    explicit = terms(FILLER.sub("",human)) - NO_TOPIC
    recent = " ".join(reply for item,reply in rows[:3] if turn.observed_at-item.recorded_at <= 1800)
    keys = explicit or (terms(recent) - NO_TOPIC)
    if not keys:
        return None
    candidates = []
    for item, reply in rows:
        for chunk in re.split(r"[，,。；;：:\n！？!?]",reply):
            chunk = chunk.strip()
            match = AMOUNT.search(chunk)
            if not match or len(chunk)>75 or re.search(r"钱包|余额|补贴|还剩|现在有|猜|大概|估计|可能|不确定",chunk):
                continue
            subject = chunk[:match.start()].strip()
            overlap = (terms(subject) - NO_TOPIC) & keys
            if overlap:
                candidates.append((len(overlap),item.recorded_at,item,chunk[:match.end()],match[0]))
    if not candidates:
        return None
    _,_,item,quote,amount = max(candidates,key=lambda x:(x[0],x[1]))
    return item, {"id":item.id, "at":datetime.fromtimestamp(item.recorded_at,SHANGHAI).strftime("%m-%d %H:%M"),
                  "quote":quote,"amount":amount}


def quote_wording(text, quote, final):
    if not quote or not isinstance(text,str) or not text:
        return text
    normalized = re.sub(r"\s+","",text)
    has_amount = re.sub(r"\s+","",quote["amount"]) in normalized
    contradiction = re.search(r"没(?:有)?(?:真|真的|实际|跟你|和你|给你)?(?:问|报|说)(?:过|到)(?:价|价格|多少|这)",text)
    attributed_to_shop = re.search(r"(?:师傅|老板|店员).{0,8}(?:亲口|告诉我|报的价|说是|确认)",text)
    claimed_inquiry = re.search(r"(?:我|对|嗯|确实|已经|当时|之前|是的)?[，, ]*(?:问过了|问过价|问到价|我问过)",text)
    reported = re.search(r"跟你说|和你说|对你说|给你报|报给你|告诉过?你|说过|报过|我当时说|我之前说|那条消息",text)
    if not contradiction and not attributed_to_shop and not claimed_inquiry and (has_amount and reported or not final):
        return text
    return "我之前跟你说过“" + quote["quote"] + "”。"
