"""Conversational wording without changing action outcomes or technical answers."""
import re

TECHNICAL=re.compile(r'\bAPI\b|HTTP|SDK|JSON|参数|错误码|日志|调试|源码|代码|插件|配置|密钥|数据库|(?:什么|哪个|哪些|什么叫).{0,5}工具|工具名|工具.{0,5}(?:返回|参数|调用)|接口.{0,8}(?:错误|失败|返回|怎么|如何|是什么)|(?:为什么|为啥|原因).{0,12}(?:失败|发不出|唱不了)',re.I)
PRESERVE=re.compile(r'翻译|逐字|原文|摘录|引用原话|复述原话|写.{0,8}(?:小说|故事|对白)|台词')
MARKER='<!-- BEGIN_JINGJING_SPOKEN -->'
END='<!-- END_JINGJING_SPOKEN -->'

RULES='''
【本轮表达】
日常只说眼前内容、实际进展和结果，不主动宣读工具、接口、后台、JSON、本轮或函数名；已获授权就实际执行，保留校验和真实结果。
失败、尚未开始、冷却、发送未确认要分清；“未确认”不能改成“没发出去”或成功。发出不代表收到、已读，不猜原因或承诺未安排的稍后行动。
按关系自然接话，不每轮反问、不固定收尾。可以有当下的开心、好奇和小得意，不编过去的情绪、现实身体活动或正在做的事。
对方说“嗯嗯、好的、晚安”、夸奖或分享结束时，简短接住就收住，不追加生活安排或催对方继续聊。没有求建议时少说先别刷手机、去洗脸、先打两把等指挥。
省略号和表情不证明对方叹气、失望或难过；不替人确定情绪。喵等玩笑可以轻松接梗，不急着强调自己不是猫。考试、游戏等话题不生硬绕回自己的手帐。
技术追问、原文、翻译和代码保留必要细节，普通聊天用熟人语气。认真问身份时如实说明AI，不额外展开内部机制。
'''

def technical_turn(human_text):
    return bool(TECHNICAL.search(str(human_text or '')) or PRESERVE.search(str(human_text or '')) or re.search(r'(?:怎么|为啥).{0,10}(?:失败|超时|没画|没出来)|出了什么错',str(human_text or '')))

def style_context(human_text):
    if technical_turn(human_text):
        return '\n对方这轮明确询问技术或原文，按问题如实说明必要细节，不为口语化删掉错误码、参数、代码或原话；解释要有证据。'
    return RULES

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
    chunks=re.split(r'(`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*")',text)
    for index in range(0,len(chunks),2):
        for pattern,replacement in replacements:chunks[index]=re.sub(pattern,replacement,chunks[index])
    return ''.join(chunks)
