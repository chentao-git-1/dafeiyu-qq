"""Keep ideas separate from unsupported work and future delivery claims."""
import re
from .interaction import QUOTED

ART_DELIVERY = re.compile(r'^(?:我|等我)?(?:画完|写完|做完|弄完|画好(?:了)?|写好(?:了)?|做好(?:了)?|完成后)(?:以后|之后|后)?(?:我|就|先|再|会|马上|直接)*给你(?:看|发|送|展示)')
WORKING = re.compile(r'^我(?:正(?:在)?|先|慢慢|这就|马上)(?:画|写|制作|生成)(?:着|一|这|那|个|张|幅|首|段|完|下|好|。|$)')
LATER = re.compile(r'^(?:我)?(?:一会儿?|等会儿?|过会儿?|稍后|晚点|到时候|到时|届时)(?:我|就|先|再|会|自动|主动|马上|一定)*.{0,10}(?:给你(?:发|看|送)|发给你|告诉你|通知你|联系你|来找你|再试|重试|重新画|重新生成)')
IDEA = re.compile(r'^(?:我)?(?:想|打算|希望|可以考虑)|^(?:等你|你.{0,12}(?:发来|画完|准备好))|^(?:如果|要是|假如)|^(?:我)?(?:不(?:会|再)|别|不要|不用)')


def ground_commitments(text, human, notification_ok=False):
    if '```' in text or re.search(r'翻译|逐字|原文|摘录|复述|台词|写.{0,8}(?:小说|故事|对白)|假如|假设', human):
        return text
    if re.search(r'在(?:我的|我想象的)?小屋故事里|在我想象的(?:小屋|故事)里', text):
        return text
    prefix = '\x00'
    while prefix in text:prefix += '\x00'
    quotes = []
    def protect(match):
        quotes.append(match[0]);return prefix + str(len(quotes)-1) + prefix
    masked = QUOTED.sub(protect, text)
    pieces = re.split(r'(?<=[，,。！？!\n])', masked)
    kept, removed, art = [], False, False
    for piece in pieces:
        clause = piece.strip()
        if IDEA.search(clause):
            kept.append(piece);continue
        artwork = bool(ART_DELIVERY.search(clause) or WORKING.search(clause))
        if artwork or not notification_ok and LATER.search(clause):
            removed = True;art = art or artwork
        else:
            kept.append(piece)
    if not removed:return text
    result = ''.join(kept).strip().rstrip('，,')
    for i, quote in enumerate(quotes):result = result.replace(prefix + str(i) + prefix, quote)
    if not result or re.fullmatch(r'(?:嗯|好|好的|好呀|那就这么定了)[。！!\s]*', result):
        return '这幅作品还只是个想法，还没实际开始。' if art else '后续还没有实际安排好。'
    return result
