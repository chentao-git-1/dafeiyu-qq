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
    """Keep a casual reaction instead of adding an unrelated interview."""
    if had_tool or '```' in text or conversational_mode(human_text) not in ('sharing','banter','feedback'):return text
    prefix='\x00'
    while prefix in text:prefix+='\x00'
    quotes=[]
    def protect(match):
        quotes.append(match[0]);return prefix+str(len(quotes)-1)+prefix
    masked=QUOTED.sub(protect,text)
    pieces=[part for part in re.split(r'(?<=[。！!?？\n])',masked) if part.strip()]
    statements=[part for part in pieces if not re.search(r'[?？]',part) or RHETORICAL.search(part)]
    if not statements or len(statements)==len(pieces):return text
    result=''.join(statements).strip()
    for index,quote in enumerate(quotes):result=result.replace(prefix+str(index)+prefix,quote)
    return result


def close_short_ack(text,human_text,recent_assistant=''):
    """After repeated questions, a bare wake-up update needs no third interview."""
    if len(re.findall(r'[?？]',recent_assistant))<2:return text
    if not re.fullmatch(r'(?:我)?(?:醒了|睡醒了|起了|起床了)[呀啊啦哦呢。！!，,\s]*',str(human_text or '').strip()):return text
    if not re.search(r'[?？]',text) or technical_turn(human_text) or '```' in text:return text
    statements=[part for part in re.split(r'(?<=[。！!?？\n])',text) if part.strip() and not re.search(r'[?？]',part)]
    if statements:return ''.join(statements).strip()
    greeting=re.match(r'^(?:嗯[，, ]*)?((?:醒(?:了|啦|咯)|睡醒了|起床了)[呀啊哦呢啦]*)[，,。！!\s]',text)
    return greeting[1]+'。' if greeting else '醒啦。'


def conversational_text(text,human_text):
    """Remove known execution narration, preserving result polarity and content."""
    if technical_turn(human_text) or '```' in text:return text
    replacements=(
        (r'(?:啊[，,]\s*)?(?:这次|刚才)没唱成[，,]\s*工具那边(?:没给过|没通过)', '刚才还没开始唱'),
        (r'工具说当前没有明确指令[，,]\s*就没生成', '刚才没接准你的意思，还没开始唱'),
        (r'需要我用发消息的工具再给([^。！？?\n]{1,16})送一遍吗', r'要再给\1发一遍吗'),
        (r'(?:发送工具|工具|接口)(?:回的是|返回的是|返回|显示|说)(?:“|「|\")?发送成功(?:”|」|\")?', '已经发过去了'),
        (r'(?:发送工具|工具|接口)(?:回的是|返回的是|返回|显示|说)(?:“|「|\")?(?:发送结果不明|发送未确认)(?:”|」|\")?', '还不能确定有没有发出去'),
        (r'(?:工具调用|工具执行|接口调用)(?:失败|出错)了?', '这次没弄好'),
        (r'(?:唱歌接口|唱歌服务)(?:暂时失败|暂时不可用|暂时用不了)(?:（HTTP \d{3}）)?', '这次没唱出来'),
        (r'这次唱歌生成超时了', '这次没能及时唱出来'),
        (r'画图接口暂时失败（HTTP (?:408|504)），没有自动重试[。；;]?', '这次画图请求超时了，还没拿到图片结果。'),
        (r'画图接口暂时失败（HTTP \d{3}），没有自动重试[。；;]?', '这次画图没完成，暂时还没拿到图片。'),
        (r'MiMo 唱歌密钥无效或没有模型权限，请管理员检查', '这次没唱出来，得先检查一下唱歌的设置'),
        (r'我(?:先|现在)?(?:调用|使用)(?:一下)?(?:工具)?(?:来)?(?:核实|查询)(?:一下)?(?:记录)?', '我看看刚才的记录'),
        (r'本轮(?:已经|已)确认', '已经确认'),
        (r'这个会话还没有开放唱歌权限', '这边暂时还不能唱歌'),
        (r'唱歌服务还没有完成启用配置', '这边暂时还不能唱歌'),
    )
    if re.search(r'发送|发给|发过去|发出去|收到|送达|私信|消息|发了',str(human_text or '')+text):
        replacements=(
            (r'((?:还|暂时)?(?:不|没|无法)[^，,。！？\n]{0,12}(?:确定|确认)[^，,。！？\n]{0,12})[，,]\s*(?:发送)?回执(?:是|为)\s*unknown\b',r'\1'),
            (r'(?:发送)?回执(?:是|为)\s*unknown\b','还不能确定有没有发出去'),
        )+replacements
    chunks=re.split(r'(`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*")',text)
    for index in range(0,len(chunks),2):
        for pattern,replacement in replacements:chunks[index]=re.sub(pattern,replacement,chunks[index])
    return ''.join(chunks)
