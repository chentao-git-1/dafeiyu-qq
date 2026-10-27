"""Atomic personal settings, bot conversation budgets and explicit calendar dates."""
import calendar
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import time

OWNER = '7000000001'
SELF = '7000000002'
TZ = dt.timezone(dt.timedelta(hours=8))
DEFAULT = {'voice': 'auto', 'memes': 'auto', 'proactive': True, 'quiet_start': None, 'quiet_end': None}


def clock_minutes(value):
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', str(value)):
        raise ValueError('时间请用 23:00 这样的格式。')
    hour, minute = map(int, value.split(':'))
    return hour * 60 + minute


def can_notify_profile(profile, timestamp):
    if not profile['proactive']:
        return False
    start, end = profile['quiet_start'], profile['quiet_end']
    if start is None or end is None:
        return True
    current = dt.datetime.fromtimestamp(timestamp, TZ)
    minute = current.hour * 60 + current.minute
    return not (start <= minute < end if start < end else minute >= start or minute < end)


def occurrence(row, year):
    if row.get('year') and year < row['year']:
        return None
    day = 28 if row['month'] == 2 and row['day'] == 29 and not calendar.isleap(year) else row['day']
    return dt.date(year, row['month'], day)


class Store:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS profiles (user_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bots (qq TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bot_groups (group_id TEXT PRIMARY KEY, started REAL NOT NULL, turns INTEGER NOT NULL, cooldown REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS bot_claims (group_id TEXT, turn_id TEXT, at REAL, PRIMARY KEY(group_id,turn_id));
                CREATE TABLE IF NOT EXISTS dates (id INTEGER PRIMARY KEY AUTOINCREMENT, creator TEXT NOT NULL, target TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, month INTEGER NOT NULL, day INTEGER NOT NULL, year INTEGER, title TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, UNIQUE(target,kind,title));
                CREATE TABLE IF NOT EXISTS notices (notice_key TEXT PRIMARY KEY, status TEXT NOT NULL, at REAL NOT NULL, message_id TEXT);
                CREATE TABLE IF NOT EXISTS meme_budget (user_id TEXT PRIMARY KEY, at REAL NOT NULL);
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

    def profile(self, uid):
        with self.connect() as db:
            row = db.execute('SELECT data FROM profiles WHERE user_id=?', (str(uid),)).fetchone()
        return {**DEFAULT, **(json.loads(row['data']) if row else {})}

    def set_profile(self, uid, changes):
        if not re.fullmatch(r'[1-9]\d{4,11}', str(uid)) or not set(changes) <= set(DEFAULT):
            raise ValueError('聊天设置参数无效。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM profiles WHERE user_id=?', (str(uid),)).fetchone()
            value = {**DEFAULT, **(json.loads(row['data']) if row else {}), **changes}
            if value['voice'] not in ('auto', 'text', 'voice') or value['memes'] not in ('auto', 'few', 'off') or not isinstance(value['proactive'], bool):
                raise ValueError('聊天设置参数无效。')
            start, end = value['quiet_start'], value['quiet_end']
            if (start is None) != (end is None) or (start is not None and (not isinstance(start,int) or not isinstance(end,int) or not 0 <= start < 1440 or not 0 <= end < 1440 or start == end)):
                raise ValueError('免打扰需要不同的开始和结束时间。')
            db.execute('INSERT OR REPLACE INTO profiles VALUES (?,?)', (str(uid), json.dumps(value)))
        return value

    def allow_meme(self, uid, mode):
        if mode == 'off': return False
        if mode != 'few': return True
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT at FROM meme_budget WHERE user_id=?', (str(uid),)).fetchone()
            if row and now - row['at'] < 300: return False
            db.execute('INSERT OR REPLACE INTO meme_budget VALUES (?,?)', (str(uid), now))
        return True

    def bots(self):
        with self.connect() as db:
            return {r['qq']: r['name'] for r in db.execute('SELECT * FROM bots ORDER BY qq')}

    def set_bot(self, actor, qq, name='', remove=False):
        if str(actor) != OWNER or not re.fullmatch(r'[1-9]\d{4,11}', str(qq)) or str(qq) in (OWNER, SELF):
            raise ValueError('只有爸爸能管理其他机器人的标记。')
        with self.connect() as db:
            if remove:
                db.execute('DELETE FROM bots WHERE qq=?', (str(qq),))
            else:
                db.execute('INSERT OR REPLACE INTO bots VALUES (?,?)', (str(qq), str(name or qq)[:32]))

    def human_turn(self, group):
        with self.connect() as db:
            db.execute('DELETE FROM bot_groups WHERE group_id=?', (str(group),))

    def bot_turn(self, group, turn_id):
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM bot_claims WHERE at<?', (now - 86400,))
            if db.execute('SELECT 1 FROM bot_claims WHERE group_id=? AND turn_id=?', (str(group), str(turn_id))).fetchone():
                return False, 'duplicate'
            row = db.execute('SELECT * FROM bot_groups WHERE group_id=?', (str(group),)).fetchone()
            if row and row['cooldown'] > now:
                return False, 'cooldown'
            turns = row['turns'] if row and now - row['started'] < 600 else 0
            started = row['started'] if turns else now
            turns += 1
            db.execute('INSERT OR REPLACE INTO bot_groups VALUES (?,?,?,?)', (str(group), started, turns, now + 1800 if turns >= 3 else 0))
            db.execute('INSERT INTO bot_claims VALUES (?,?,?)', (str(group), str(turn_id), now))
        return True, 'allowed'

    def set_date(self, actor, target, name, kind, month, day, year=None, title=''):
        actor, target = str(actor), str(target)
        if actor != OWNER and actor != target:
            raise ValueError('只能设置自己的日期。')
        if not re.fullmatch(r'[1-9]\d{4,11}', target) or kind not in ('birthday', 'anniversary'):
            raise ValueError('日期对象无效。')
        dt.date(year or 2000, month, day)
        if year is not None and not 1900 <= year <= 2100:
            raise ValueError('年份请用 1900–2100 之间的四位数字。')
        title = '' if kind == 'birthday' else str(title).strip()[:40]
        if kind == 'anniversary' and not title:
            raise ValueError('请给纪念日起个名字。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM dates WHERE target=? AND kind=? AND title=?', (target, kind, title)).fetchone()
            if prior:
                if (prior['name'],prior['month'],prior['day'],prior['year']) != (str(name)[:32],month,day,year):
                    db.execute('UPDATE dates SET creator=?,name=?,month=?,day=?,year=?,revision=revision+1 WHERE id=?', (actor, str(name)[:32], month, day, year, prior['id']))
                number = prior['id']
            else:
                number = db.execute('INSERT INTO dates (creator,target,name,kind,month,day,year,title) VALUES (?,?,?,?,?,?,?,?)', (actor, target, str(name)[:32], kind, month, day, year, title)).lastrowid
        return self.get_date(actor, number)

    def dates(self, actor):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM dates WHERE ?=? OR creator=? OR target=? ORDER BY month,day,id', (str(actor), OWNER, str(actor), str(actor))).fetchall()
        return [dict(r) for r in rows]

    def get_date(self, actor, number):
        return next((row for row in self.dates(actor) if row['id'] == int(number)), None)

    def delete_date(self, actor, number):
        if not self.get_date(actor, number): raise ValueError('没有可管理的这个日期记录。')
        with self.connect() as db:
            db.execute('DELETE FROM dates WHERE id=?', (int(number),))

    def due_dates(self, timestamp):
        now = dt.datetime.fromtimestamp(timestamp, TZ)
        if not 9 <= now.hour < 21: return []
        actions = []
        for row in self.dates(OWNER):
            for year in (now.year, now.year + 1):
                due = occurrence(row, year)
                if due is None: continue
                delta = (due - now.date()).days
                if delta not in (0, 1): continue
                key = 'date:{}:{}:{}:{}'.format(row['id'], row['revision'], due.isoformat(), delta)
                with self.connect() as db:
                    used = db.execute('SELECT 1 FROM notices WHERE notice_key=?', (key,)).fetchone()
                if not used:
                    actions.append({'key':key, 'row':row, 'due':due.isoformat(), 'days_before':delta})
        return actions

    def reserve_notice(self, key):
        with self.connect() as db:
            try:
                db.execute('INSERT INTO notices VALUES (?,?,?,NULL)', (key, 'sending', self.clock()))
            except sqlite3.IntegrityError:
                return False
        return True

    def finish_notice(self, key, status, message_id=None):
        with self.connect() as db:
            db.execute('UPDATE notices SET status=?,message_id=? WHERE notice_key=?', (status, str(message_id) if message_id is not None else None, key))
