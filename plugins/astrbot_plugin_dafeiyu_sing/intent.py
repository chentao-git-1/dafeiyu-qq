"""Scoped song intent and durable, single-attempt generation reservations."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

QUOTES=re.compile(r'```.*?```|`[^`]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"',re.S)
CANCEL=re.compile(r'别唱|不要唱|不用唱|先不唱|停唱|算了|取消')
REQUEST=re.compile(r'^/?(?:唱歌|唱)[吧。！!\s]*$|唱(?:一首|首歌|一段|一小段|个|出来|给|慢一点|快一点|完.{0,8}发)|(?:你|给我|帮我|请|来|再|试着|给.{1,16}).{0,8}唱(?:歌|首|一|段|个)|\bsing\b',re.I)
FOLLOW=re.compile(r'(?:你(?:自己)?(?:找|选|挑)(?:一首|首|个)?(?:吧|就行)?|一首(?:好听|流行|温柔|慢一点)的|来一首|再来(?:一首)?|再试(?:一次)?|继续(?:刚才的)?|好|好的|可以|行|嗯)[。！!\s]*')


def intent_kind(text, previous=False, offered=False):
    text=QUOTES.sub('',str(text or '')).strip()
    if re.search(r'他说|她说|假如|假设|比如|翻译|什么意思|这句话',text):return 'none'
    if CANCEL.search(text):return 'cancel'
    if re.fullmatch(r'.{0,15}(?:会|能|可以)唱(?:歌|流行歌|一首歌)?吗[？?\s]*',text):return 'none'
    if REQUEST.search(text) and not re.fullmatch(r'.{0,15}(?:会|能|可以)唱(?:歌|流行歌|一首歌)?吗[？?\s]*',text):return 'request'
    if FOLLOW.fullmatch(text):
        if text.strip('。！! ') in ('好','好的','可以','行','嗯'):
            return 'request' if offered else 'none'
        return 'request' if previous or offered else 'none'
    return 'none'


class IntentStore:
    def __init__(self,path,clock=time.time):
        self.path,self.clock=Path(path),clock
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks(scope TEXT PRIMARY KEY,target TEXT,at REAL,status TEXT);
                CREATE TABLE IF NOT EXISTS attempts(request_key TEXT PRIMARY KEY,target TEXT,at REAL,status TEXT,result TEXT);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(str(self.path),timeout=3);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def task(self,scope):
        with self.connect() as db:row=db.execute('SELECT * FROM tasks WHERE scope=?',(scope,)).fetchone()
        return dict(row) if row and self.clock()-row['at']<600 and row['status']!='cancelled' else None

    def observe(self,scope,text,target,offered=False):
        previous=self.task(scope)
        kind=intent_kind(text,bool(previous),offered)
        with self.connect() as db:
            if kind=='cancel':db.execute('DELETE FROM tasks WHERE scope=?',(scope,))
            elif kind=='request':db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?,?,?)',(scope,target,self.clock(),'pending'))
            elif previous and not re.search(r'歌|唱|旋律|好听|难听|流行|歌词|音乐|歌喉|升级|^[嗯哦好哈。！!\s]+$',text):
                db.execute('DELETE FROM tasks WHERE scope=?',(scope,))
        return kind

    def reserve(self,key,target,cooldown):
        now=self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT * FROM attempts WHERE request_key=?',(key,)).fetchone()
            if prior:return json.loads(prior['result']) if prior['result'] else {'status':'in_flight','reply':'这条请求已经在生成，先等这一条结果。'}
            active=db.execute("SELECT 1 FROM attempts WHERE status='generating' AND at>?",(now-180,)).fetchone()
            if active:return {'status':'busy','reply':'现在还有一首正在生成，先等它完成。'}
            recent=db.execute('SELECT at FROM attempts WHERE target=? ORDER BY at DESC LIMIT 1',(target,)).fetchone()
            if recent and now-recent['at']<cooldown:
                seconds=max(1,int(cooldown-(now-recent['at'])+0.999))
                return {'status':'cooldown','reply':'刚唱过一段，约 '+str(seconds)+' 秒后可以再唱。'}
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?)',(key,target,now,'generating',None))
            db.execute('DELETE FROM attempts WHERE at<?',(now-7*86400,))
        return None

    def finish(self,key,outcome):
        with self.connect() as db:
            db.execute('UPDATE attempts SET status=?,result=? WHERE request_key=?',(outcome['status'],json.dumps(outcome,ensure_ascii=False),key))


def request_key(event,target):
    mid=str(getattr(event.message_obj,'message_id','') or '')
    if not mid:return None
    return hashlib.sha256((event.unified_msg_origin+'|'+str(event.get_sender_id())+'|'+mid+'|'+target).encode()).hexdigest()
