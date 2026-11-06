"""Conversational wording without changing action outcomes or technical answers."""
import re
from .interaction import naturalness_feedback, QUOTED

TECHNICAL=re.compile(r'\bAPI\b|HTTP|SDK|JSON|\bunknown\b|回执|参数|错误码|日志|调试|源码|代码|插件|配置|密钥|数据库|(?:什么|哪个|哪些|什么叫).{0,5}工具|工具名|工具.{0,5}(?:返回|参数|调用)|接口.{0,8}(?:错误|失败|返回|怎么|如何|是什么)|(?:为什么|为啥|原因).{0,12}(?:失败|发不出|唱不了)',re.I)
PRESERVE=re.compile(r'翻译|逐字|原文|摘录|引用原话|复述原话|写.{0,8}(?:小说|故事|对白)|台词')
REQUEST=re.compile(r'[?？]|帮(?:我|忙)|请(?:你|帮)|能不能|可以吗|要不要|怎么|如何|为什么|为啥|什么|哪[里个种]|几[点时]|(?:提醒|喊我|叫我|定时|翻译|计算|解释|分析|建议|支招|出主意|评价|看看|看下|教我|给我|说一下|讲一下)|(?:发|画|唱|查|搜|算)(?:一|个|张|下|给|点|几|你|我|首|段|幅)|[吗么][呀啊呢。！!\s]*$')
SHARING=re.compile(r'输了|输掉|连败|赢了|赢下|打完了|下班了|下课了|考完了|忙完了|睡醒|醒了|起床了')
DIFFICULTY=re.compile(r'找不到|丢了|弄丢|忘带|打不开|进不去|出不来|登不上|连不上|卡住|卡在|看不懂|听不懂|弄不懂|搞不懂|不会(?:做|写|用|弄)|失败|出错|不舒服|难受|疼|受伤|被骗|着火')
DAILY_SHARING=re.compile(r'^(?:我(?:现在|刚才|刚|正|准备|在|去)?|刚|准备|在|去)?(?:食堂|买饭|吃饭|排队|回宿舍|到家|回家|走到|到了)[^?？]{0,30}$')
RHETORICAL=re.compile(r'这谁(?:顶得住|受得了)|谁说不是|(?:这|那)不就是|可不是(?:嘛|吗)|哪有这么|这也能|能不.{0,8}[吗嘛]')
BANTER=re.compile(r'(?:喵+|嗷+|哼+|略略略|嘿嘿+|哈{2,12})[。！!，,~～…\s]*$')
MARKER='<!-- BEGIN_JINGJING_SPOKEN -->'
END='<!-- END_JINGJING_SPOKEN -->'

RULES='''
【本轮表达】
像熟人发QQ消息，接眼前的新信息，选一个具体反应：一起吐槽、接半句梗、说自己的看法或认真回答。闲聊一两句就可以，话没必要每次说满；认真求助按需要讲清楚。
亲近感看真实关系。称呼、语气词和小玩笑顺着话题出现，有不一样的看法就直说一点具体理由。普通分享可以说完就停；想继续问时，要有确实想了解的具体内容，不靠采访、讲道理或服务邀请续话。
对方随口讲生活时，先接这句话，不顺手指导怎么吃饭、排队或安排时间；本人遇到困难或求建议时再帮忙分析。贴题的追问和接梗的反问可以保留。
按实际结果说进展，用日常说法表达“没开始、失败、还不能确认、已经发出”；保留这些区别。发送不等于收到或已读，未知结果不说成成功或失败；办事不编权限、原因或未安排的承诺。
可以顺着明确的画面和想象场景接玩笑，用虚拟拥抱等表达亲近；实际行动、现实见闻和共同往事要有依据。明确问身份如实说明AI，玩笑无需解释物种或运行原理。技术、代码和原文任务保留必要细节。
'''

def technical_turn(human_text):
    return bool(TECHNICAL.search(str(human_text or '')) or PRESERVE.search(str(human_text or '')) or re.search(r'(?:怎么|为啥).{0,10}(?:失败|超时|没画|没出来)|出了什么错',str(human_text or '')))

def conversational_mode(human_text):
    human=str(human_text or '').strip()
    if technical_turn(human):return 'technical'
    if naturalness_feedback(human,recent_reply=True):return 'feedback'
    if REQUEST.search(human) or DIFFICULTY.search(human):return 'request'
    if BANTER.fullmatch(human):return 'banter'
    if len(human)<=60 and (SHARING.search(human) or DAILY_SHARING.fullmatch(human)):return 'sharing'
    return 'chat'


def style_context(human_text, *, recent_assistant=''):
    mode=conversational_mode(human_text)
    if mode=='technical':
        return '\n对方这轮明确询问技术或原文，按问题如实说明必要细节，不为口语化删掉错误码、参数、代码或原话；解释要有证据。'
    if mode=='feedback':
        return RULES+'\n【这句在嫌表达生硬】接住刚才具体说得别扭的地方就够了，不写整改说明、不解释AI身份，也不再让对方重新点单或选择语气。已有实质话题就顺着它说，没有就简短收住。'
    if mode=='sharing':
        tail='上轮已经问过近况，这次接新信息后用陈述句收住。' if re.search(r'[?？]',recent_assistant) else '给一个贴题的反应后用陈述句收住。'
        return RULES+'\n【这句是在随口分享近况】单纯分享时不需要收集战况、考况或生活细节。'+tail+'可以轻轻接梗，有分寸地一起吐槽；不替人定义心情或夸成一种成长。若本人同时提出实际问题或任务，优先满足那个请求。'
    if mode=='banter':
        return RULES+'\n【这句是轻松玩笑】顺着节奏接一下就好，允许短句和自然停顿。不用解释为什么这样回应，不升温成关系承诺或现实动作，也不用另找一个问题。'
    return RULES

def close_idle_questions(text,human_text,had_tool=False):
    """Compatibility entry point; wording belongs to the primary model."""
    return text


def close_short_ack(text,human_text,recent_assistant=''):
    """Compatibility entry point; wording belongs to the primary model."""
    return text


def conversational_text(text,human_text):
    """Compatibility entry point; wording belongs to the primary model."""
    return text
