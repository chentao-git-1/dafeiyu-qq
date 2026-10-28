"""Confirmed dialogue metadata and deliberately coarse, revocable public topics.

No message text, nickname, media or generated reply is stored here. Public
summaries are fixed phrases from a small vocabulary, never model summaries of
private conversations.
"""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

OWNER = '7000000001'
SELF = '7000000002'
KNOWN_BOTS = {'7000000201', '7000000202'}
TZ = dt.timezone(dt.timedelta(hours=8))
WEEK = 7 * 86400
SEGMENT_GAP = 30 * 60
TOPICS = {
    'music': (r'音乐|旋律|节奏|歌曲', '聊到了音乐里的旋律和节奏'),
    'movies': (r'电影|影片|镜头', '聊到了电影的叙事和画面'),
    'games': (r'游戏|关卡|玩法', '聊到了游戏的玩法和乐趣'),
    'colours': (r'颜色|配色|色彩', '聊到了颜色和配色'),
    'science': (r'科普|光合作用|彩虹|为什么天空|天文', '聊到了自然科学里的小知识'),
}
SENSITIVE = re.compile(
    r'\d|https?://|[@＠]|[“”「」『』"`]|QQ|微信|群名|昵称|姓名|名字|叫我|叫做|叫作|'
    r'住在|地址|学校|公司|医院|医生|生病|健康|药|痛|癌|抑郁|钱|余额|工资|银行|付款|'
    r'感情|恋爱|分手|男友|女友|喜欢你|吵架|争执|家庭|爸爸|妈妈|爸妈|父母|老公|老婆|'
    r'孩子|考试|面试|求职|工作|简历|成绩|录取|计划|打算|准备|约好|明天|后天|下周|'
    r'行程|车站|机场|几点|点半|密码|密钥|凭据|账号|身份证|手机号|住院|去世|失眠|'
    r'诊断|手术|怀孕|治疗|发烧|身体|借款|房租|股票|信用卡|欠款|转账|老妈|老爸|亲戚|'
    r'被裁|考研|高考|考公|录用|加班|婚|离异|收入|债|住址|定位|航班|酒店|'
    r'保密|隐私|秘密|别告诉|不公开|别公开|别写|不要写|同学|同事|邻居|只有我|第一次|'
    r'纪念|生日|今天我|昨天我|我刚|我在|我家|我们家|一起去|见面|旅游|出差', re.I)


def privacy_command(text):
    """Only a complete, direct first-person command can change permission."""
    value = str(text or '').strip().lstrip('/').strip('。！!～~ ')
    if value in ('这段别写进总结', '这段不要写进总结'):
        return 'segment'
    if value in ('以后别公开我的聊天', '以后不要公开我的聊天'):
        return 'disable'
    return None


def safe_topic(text, media=False):
    if media or not isinstance(text, str) or not 2 <= len(text) <= 400 or SENSITIVE.search(text):
        return None
    found = [key for key, (pattern, _) in TOPICS.items() if re.search(pattern, text)]
    return found[0] if len(found) == 1 else None


class ActivityStore:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS dialogue_turns (
                    event_key TEXT PRIMARY KEY, user_id TEXT NOT NULL, occurred_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS dialogue_turns_time ON dialogue_turns(occurred_at,user_id);
                CREATE TABLE IF NOT EXISTS public_summary_preferences (
                    user_id TEXT PRIMARY KEY, disabled INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS public_topic_segments (
                    user_id TEXT, origin TEXT, segment TEXT NOT NULL, last_at REAL NOT NULL,
                    topic TEXT, blocked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,origin));
                CREATE TABLE IF NOT EXISTS public_topic_candidates (
                    event_key TEXT PRIMARY KEY, user_id TEXT NOT NULL, origin TEXT NOT NULL,
                    segment TEXT NOT NULL, occurred_at REAL NOT NULL, topic TEXT NOT NULL);
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

    @staticmethod
    def key(uid, origin, mid):
        return hashlib.sha256((str(uid)+'\0'+str(origin)+'\0'+str(mid)).encode()).hexdigest()

    def prepare(self, uid, origin, mid, occurred_at, text, media=False):
        """Called on original input, before captions, history or STT are added."""
        now = self.clock()
        if (not re.fullmatch(r'[1-9]\d{4,11}', str(uid)) or not mid
                or isinstance(occurred_at, bool) or not isinstance(occurred_at, (int, float))
                or not math.isfinite(occurred_at) or not now-WEEK < occurred_at <= now+5):
            return None
        command = privacy_command(text)
        topic = safe_topic(text, media)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM public_topic_segments WHERE user_id=? AND origin=?', (str(uid), origin)).fetchone()
            # Refusals affect the current topic, including earlier candidates.
            fresh = not row or (not command and (
                occurred_at-row['last_at'] > SEGMENT_GAP or
                topic and row['topic'] and topic != row['topic']))
            segment = uuid.uuid4().hex if fresh else row['segment']
            blocked = 0 if fresh else row['blocked']
            if command or media or SENSITIVE.search(text):
                blocked = 1
                db.execute('DELETE FROM public_topic_candidates WHERE user_id=? AND origin=? AND segment=?', (str(uid), origin, segment))
            if command == 'disable':
                db.execute('INSERT OR REPLACE INTO public_summary_preferences VALUES (?,1)', (str(uid),))
                db.execute('DELETE FROM public_topic_candidates WHERE user_id=?', (str(uid),))
            # An old redelivery must not reopen or move a newer conversation.
            if not row or occurred_at >= row['last_at']:
                db.execute('INSERT OR REPLACE INTO public_topic_segments VALUES (?,?,?,?,?,?)',
                           (str(uid), origin, segment, occurred_at, topic or (row['topic'] if row and not fresh else None), blocked))
            elif not fresh:
                blocked = 1
        if command:
            return {'command': command}
        return {'user_id': str(uid), 'origin': origin, 'event_key': self.key(uid, origin, mid),
                'occurred_at': occurred_at, 'segment': segment, 'topic': topic if not blocked else None}

    def record_confirmed(self, source, excluded=()):
        if not source or 'event_key' not in source or source['user_id'] in {SELF, *KNOWN_BOTS, *map(str, excluded)}:
            return False
        now, at = self.clock(), source['occurred_at']
        if not now-WEEK < at <= now+5:
            return False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM dialogue_turns WHERE occurred_at<=?', (now-WEEK,))
            db.execute('DELETE FROM public_topic_candidates WHERE occurred_at<?', (now-2*86400,))
            added = db.execute('INSERT OR IGNORE INTO dialogue_turns VALUES (?,?,?)',
                               (source['event_key'], source['user_id'], at)).rowcount == 1
            permission = db.execute('SELECT disabled FROM public_summary_preferences WHERE user_id=?', (source['user_id'],)).fetchone()
            segment = db.execute('SELECT segment,blocked FROM public_topic_segments WHERE user_id=? AND origin=?',
                                 (source['user_id'], source['origin'])).fetchone()
            if (added and source.get('topic') in TOPICS and not (permission and permission['disabled'])
                    and segment and segment['segment'] == source['segment'] and not segment['blocked']):
                db.execute('INSERT OR IGNORE INTO public_topic_candidates VALUES (?,?,?,?,?,?)',
                           (source['event_key'], source['user_id'], source['origin'], source['segment'], at, source['topic']))
        return added

    def ranked(self, excluded=()):
        now = self.clock()
        omitted = {OWNER, SELF, *KNOWN_BOTS, *map(str, excluded)}
        with self.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='bots'").fetchone():
                omitted.update(row[0] for row in db.execute('SELECT qq FROM bots'))
            rows = db.execute('''SELECT user_id,count(*) AS turns,max(occurred_at) AS last_at
                FROM dialogue_turns WHERE occurred_at>? AND occurred_at<=?
                GROUP BY user_id ORDER BY turns DESC,last_at DESC,CAST(user_id AS INTEGER) ASC''', (now-WEEK, now)).fetchall()
        return [{'qq': r['user_id'], 'turns': r['turns'], 'last_at': r['last_at']} for r in rows if r['user_id'] not in omitted]

    def public_topics(self, now=None):
        now = self.clock() if now is None else now
        day = dt.datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.connect() as db:
            bots = {row[0] for row in db.execute('SELECT qq FROM bots')} if db.execute("SELECT 1 FROM sqlite_master WHERE name='bots'").fetchone() else set()
            rows = db.execute('''SELECT c.event_key,c.topic,c.user_id FROM public_topic_candidates c
                LEFT JOIN public_summary_preferences p ON c.user_id=p.user_id
                WHERE c.occurred_at>=? AND c.occurred_at<=? AND COALESCE(p.disabled,0)=0
                ORDER BY c.occurred_at DESC,c.event_key''', (day.timestamp(), now)).fetchall()
        result, seen = [], set()
        for row in rows:
            if row['topic'] not in TOPICS or row['topic'] in seen or row['user_id'] in bots:
                continue
            result.append({'key': row['event_key'], 'date': day.date().isoformat(), 'summary': TOPICS[row['topic']][1]})
            seen.add(row['topic'])
            if len(result) == 2:
                break
        return result

    def topics_valid(self, topics):
        if not topics:
            return True
        with self.connect() as db:
            bots = {row[0] for row in db.execute('SELECT qq FROM bots')} if db.execute("SELECT 1 FROM sqlite_master WHERE name='bots'").fetchone() else set()
            for topic in topics:
                row = db.execute('''SELECT c.topic,c.occurred_at,c.user_id,p.disabled FROM public_topic_candidates c
                    LEFT JOIN public_summary_preferences p ON c.user_id=p.user_id WHERE c.event_key=?''', (topic.get('key'),)).fetchone()
                if (not row or row['disabled'] or row['user_id'] in bots or row['topic'] not in TOPICS
                        or topic.get('summary') != TOPICS[row['topic']][1]
                        or dt.datetime.fromtimestamp(row['occurred_at'], TZ).date().isoformat() != topic.get('date')):
                    return False
        return True

    def purge_drafts(self, qzone_path):
        """Erase revoked, unpublished drafts; never touch confirmed public posts."""
        if not Path(qzone_path).is_file():
            return
        with sqlite3.connect(str(qzone_path), timeout=3) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='qzone_editorial_candidates'").fetchone():
                return
            rows = db.execute('SELECT text_key,plan FROM qzone_editorial_candidates').fetchall()
            for key, raw in rows:
                plan = json.loads(raw)
                if plan.get('chat_topics') and not self.topics_valid(plan['chat_topics']):
                    db.execute('DELETE FROM qzone_editorial_candidates WHERE text_key=?', (key,))
