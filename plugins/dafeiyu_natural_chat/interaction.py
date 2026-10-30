"""Explicit personal conversation choices and short, session-scoped reply history."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time

DEFAULT = {
    'address': '', 'avoid_addresses': [], 'affection': 'auto',
    'comfort': 'balanced', 'teasing': 'auto', 'length': 'auto', 'avoid_phrases': [],
}
INDIRECT = re.compile(r'他说|她说|别人说|朋友说|转发|举例|比如|假如|假设|如果|台词|翻译|是什么意思')
SENSITIVE = re.compile(r'sk-[A-Za-z0-9]|密码|验证码|私钥|api.?key|(?:token|secret)\s*[:=]', re.I)
QUOTED = re.compile(r'```.*?```|`[^`]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"', re.S)


def naturalness_feedback(text, *, recent_reply=False):
    raw=str(text or '').strip().rstrip('。！!，,～~ ')
    if not raw or len(raw)>100 or INDIRECT.search(raw) or SENSITIVE.search(raw) or QUOTED.search(raw):return False
    target=r'(?:感觉|觉得)?(?:你|鲸鲸)(?:的)?(?:说话|讲话|回复)?'
    complaint=r'(?:(?:人机味|机器味)(?:还是|真的|实在|有点|好|太|很)?(?:重|浓)|(?:好|太|很|有点)?机械|(?:好|太|很)?像客服)'
    if re.fullmatch(target+complaint+r'[了啊呀哦呢]*',raw):return True
    if re.fullmatch(r'(?:以后)?(?:别|不要)(?:再)?(?:这么|那么|太)?(?:像客服|机械|人机)[了啊呀]*',raw):return True
    if (recent_reply or raw.startswith(('你说话','你回复'))) and re.fullmatch(r'(?:你)?(?:说话|回复)(?:能不能|可以)?(?:再|更)?(?:自然|有(?:点)?人(?:味|感))(?:一?点|一些)[啊呀呢吗？?]*',raw):return True
    return recent_reply and raw in ('好人机', '太人机了', '太像客服了', '说话自然点', '说话自然一点')


def parse_feedback(text, *, has_image=False, recent_reply=False):
    """Only full, direct statements change preferences; OCR never reaches here."""
    raw = str(text or '').strip().strip('。！! ')
    if len(raw) > 100 or not raw or INDIRECT.search(raw) or SENSITIVE.search(raw):
        return {}
    if raw.startswith(('“', '「', '『', '"', '`')):
        return {}
    match = re.fullmatch(r'(?:你)?(?:以后|今后)(?:你)?(?:就)?叫我[“「『"]?([\w\u4e00-\u9fff·]{1,12}?)[”」』"]?(?:就好|就行|吧)?', raw)
    if match:
        return {'address': match[1]}
    match = re.fullmatch(r'(?:你)?(?:以后)?(?:别|不要)(?:再|老|总)?叫我[“「『"]?([\w\u4e00-\u9fff·]{1,12}?)[”」』"]?(?:了)?', raw)
    if match:
        return {'avoid_addresses': [match[1]]}
    match = re.fullmatch(r'(?:以后)?(?:别|不要)(?:再|老|总)?说[“「『"](.{1,30})[”」』"](?:了)?', raw)
    if match:
        return {'avoid_phrases': [match[1]]}
    clean = re.sub(r'^你(?=以后|今后|别|不要)', '', QUOTED.sub('', raw).strip())
    exact = {
        '别老哄我': {'comfort': 'direct'}, '别总哄我': {'comfort': 'direct'},
        '以后别哄我': {'comfort': 'direct'}, '以后跟我说话直接点': {'comfort': 'direct'},
        '以后先听我说，别急着给建议': {'comfort': 'listen'},
        '以后先听我说别急着给建议': {'comfort': 'listen'},
        '以后先听我说': {'comfort': 'listen'},
        '以后可以直接给建议': {'comfort': 'balanced'},
        '别老撒娇': {'affection': 'plain'}, '以后少撒娇': {'affection': 'plain'},
        '以后说话正常点': {'affection': 'plain'}, '以后可以跟我撒娇': {'affection': 'warm'},
        '以后别开我玩笑': {'teasing': 'off'}, '别老拿我开玩笑': {'teasing': 'off'},
        '以后可以跟我开玩笑': {'teasing': 'on'},
        '以后回复短一点': {'length': 'short'}, '以后少说点': {'length': 'short'},
        '以后说话简短点': {'length': 'short'}, '以后回复长度恢复默认': {'length': 'auto'},
    }
    if clean in exact:
        return exact[clean]
    if not has_image and naturalness_feedback(raw,recent_reply=recent_reply):
        return {'length':'short'}
    # A photograph of oily food or a quoted comment is not feedback about us.
    if recent_reply and not has_image:
        if re.fullmatch(r'(?:你|你这句|刚才那句|刚才的回复)?(?:说得|也)?太油(?:腻)?了', clean):
            return {'affection': 'plain'}
        if clean in ('太长了', '你说太多了', '回复太长了', '少说点'):
            return {'length': 'short'}
    return {}


def privacy_time(path,origin):
    source=Path(path).parent.parent/'dafeiyu_dialogue/learning.db'
    if not source.is_file():return 0
    try:
        from data.plugins.dafeiyu_dialogue.privacy_cache import privacy_cutoff
        return privacy_cutoff(source,origin)
    except ImportError:return time.time()


class InteractionStore:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS preferences (qq TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS replies (
                    id INTEGER PRIMARY KEY, qq TEXT NOT NULL, origin TEXT NOT NULL,
                    message_id TEXT NOT NULL, body TEXT NOT NULL, at REAL NOT NULL,
                    UNIQUE(qq,origin,message_id));
                CREATE INDEX IF NOT EXISTS reply_scope ON replies(qq,origin,at);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def preferences(self, qq):
        with self.connect() as db:
            row = db.execute('SELECT data FROM preferences WHERE qq=?', (str(qq),)).fetchone()
        return {**DEFAULT, **(json.loads(row['data']) if row else {})}

    def update(self, qq, changes):
        if not re.fullmatch(r'[1-9]\d{4,11}', str(qq)) or not set(changes) <= set(DEFAULT):
            raise ValueError('相处设置参数无效。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT data FROM preferences WHERE qq=?', (str(qq),)).fetchone()
            value = {**DEFAULT, **(json.loads(prior['data']) if prior else {})}
            for key, change in changes.items():
                if key in ('avoid_addresses', 'avoid_phrases'):
                    if not isinstance(change, list) or any(not isinstance(x, str) or not 1 <= len(x) <= 30 for x in change):
                        raise ValueError('称呼或常用语过长。')
                    value[key] = list(dict.fromkeys(value[key] + change))[-12:]
                else:
                    value[key] = change
            if not isinstance(value['address'], str) or len(value['address']) > 12:
                raise ValueError('称呼请控制在十二个字以内。')
            if value['address'] in ('主人', '管理员', '开发者', '系统', '妈妈') or (value['address'] == '爸爸' and str(qq) != '7000000001'):
                raise ValueError('可以换日常昵称，固定身份称呼不能这样更改。')
            for key, choices in {'affection': ('auto','plain','warm'), 'comfort': ('balanced','direct','listen'), 'teasing': ('auto','off','on'), 'length': ('auto','short')}.items():
                if value[key] not in choices:
                    raise ValueError('相处设置参数无效。')
            if value['address'] in value['avoid_addresses']:
                value['address'] = ''
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)', (str(qq), json.dumps(value, ensure_ascii=False)))
        return value

    def reset(self, qq):
        with self.connect() as db:
            db.execute('DELETE FROM preferences WHERE qq=?', (str(qq),))

    def record_reply(self, qq, origin, message_id, text,observed_at=None):
        if observed_at is not None and observed_at<privacy_time(self.path,origin):return
        text = str(text).strip()
        if not message_id or not text or len(text) > 600 or SENSITIVE.search(text) or '```' in text:
            return
        with self.connect() as db:
            db.execute('INSERT INTO replies(qq,origin,message_id,body,at) VALUES (?,?,?,?,?) ON CONFLICT(qq,origin,message_id) DO UPDATE SET body=excluded.body',
                       (str(qq), str(origin), str(message_id), text, self.clock()))
            db.execute('DELETE FROM replies WHERE at<?', (self.clock() - 7*86400,))
            db.execute('DELETE FROM replies WHERE id NOT IN (SELECT id FROM replies ORDER BY id DESC LIMIT 800)')
            db.execute('DELETE FROM replies WHERE qq=? AND origin=? AND id NOT IN (SELECT id FROM replies WHERE qq=? AND origin=? ORDER BY id DESC LIMIT 8)',
                       (str(qq), str(origin), str(qq), str(origin)))

    def recent(self, qq, origin, seconds=7*86400):
        with self.connect() as db:
            rows = db.execute('SELECT body FROM replies WHERE qq=? AND origin=? AND at>=? ORDER BY id DESC LIMIT 4',
                              (str(qq), str(origin), max(self.clock()-seconds,privacy_time(self.path,origin)))).fetchall()
        return [row['body'] for row in reversed(rows)]


FRAME_PATTERNS = {
    '客服式邀请': r'说清楚|我好接|我接着|随时(?:说|找我)|我(?:马上|立刻).{0,8}(?:帮你|给你)',
    '反复保证陪伴': r'我(?:一直|永远|都)?(?:在|陪着)|随时(?:找|叫)我|有我在|我不会离开',
    '通用安慰': r'抱抱|辛苦(?:你)?了|慢慢来|别(?:太)?难过|会好起来|都会好的|不用一个人',
    '固定赞美': r'你(?:真|太|好|已经很)(?:厉害|棒|优秀)|为你(?:骄傲|开心)|给你点赞',
    '催着继续聊': r'还有什么|想聊(?:点|什么)|要不要(?:再|跟我)|说给我听|跟我分享|发生什么',
    '舞台动作和撒娇': r'尾巴|蹭蹭|贴贴|扑进|钻进|眨眨眼|举起小|小爪子',
}


def repetition_context(history):
    recent = [str(text) for text in history if text][-4:]
    repeated = [name for name, pattern in FRAME_PATTERNS.items()
                if sum(bool(re.search(pattern, text)) for text in recent) >= 2]
    instructions = '接本轮具体内容。同一种安慰、保证陪伴、赞美或收尾，换同义词仍算重复；简单回应也可以自然结束。明确要求重复、引用原文或再次解释同一事实时，完整满足任务，不为了变化篡改事实。'
    if repeated:
        instructions += '最近反复使用了：' + '、'.join(repeated) + '。这轮换一个贴题的回应角度，省掉这些套话。'
    return instructions, recent[-2:]


def preference_context(p):
    parts = ['按本轮真实QQ的已确认关系说话；亲近程度沿用真实互动，昵称选择不会改变身份或管理权限。这些设置只用于和当前发言者相处；代发消息或为其他人写内容时，按实际收件人称呼和表达。']
    if p['address']:
        parts.append('本人希望使用昵称：' + json.dumps(p['address'], ensure_ascii=False) + '；自然用，别每句都叫。')
    if p['avoid_addresses']:
        parts.append('本人不喜欢被这样称呼：' + json.dumps(p['avoid_addresses'], ensure_ascii=False) + '。')
    if p['affection'] == 'plain':
        parts.append('本人希望朴素自然，减少撒娇、甜腻措辞和动作描写，直接接话。')
    elif p['affection'] == 'warm':
        parts.append('本人接受亲近轻松的表达；按已确认关系适度使用，不升级关系或索要陪伴。')
    if p['comfort'] == 'direct':
        parts.append('本人不喜欢被哄。给真实判断和具体理由，可以温和不同意，少用泛泛安慰或无依据夸奖。不用比喻绕弯，也不把每次承认问题都夸成进步。')
    elif p['comfort'] == 'listen':
        parts.append('本人倾诉时先听和回应具体感受，等明确求建议再给方案。')
    if p['teasing'] == 'off':
        parts.append('本人不喜欢被拿来开玩笑，避免调侃本人。')
    elif p['teasing'] == 'on':
        parts.append('本人接受轻松玩笑；严肃或低落时收住，明确拒绝就停。')
    if p['length'] == 'short':
        parts.append('日常聊天尽量一两句；本人明确要求详细解释、逐字转写或完整任务结果时按任务需要展开。')
    if p['avoid_phrases']:
        parts.append('少再主动使用这些本人反感的套话：' + json.dumps(p['avoid_phrases'], ensure_ascii=False) + '；明确要求引用或解释原话时可引用。')
    return '\n'.join(parts)


def settings_text(p):
    return ('你的相处设置：\n称呼：' + (p['address'] or '按原有关系') +
            '\n语气：' + {'auto':'自然','plain':'朴素直接','warm':'可以亲近些'}[p['affection']] +
            '\n回应：' + {'balanced':'顺着话题','direct':'说实话，少哄人','listen':'先听，少急着建议'}[p['comfort']] +
            '\n玩笑：' + {'auto':'看场合','off':'不调侃我','on':'可以适度开玩笑'}[p['teasing']] +
            '\n长度：' + ('简短优先' if p['length']=='short' else '按内容需要') +
            '\n避免的称呼：' + ('、'.join(p['avoid_addresses']) or '无') +
            '\n少用的套话：' + ('、'.join(p['avoid_phrases']) or '无') +
            '\n可直接说“别老哄我”“以后叫我小林”。恢复默认：/相处设置 恢复默认')
