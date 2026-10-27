"""Bounded generation intent and same-sender follow-up context."""

import re
import time
from collections import OrderedDict

IMAGE = r"自拍|头像|壁纸|插画|海报|封面|画像|照片|图片|图像|图画|卡通形象"
CANCEL = re.compile(
    r"(?:不要|别|不用|不需要|停止|取消|先不|暂时不|不想)"
    r"(?:(?:再|给我|帮我|继续|现在|马上|真的|实际|去|来|进行|任何|替我|先|重新)){0,3}"
    r"(?:画|绘|生图|生成|出图|做图|拍|照(?:一?(?:下|张)|相|了))|^(?:别|不要|不用|先不)(?:再)?(?:改|换|变|调整)|"
    r"\b(?:do not|don't|dont|stop|never)\s+(?:draw|paint|generate|create|make)\b",
    re.I,
)
TEXT_ONLY = re.compile(
    r"只(?:要|用|需要)?(?:文字|描述|说|讲)|文字版|用文字描述|只聊聊|\btext\s+only\b|only\s+describe",
    re.I,
)
DISCUSS = re.compile(
    r"(?:怎么|如何|教我).{0,14}(?:画|绘|生图|生成)|"
    r"(?:会不会|会|能不能|能|可以|支持).{0,8}(?:画画|画图|生图|生成图片)(?:吗|么|[？?])|"
    r"^你会画.{0,10}[吗么？?]|(?:画图|生图).{0,10}(?:收费|价格|多少钱|怎么用)|"
    r"(?:画图|生图|生成图片).{0,12}(?:太慢|很慢|好慢|失败|报错|有问题|怎么回事)|"
    r"识图|识别|看看.{0,6}(?:这|那)张|(?:这|那)张.{0,12}(?:什么|内容|好看)|"
    r"(?:我|他|她)(?:正在|在|刚才在)画|画画.{0,6}(?:难吗|很难|好难)|"
    r"(?:搜索|搜|找).{0,15}(?:图片|照片|壁纸|素材)|how\s+to\s+(?:draw|paint)|"
    r"(?:怎么|如何|教我).{0,14}(?:拍|照相|摄影)|"
    r"(?:会不会|会|能不能|能|可以|支持)(?:给人)?(?:拍照|照相|摄影)(?:吗|么|[？?])|"
    r"(?:拍照|照相|摄影).{0,10}(?:收费|价格|多少钱|怎么用)|"
    r"(?:打开|调用|使用).{0,5}(?:摄像头|相机)|实时(?:拍|照|画面)",
    re.I,
)
PHOTO_REQUEST = re.compile(
    r"^(?:(?:请|麻烦你|麻烦|能不能|可不可以|能否|可以|能|你|帮我|给我|替我|我想让你|想让你|还是|直接|再|也)\s*){0,5}"
    r"(?:(?:拍|照)(?:一(?:张|下)|张|下|个)|拍(?:个)?照片|拍照|照相|拍摄)",
    re.I,
)
NEW = re.compile(
    r"^(?:(?:请|麻烦你|麻烦|能不能|可以|能否|你|直接|还是|给我|帮我|替我|想让你|我想让你|我要你)\s*){0,4}"
    r"(?:画(?!风|质|面|作|家|册|的|得|重点|饼|等号|句号)|绘制|生图|出图|生成(?:图片|图像|一张图))|"
    r"(?:给我|帮我|替我|我想看|我想要|我要|想看|想要|来|发|做|弄|出|制作|设计|生成).{0,24}(?:"
    + IMAGE
    + r")|"
    r"(?:做|制作|生成|画|设计).{0,16}表情包|"
    r"(?:给我|来|发|做|整|弄|出).{0,20}图(?:吧|呀|啊|看看)?$|"
    r"(?:draw|paint|generate|create|make)\s+.{0,50}(?:picture|image|portrait|avatar|wallpaper|selfie)|"
    r"^draw\s+.+",
    re.I,
)
RESULT_REQUEST = re.compile(
    r"^(?:(?:请|麻烦你|麻烦|能不能|可以|能否|你|直接|还是|给我|帮我|替我|就)\s*){0,4}"
    r"(?:(?:把|将).{1,50}(?:画(?:出来|给我看)|绘制出来)|"
    r"(?:用|靠|以)(?:画图|绘画|绘图|图片|图像)(?:的?(?:形式|方式))?"
    r".{0,30}(?:画出来|画给我看|绘制出来|展示(?:出来|一下)?|呈现(?:出来|一下)?|表达出来))"
    r"(?:吧|呀|啊|好吗|可以吗|行吗)?[？?]?$"
)
RESULT_NEGATION = re.compile(
    r"^(?:请)?(?:(?:不要|别|不需要|先不|暂时不)(?:再)?|不(?=用))"
)
REVISION = re.compile(
    r"再来(?:一)?(?:张|幅|个)|再生成|重新生成|重画|再画|换(?:成|个|一种|一下|一张)|"
    r"改成|改为|变成|调整|沿用|还是上一张|(?:把|让).{0,25}(?:换|改|变)|"
    r"(?:亮|暗|可爱|清楚|清晰|大|小|近|远|自然)(?:一点|一些)|"
    r"^(?:都行|随便(?:你)?|你决定|你来定|你选|按你喜欢的|就这个|就这样)(?:吧|呀|啊)?$"
)
STYLE = re.compile(
    r"^(?:蓝色|红色|粉色|紫色|绿色|白色|黑色|动漫风|二次元|写实|水彩|油画|"
    r"横版|竖版|近景|远景|半身|全身|背景(?:要|是|用)|穿(?:蓝|红|白|黑|粉))"
)


def detect_intent(text, previous=False):
    """Recognize requests while keeping explanations and refusals out.

    Args:
        text: The actual current user's message, never a generated prompt.
        previous: Whether this same sender has an active drawing request.

    Returns:
        new, continue, cancel, or none. The last clear clause takes precedence.
    """
    decision = "none"
    for clause in re.findall(r"[^，。！？!?；;\n]+[！？!?]?", str(text or "")):
        clause = clause.strip()
        if not clause:
            continue
        negative_request = RESULT_NEGATION.sub("", clause, count=1)
        if (
            CANCEL.search(clause)
            or TEXT_ONLY.search(clause)
            or (negative_request != clause and RESULT_REQUEST.search(negative_request))
        ):
            decision = "cancel"
        elif re.search(
            r"怎么(?:还)?(?:不|没)画|(?:让|叫)你画|我要的是(?:图|图片)|我说的是生图",
            clause,
        ):
            decision = "new"
        elif DISCUSS.search(clause):
            continue
        elif (
            NEW.search(clause)
            or RESULT_REQUEST.search(clause)
            or PHOTO_REQUEST.search(clause)
        ):
            decision = "new"
        elif previous and (
            REVISION.search(clause)
            or (
                len(clause) <= 50
                and STYLE.search(clause)
                and not re.search(
                    r"好看|喜欢|不错|满意|谢谢|好棒|画得|什么|怎么|吗", clause
                )
            )
        ):
            decision = "continue"
    return decision


class IntentContext:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.records = OrderedDict()

    def get(self, key):
        record = self.records.get(key)
        if record and self.clock() - record["at"] < 600:
            return record["prompt"]
        self.records.pop(key, None)
        return ""

    def remember(self, key, prompt):
        self.records[key] = {"at": self.clock(), "prompt": str(prompt)[:1500]}
        self.records.move_to_end(key)
        while len(self.records) > 128:
            self.records.popitem(last=False)

    def observe(self, key, text):
        previous = self.get(key)
        mode = detect_intent(text, bool(previous))
        if mode in ("new", "continue"):
            self.remember(key, previous if mode == "continue" else text)
        else:
            # An unrelated turn or refusal ends the drawing continuation.
            self.records.pop(key, None)
        return {"mode": mode, "previous_prompt": previous if mode == "continue" else ""}


def continue_prompt(previous, change, organized):
    """Preserve the scene while applying a current explicit revision.

    Args:
        previous: The prior accepted scene description.
        change: The user's current modification request.
        organized: The model's complete description for the new generation.

    Returns:
        A bounded text prompt for a new generation, not a pixel-level image edit.
    """
    return (
        "延续上一张的主体与场景："
        + str(previous)[:650]
        + "\n本轮用户修改要求（与旧描述冲突时以此为准）："
        + str(change)[:300]
        + "\n本轮完整画面描述："
        + str(organized)[:450]
    )[:1500]
