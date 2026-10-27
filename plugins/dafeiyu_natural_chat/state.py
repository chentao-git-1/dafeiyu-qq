"""Bounded, attributed episodes. Conversation mood is deliberately ephemeral."""
import datetime as dt
import hashlib
import json
import re
import sqlite3
import time

OWNER = '7000000001'
FAMILY = {OWNER: '爸爸', '7000000011': '猛叔叔', '7000000012': '田阿姨', '7000000013': '定叔叔'}
TZ = dt.timezone(dt.timedelta(hours=8))
NO_RECORD = r'不要.{0,6}记|不用.{0,6}记|别.{0,6}记|不许.{0,6}记|不想.{0,6}记|不记录|不保存|不要存|别保存|别存'
INDIRECT = r'假如|假设|如果|比如|举例|小说|剧本|转发|引用|他说|她说|他们说|朋友说|我朋友|我同学|我同事|我爸|我妈|(?:田阿姨|猛叔叔|定叔叔|爸爸).{0,4}说'
TOPICS = ('考试', '面试', '答辩', '比赛', '演出', '出差', '搬家', '旅行', '开学', '复查', '体检', '汇报', '项目', '作业')
DONE = r'考完|考过|考砸|面试完|面试结束|答辩过|结束了|做完|交完|通过了|没通过|没过|取消了|成绩出来|结果出来|已经完成|已经结束'


def key(text):
    return re.sub(r'[\s，。！？、,.!?：:；;「」“”]', '', str(text)).lower()


def forbidden(text):
    # “别忘记” is a request to remember, with the opposite meaning of “别记”.
    text = re.sub(r'(?:千万别|不要|别)(?:再)?忘记?', '', text)
    return bool(re.search(NO_RECORD, text))


def direct(text):
    return bool(re.search(r'我|咱们|我们', text)) and not re.search(INDIRECT, text)


def mood_cue(text, previous=None, now=None):
    now = time.time() if now is None else now
    # Quotes, hypotheses and third-person reports must not become this speaker's mood.
    if re.search(INDIRECT, text) or re.search(r'[?？]|吗|不难过|没有难过|并不累|不觉得累', text):
        return {'style': 'neutral', 'at': now, 'remaining': 0}
    if re.search(r'好多了|没事了|开心起来|缓过来|不难过了|不累了|已经好了', text):
        return {'style': 'relieved', 'at': now, 'remaining': 2}
    if re.search(r'我.{0,10}(?:难过|难受|不开心|心情不好|好累|压力大|失落)|^(?:好累|好难过|心情不好)', text):
        return {'style': 'gentle', 'at': now, 'remaining': 3}
    if re.search(r'我.{0,10}(?:好开心|很开心|太开心)|^(?:好耶|太好了)', text):
        return {'style': 'bright', 'at': now, 'remaining': 2}
    if previous and now - previous['at'] < 1800 and previous['remaining'] > 0:
        return dict(previous, remaining=previous['remaining'] - 1)
    return {'style': 'neutral', 'at': now, 'remaining': 0}


class Episodes:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS episodes(
                id INTEGER PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                topic TEXT NOT NULL, phase TEXT NOT NULL, detail TEXT NOT NULL,
                evidence TEXT NOT NULL, origin TEXT NOT NULL, message_id TEXT NOT NULL,
                event_day TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                history TEXT NOT NULL, digest TEXT UNIQUE NOT NULL)''')
            c.execute('CREATE INDEX IF NOT EXISTS episode_owner_updated ON episodes(owner,updated)')
        path.chmod(0o600)

    def connect(self):
        c = sqlite3.connect(str(self.path), timeout=3)
        c.row_factory = sqlite3.Row
        return c

    def prune(self):
        now = time.time()
        with self.connect() as c:
            c.execute("DELETE FROM episodes WHERE kind='event' AND updated<?", (now - 180*86400,))
            c.execute("DELETE FROM episodes WHERE kind='event' AND phase IN ('resolved','cancelled') AND updated<?", (now - 90*86400,))
            c.execute('DELETE FROM episodes WHERE id NOT IN (SELECT id FROM episodes ORDER BY updated DESC LIMIT 500)')

    def get(self, number):
        with self.connect() as c:
            row = c.execute('SELECT * FROM episodes WHERE id=?', (number,)).fetchone()
        return dict(row) if row else None

    def search(self, query='', owner='', limit=5):
        # Literal substring match; LIKE wildcards never turn a query into a dump.
        with self.connect() as c:
            rows = c.execute('SELECT * FROM episodes WHERE (?="" OR owner=?) ORDER BY updated DESC LIMIT 500', (owner, owner)).fetchall()
        tokens = [key(t) for t in re.split(r'\s+', query) if t]
        results = []
        for raw in rows:
            row = dict(raw)
            hay = key(row['topic'] + row['detail'] + FAMILY.get(row['owner'], '') + row['owner'])
            if not tokens or any(t in hay for t in tokens):
                results.append(row)
        return results[:limit]

    @staticmethod
    def view(row):
        return {k: row[k] for k in ('id', 'owner', 'kind', 'topic', 'phase', 'detail', 'event_day', 'updated')} | {'address': FAMILY.get(row['owner'], 'QQ '+row['owner'])}

    def save(self, actor, origin, mid, source, kind, topic, evidence, phase='ongoing', number=0, day='', detail='', joke_confirmed=False):
        if forbidden(source):
            raise ValueError('本人要求不记，这条没有保存。')
        if kind not in ('event', 'joke') or phase not in ('planned', 'ongoing', 'resolved', 'cancelled'):
            raise ValueError('不支持的往事类型或进展。')
        topic, evidence, detail = str(topic).strip(), str(evidence).strip(), str(detail).strip()
        if kind == 'event' and not number:
            # A title is the object/topic, rather than a whole first-person progress sentence.
            clipped=re.split(r'[，。,.；;]',topic,1)[0]
            noun=re.sub(r'^我(?:正在|在)?(?:制作|做|参加|进行)(?:一个|一只|一件)?','',clipped).strip()
            if len(noun)>=2 and noun in evidence:topic=noun
        if kind == 'joke':
            clipped=re.split(r'[，。,.；;]',topic,1)[0]
            noun=re.sub(r'^(?:我们|咱们)?(?:以后)?(?:就|都)?(?:把这)?(?:叫作|称为|就叫|叫)','',clipped).strip(' “”“「」')
            noun=re.sub(r'吧$','',noun)
            if len(noun)>=2 and noun in evidence:topic=noun
        if not 2 <= len(topic) <= 40 or not 2 <= len(evidence) <= 350 or evidence not in source:
            raise ValueError('主题和原话依据不完整，不能猜测保存。')
        if re.search(INDIRECT, source) or not direct(evidence):
            # A short first-person result can omit 我, provided the full human message has it.
            if not (kind == 'event' and direct(source) and re.search(DONE, evidence) and not re.search(INDIRECT, source)):
                raise ValueError('只记本人直接说的真实事情，不记转述、假设或猜测。')
        if re.search(r'(?:https?://|sk-[A-Za-z0-9]|密码|验证码|API.?Key|密钥)', source, re.I):
            raise ValueError('这条含凭据内容，没有保存。')
        if detail and detail not in evidence:
            raise ValueError('进展文字必须取自原话，不添加推测。')
        detail = detail or evidence
        if kind == 'event':
            if topic not in evidence and not any(t in topic and t in evidence for t in TOPICS):
                raise ValueError('事情主题需要在原话里出现。')
            if phase in ('resolved', 'cancelled') and not re.search(DONE + r'|取消|不去了|不做了|放弃了|已结束|已完成', evidence):
                raise ValueError('没有明确结果，不把事情标成结束。')
            if phase == 'planned' and not re.search(r'明天|后天|今天|下周|周[一二三四五六日天]|\d+[月日号]|准备|打算|计划|将要|20\d\d-', evidence):
                raise ValueError('没有明确计划依据。')
            if phase == 'planned' and re.search(r'(?:不|没|没有|不用|无需).{0,4}(?:'+re.escape(topic)+r'|参加|去面试|去考试)', evidence):
                raise ValueError('原话是否定计划，不把它记成会发生的事情。')
        else:
            if topic not in evidence or not joke_confirmed:
                raise ValueError('共同梗需要真实双方上下文或本人明确确认，不能凭空发明过去。')
            phase = 'ongoing'
            if not number:
                same=[r for r in self.search(topic,actor,20) if r['kind']=='joke' and key(r['topic'])==key(topic)]
                if same:number=same[0]['id']
        today = dt.datetime.now(TZ).date()
        if day:
            parsed = dt.date.fromisoformat(day)
            if abs((parsed - today).days) > 366:
                raise ValueError('日期过远，先按原话保留，不猜时间。')
            # Exact dates only. Relative dates are resolved deterministically by observe().
            if day not in evidence and not re.search(r'今天|明天|后天', evidence):
                # Store the verified report timestamp separately, leave the event's date unknown.
                day = ''
            if re.search(r'今天|明天|后天', evidence):
                offset = 2 if '后天' in evidence else 1 if '明天' in evidence else 0
                if parsed != today + dt.timedelta(days=offset):
                    raise ValueError('相对日期与北京时间不符。')
        digest = hashlib.sha256(json.dumps([actor, origin, str(mid), kind, topic, evidence, phase], ensure_ascii=False).encode()).hexdigest()
        now = time.time()
        with self.connect() as c:
            prior = c.execute('SELECT id FROM episodes WHERE digest=?', (digest,)).fetchone()
            if prior:
                return self.get(prior['id'])
            if number:
                row = self.get(number)
                if not row or row['owner'] != actor or row['kind'] != kind or key(row['topic']) != key(topic):
                    raise ValueError('只能更新本人同一件事；不能改变归属或主题。')
                history = json.loads(row['history'])[-11:]
                history.append({'phase': row['phase'], 'detail': row['detail'], 'evidence': row['evidence'], 'origin': row['origin'], 'message_id': row['message_id'], 'updated': row['updated']})
                c.execute('UPDATE episodes SET phase=?,detail=?,evidence=?,origin=?,message_id=?,event_day=?,updated=?,history=?,digest=? WHERE id=?',
                          (phase, detail, evidence, origin, str(mid), day or row['event_day'], now, json.dumps(history, ensure_ascii=False), digest, number))
            else:
                number = c.execute('INSERT INTO episodes(owner,kind,topic,phase,detail,evidence,origin,message_id,event_day,created,updated,history,digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (actor, kind, topic, phase, detail, evidence, origin, str(mid), day, now, now, '[]', digest)).lastrowid
        self.prune()
        return self.get(number)

    def forget(self, actor, number):
        row = self.get(number)
        if not row or (row['owner'] != actor and actor != OWNER):
            raise ValueError('只能删除自己的往事；爸爸可以管理全部。')
        with self.connect() as c:
            c.execute('DELETE FROM episodes WHERE id=?', (number,))

    def observe(self, actor, origin, mid, text):
        """Cheap conservative extraction: only dated first-person plans or explicit topic outcomes."""
        if len(text) > 350 or forbidden(text) or not direct(text) or re.search(r'[?？]|吗|是不是|能不能', text):
            return []
        # Avoid interpreting commands/prompts about sending as a new personal plan.
        if re.search(r'提醒我|定时|帮我|给.{0,12}发|写.{0,8}(?:故事|歌词)|记忆|往事', text):
            return []
        found = []
        for topic in TOPICS:
            if topic not in text:
                continue
            done = bool(re.search(DONE, text))
            planned = bool(re.search(r'今天|明天|后天|下周|周[一二三四五六日天]|\d+[月日号]', text))
            if not done and not planned:
                continue
            rows = self.search(topic, actor, 10)
            if done:
                candidates = [r for r in rows if r['kind']=='event' and r['phase'] in ('ongoing','planned')]
                # Ambiguous multiple pending events are left for explicit tool selection.
                if len(candidates) > 1:
                    continue
                number = candidates[0]['id'] if candidates else 0
                phase = 'cancelled' if '取消' in text else 'resolved'
                day = ''
            else:
                if not re.search(r'我.{0,20}(?:要|准备|打算|计划|有|参加|考试|面试|答辩|比赛|出差|搬家)', text):
                    continue
                day = ''
                if re.search(r'今天|明天|后天', text):
                    offset = 2 if '后天' in text else 1 if '明天' in text else 0
                    day = (dt.datetime.now(TZ).date()+dt.timedelta(days=offset)).isoformat()
                existing = [r for r in rows if r['kind']=='event' and r['phase']=='planned' and ((day and r['event_day']==day) or key(r['evidence'])==key(text))]
                number = existing[0]['id'] if len(existing)==1 else 0
                phase = 'planned'
            try:
                found.append(self.save(actor, origin, mid, text, 'event', topic, text, phase, number, day))
            except (ValueError, sqlite3.Error):
                continue
        return found
