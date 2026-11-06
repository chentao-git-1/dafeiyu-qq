"""Keep ideas separate from unsupported work and future delivery claims."""
import re
from .interaction import QUOTED

ART_DELIVERY = re.compile(r'^(?:我|等我)?(?:画完|写完|做完|弄完|画好(?:了)?|写好(?:了)?|做好(?:了)?|完成后)(?:以后|之后|后)?(?:我|就|先|再|会|马上|直接)*给你(?:看|发|送|展示)')
WORKING = re.compile(r'^我(?:正(?:在)?|先|慢慢|这就|马上)(?:画|写|制作|生成)(?:着|一|这|那|个|张|幅|首|段|完|下|好|。|$)')
LATER = re.compile(r'^(?:我)?(?:一会儿?|等会儿?|过会儿?|稍后|晚点|到时候|到时|届时)(?:我|就|先|再|会|自动|主动|马上|一定)*.{0,10}(?:给你(?:发|看|送)|发给你|告诉你|通知你|联系你|来找你|再试|重试|重新画|重新生成)')
IDEA = re.compile(r'^(?:我)?(?:想|打算|希望|可以考虑)|^(?:等你|你.{0,12}(?:发来|画完|准备好))|^(?:如果|要是|假如)|^(?:我)?(?:不(?:会|再)|别|不要|不用)')


def ground_commitments(text, human, notification_ok=False):
    """Compatibility entry point; wording belongs to the primary model."""
    return text
