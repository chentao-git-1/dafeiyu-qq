"""Recognize a recent bedtime sign-off in confirmed private replies."""
import json
import re


SLEEP_TOPIC = re.compile(r'晚安|早安|失眠|熬夜|补觉|困(?:了|吗|不困)|休息|歇(?:会儿|着|一会)|'
                        r'躺(?:下|会儿|一会)|睡(?!衣|裤|裙|袍|帽|袋|莲)')
GOODBYE = re.compile(r'再见|拜拜|回头聊|下次聊|明天聊|先不聊|下线了')
# Match closing clauses, keeping descriptions of sleep or other people's words out.
SIGN_OFF = re.compile(
    r'(?:(?:那就|那|先)?晚安(?:啦|了|哦|呀|吧)?|'
    r'(?:我|那我)?(?:这就|就|先|真|真的|准备|也)?(?:回去躺|去躺下|回去睡|去睡|睡觉去|睡)(?:下|觉|了|啦)?|'
    r'(?:你|您)?(?:也|就|要|记得|别忘了)?(?:早点|早些|早一点|赶紧|放心|快去|该)'
    r'(?:睡觉|睡|休息|歇着|躺下)(?:了|吧|啦|哦|呀)?|'
    r'(?:你|您)?(?:也|先|去)?(?:睡觉|睡|休息|歇着|躺下)(?:吧|啦|哦|呀)|'
    r'(?:看完|吃完|忙完|聊完)(?:了)?(?:就|也)?该睡(?:觉)?了)'
)
QUOTED = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"')


def recent_bedtime_closing(turn, human_text, items):
    if turn.is_group or not human_text or SLEEP_TOPIC.search(human_text) or GOODBYE.search(human_text):
        return False
    recent = (item for item in items if item.source == 'confirmed_exchange'
              and item.kind == 'exchange' and item.owner == turn.actor and item.origin == turn.origin
              and item.visible_to(turn) and 0 <= turn.observed_at - item.recorded_at <= 15 * 60)
    for item in sorted(recent, key=lambda row: row.recorded_at, reverse=True)[:3]:
        try:
            pair = json.loads(item.text)
        except (TypeError, ValueError):
            continue
        reply = pair.get('reply') if isinstance(pair, dict) else None
        if isinstance(reply, str):
            tail = re.split(r'[，,。！？!?；;\n…]+', QUOTED.sub('', reply))
            if any(SIGN_OFF.fullmatch(clause.strip()) for clause in tail if clause.strip()):
                return True
    return False
