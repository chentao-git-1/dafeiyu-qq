"""Local checks for the evening diary; no private conversation input."""
import datetime as dt
import re

TZ = dt.timezone(dt.timedelta(hours=8))


class DiaryCancelled(ValueError):
    pass


class ImagePreparationFailed(RuntimeError):
    """The post write has not started, so one text-only fallback is safe."""


def window(day):
    date = dt.date.fromisoformat(day) if isinstance(day, str) else day
    return (dt.datetime.combine(date, dt.time(21, 30), TZ),
            dt.datetime.combine(date, dt.time(22, 30), TZ))


def diary_reason(text, plan):
    if not 10 <= len(text.strip()) <= 160:
        return '日记须为160字以内的短文；素材少时允许简短想法'
    if re.search(r'\d{5,}|https?://|[@＠]|QQ|微信|群名|昵称|真实姓名|密码|密钥|账户|余额|爸爸|叔叔|阿姨|宝贝|[“”「」『』"`]', text, re.I):
        return '不公开身份、原话、凭据或私人关系线索'
    if re.search(r'(?:我|鲸鲸)(?:今天|刚才|刚刚)?(?:出门|出差|逛街|上课|吃了|喝了|见了|遇见|和朋友一起|去医院)|手绘|亲手画', text):
        return '不编造身体活动，也不把别人的经历写成自己的'
    if any(word in text for word in ('小屋', '小芽', '浇花')) and not re.search(r'想象|故事|虚构', text):
        return '小屋与小芽须明确属于想象或故事'
    life = plan.get('own_life', {})
    for kind, pattern in (('reading', r'读完|读了|阅读了'), ('writing', r'写完|写了|创作了.*(?:文字|散文)'), ('drawing', r'画完|画了|生成了.*插画')):
        if re.search(pattern, text) and life.get('kind') != kind:
            return '不能把未核验的阅读或创作写成今天已完成'
    return ''
