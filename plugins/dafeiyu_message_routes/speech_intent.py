"""Recognize explicit output requests in the current user's own text."""
import re

QUOTED = re.compile(r'```.*?```|`[^`]*`|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"]*"', re.S)
SINGING = re.compile(r'唱歌|唱出来|唱一[首段]|/sing\b|/唱歌', re.I)
VOICE = re.compile(
    r'(?:发|发送|来|整|给|回|回复|用|改用|换成|转成|转为|以).{0,8}语音'
    r'|语音.{0,4}(?:回复|回答|说|讲|念|读|介绍|解释|告诉)'
    r'|用.{0,4}声音.{0,4}(?:说|讲|回复|回答)'
    r'|(?:读|念|说)给我听|(?:说|讲)(?:一|两|几)?句(?:话)?|开口说话'
    r'|^语音(?:吧|呗|呀)?$'
)
TEXT = re.compile(
    r'(?:不要|别|不用|不必|无需|禁止|停止|取消|不需要|不想听|不喜欢).{0,8}(?:语音|说话|朗读)'
    r'|(?:只|就|直接|改用|换成|用)(?:发|发送)?(?:文字|文本|打字)'
    r'|语音.{0,4}(?:关掉|关闭|停用)'
)
COMMENT = re.compile(r'(?:为什么|怎么|为何|老是|总是).{0,12}(?:发|发送).{0,6}语音')


def output_intent(text):
    """Return voice, text, or None; later clauses override earlier ones."""
    clean = QUOTED.sub('', text or '')
    if SINGING.search(clean):
        return None
    intent = None
    for clause in re.split(r'[，,。.!！?？;；\n]', clean):
        clause = re.sub(r'\s+', '', clause)
        if TEXT.search(clause):
            intent = 'text'
        elif VOICE.search(clause) and not COMMENT.search(clause):
            intent = 'voice'
    return intent
