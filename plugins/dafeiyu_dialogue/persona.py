"""Replace only a uniquely matched operator persona; retain its identity and tool policies."""
import re
import json
import copy


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


def semantic_persona(persona):
    if any(persona.count('<!-- BEGIN_'+name+' -->')!=1 for name in (
            'DAFEIYU_FAMILY_ROLE','DAFEIYU_MEMORY_SCOPE','JINGJING_OWN_LIFE')) or LEGACY_STYLE not in persona:
        return persona
    first=persona.find('<!-- BEGIN_')
    identity='\n'.join(persona[:first].splitlines()[:2])
    names=('DAFEIYU_FAMILY_ROLE','DAFEIYU_COMPANION','DAFEIYU_FRIEND_GATE')
    capabilities=[];tail=persona[first:]
    for name in names:
        pattern=r'<!-- BEGIN_'+name+r' -->(.*?)<!-- END_'+name+r' -->'
        found=re.search(pattern,tail,re.S)
        if found:capabilities.append({'scope':name,'rules':found[1].strip()});tail=re.sub(pattern,'',tail,flags=re.S)
    for name in ('DAFEIYU_MEMORY_SCOPE','JINGJING_NATURAL_COMPANY','JINGJING_OWN_LIFE','DAFEIYU_QZONE','JINGJING_OWN_SPACE_READ'):
        tail=re.sub(r'<!-- BEGIN_'+name+r' -->.*?<!-- END_'+name+r' -->','',tail,flags=re.S)
    core='''自然、亲近、有自己的判断，按眼前内容回应，不用采访或服务邀请撑场。表达反馈直接体现在下一句话里；关心跟随实际处境，不替人定情绪或动机。
你是AI，没有现实身体生活轨迹。数字阅读和作品要有真实记录，想法与创作可以自由发展；兴趣和角色外观不是已经做过事情的证明。没有工作记录可以谈当下想法，不编正忙着什么，也不向人报告后台核验。
当前明确更正优先，条件偏好按情境生效。普通人只改删本人资料，爸爸按原权限管理；共享事实保留归属，成对私聊片段不跨人，资料不自动公开。忘记记忆不等于删除QQ原记录。'''
    core+='\n排查时保留不确定性，不凭单个现象断定根因。操作缺关键信息就问必要问题，先确认安全范围内的步骤，不擅自破坏现有资料。'
    return identity+'\n'+core+'\n原操作员设定的关系、动作许可与工具规则仍有效；其中正文/配图格式只约束相应工具内容，不改变最终聊天的JSON协议：\n'+json.dumps(capabilities,ensure_ascii=False)+'\n'+tail.strip()


def attach_tool_contracts(req,known_persona):
    if not known_persona or semantic_persona(known_persona)==known_persona or req.func_tool is None:return
    tools=copy.copy(req.func_tool);tools.tools=[copy.copy(t) for t in req.func_tool.tools]
    scopes={'dafeiyu_qzone_publish':'DAFEIYU_QZONE','dafeiyu_qzone_draft':'DAFEIYU_QZONE',
        'dafeiyu_qzone_read_own':'JINGJING_OWN_SPACE_READ'}
    for tool in tools.tools:
        name=scopes.get(tool.name)
        if not name:continue
        found=re.search(r'<!-- BEGIN_'+name+r' -->(.*?)<!-- END_'+name+r' -->',known_persona,re.S)
        if found:
            marker='\n[DAFEIYU_OPERATOR_CONTRACT]\n'
            tool.description=str(tool.description or '').split(marker,1)[0]+marker+found[1].strip()
    req.func_tool=tools


def replace_persona(prompt, known_persona, contract=False):
    if not known_persona or prompt.count(known_persona) != 1:
        return prompt, False
    compact = semantic_persona(known_persona) if contract else compact_persona(known_persona)
    return prompt.replace(known_persona, compact, 1), compact != known_persona
