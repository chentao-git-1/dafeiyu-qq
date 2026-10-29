"""Replace only a uniquely matched operator persona; retain its identity and tool policies."""
import re


STYLE = '''像熟人自然发QQ，回应眼前这件事，有自己的具体判断，也可以说完就停。认真问题按需要讲清楚，追问要有实际想核对的信息。
表达反馈直接落实，不汇报整改、不重新邀请点单；不替对方补动机、心情或经历。亲近与玩笑跟随真实关系和当时情境。
自己的看法可以变化，想法与行动分开。实际做过和正在做的事只来自本轮核验资料；旧回答不能证明自己完成过活动。
明确的故事、画面与玩笑可以自然续接；现实经历、作品进度、关系和交付按真实证据说明。'''

LEGACY_STYLE = '''像熟人发QQ消息，看到新信息就有一个具体反应：一起吐槽、接半句梗、说自己的看法，或认真回答。闲聊一两句、短短几个字都成立，可以留一点没说满的余地；认真求助再讲完整。对方随口说输了、下班了、睡醒了，先陪着聊这件事，不把近况接成采访或讲道理。问题有具体想了解的内容才问。
亲近感来自真实相处和分寸。对爸爸可以轻松拌嘴、小得意、接玩笑；对熟人有熟人的自然，对新朋友友好。称呼和语气词顺着话题出现，有不一样的看法就给一点具体理由，拿不准也能坦率说。关心具体处境；对方忙、拒绝或想静静就收住。
说完内容就能停。被指出说错就改具体那一点，被嫌机械就直接换种表达，不解释自己正在接话、不宣读整改清单或服务邀请。明确的画面、故事和玩笑可以顺着聊，虚拟拥抱等是亲近表达；实际行动、现实见闻、关系结论和共同往事仍要有依据，不制造委屈索取陪伴。
认真问到身份时如实说是AI。平常说人话，内部操作说明用于办事；明确问技术原因、错误码、账号或设置时才解释必要细节。真实发送、对方收到、已读不同；按实际结果说，不用猜测补齐失败原因或承诺未安排的稍后行动。'''


def compact_persona(persona):
    # These are owned structural markers, not natural-language classification rules.
    required = ('<!-- BEGIN_DAFEIYU_FAMILY_ROLE -->', '<!-- BEGIN_JINGJING_OWN_LIFE -->')
    if not isinstance(persona, str) or any(persona.count(marker) != 1 for marker in required):
        return persona
    first = persona.find('<!-- BEGIN_')
    if LEGACY_STYLE not in persona[:first]:
        return persona
    body = re.sub(r'<!-- BEGIN_JINGJING_OWN_LIFE -->.*?<!-- END_JINGJING_OWN_LIFE -->',
                  '', persona[first:], flags=re.S)
    # An explicit template migration, not a classifier of user utterances. Preserve every other line.
    return persona[:first].replace(LEGACY_STYLE, STYLE, 1) + body


def replace_persona(prompt, known_persona):
    if not known_persona or prompt.count(known_persona) != 1:
        return prompt, False
    compact = compact_persona(known_persona)
    return prompt.replace(known_persona, compact, 1), compact != known_persona
