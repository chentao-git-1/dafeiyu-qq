"""Persistent bot-owned activities and verified artifacts, without a runtime."""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3
import time

TZ = dt.timezone(dt.timedelta(hours=8))


def beijing_day(now=None):
    return dt.datetime.fromtimestamp(time.time() if now is None else now, TZ).date().isoformat()


def failure_code(error):
    code=getattr(error,'code','')
    return code if isinstance(code,str) and re.fullmatch(r'reading_(query|search|fetch)_[a-z0-9_]{1,48}',code) else type(error).__name__


class LifeStore:
    def __init__(self,root,model_schedule=False):
        self.model_schedule=model_schedule
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.root.chmod(0o700)
        self.path=self.root/'life.db'
        with self.db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS runs(day TEXT PRIMARY KEY, run_at REAL NOT NULL,status TEXT NOT NULL,kind TEXT,record TEXT,error TEXT,updated REAL NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS failed_attempts(id TEXT PRIMARY KEY,day TEXT,kind TEXT,status TEXT,error TEXT,updated REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS activities(id TEXT PRIMARY KEY,day TEXT,run_at REAL,status TEXT,kind TEXT,choice TEXT,record TEXT,error TEXT,updated REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS activity_failures(id TEXT PRIMARY KEY,activity_id TEXT,day TEXT,kind TEXT,status TEXT,error TEXT,updated REAL)')
            c.execute('CREATE INDEX IF NOT EXISTS activities_by_day ON activities(day,run_at)')
            c.execute("INSERT OR IGNORE INTO settings VALUES('enabled','true')")
            c.execute("UPDATE runs SET status='unknown',error='interrupted_before_confirmation' WHERE status='started'")
            c.execute("UPDATE activities SET status='unknown',error='interrupted_before_confirmation' WHERE status='started'")
        self.path.chmod(0o600)
    @contextmanager
    def db(self):
        c=sqlite3.connect(str(self.path),timeout=3);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def enabled(self):
        with self.db() as c:return c.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0]=='true'
    def set_enabled(self,value):
        with self.db() as c:c.execute("UPDATE settings SET value=? WHERE key='enabled'",('true' if value else 'false',))
    @staticmethod
    def target(key):
        return ('activities','id') if str(key).startswith('agenda:') else ('runs','day')
    def activity(self,key):
        with self.db() as c:
            row=c.execute('SELECT * FROM activities WHERE id=?',(key,)).fetchone()
        return dict(row) if row else None
    def in_progress(self,now=None):
        now=time.time() if now is None else now
        with self.db() as db:
            rows=db.execute("SELECT day,kind,status,updated FROM runs WHERE day=? AND status='started' UNION ALL SELECT day,kind,status,updated FROM activities WHERE day=? AND status='started' ORDER BY updated DESC",(beijing_day(now),beijing_day(now))).fetchall()
        return next((dict(r) for r in rows if 0<=now-r['updated']<=600),None)
    def artifact_name(self,key,day):
        return day+'-'+hashlib.sha256(key.encode()).hexdigest()[:12] if str(key).startswith('agenda:') else day
    def schedule(self,now=None):
        now=time.time() if now is None else now
        day=dt.datetime.fromtimestamp(now,TZ).date()
        start=dt.datetime.combine(day,dt.time(9),TZ).timestamp();end=start+12*3600-600
        when=random.SystemRandom().uniform(max(start,now+120),end) if now+120<end else end
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO runs(day,run_at,status,updated) VALUES(?,?,?,?)',(day.isoformat(),when,'pending',now))
            return dict(c.execute('SELECT * FROM runs WHERE day=?',(day.isoformat(),)).fetchone())
    def claim(self,day,kind,now=None,failed_at=None):
        now=time.time() if now is None else now
        table,column=self.target(day)
        with self.db() as c:
            if failed_at is not None:
                return c.execute("UPDATE "+table+" SET status='started',kind=?,error=NULL,updated=? WHERE "+column+"=? AND status='failed' AND kind='reading' AND updated=?",
                    (kind,now,day,failed_at)).rowcount==1
            return c.execute("UPDATE "+table+" SET status='started',kind=?,updated=? WHERE "+column+"=? AND status='pending'",(kind,now,day)).rowcount==1
    def skip(self,key,reason='model_chose_no_activity'):
        table,column=self.target(key)
        with self.db() as db:
            return db.execute("UPDATE "+table+" SET status='skipped',kind=COALESCE(?,kind),error=?,updated=? WHERE "+column+"=? AND status IN ('pending','failed')",
                ('none' if reason=='model_chose_no_activity' else None,reason,time.time(),key)).rowcount==1
    def reopen_skipped(self,plan):
        if any((self.root/'works'/(plan['day']+suffix)).exists() for suffix in ('.json','.jpg')):return False
        with self.db() as db:
            return db.execute("UPDATE runs SET status='pending',kind=NULL,updated=? WHERE day=? AND status='skipped' AND kind='none' AND updated=?",
                (time.time(),plan['day'],plan['updated'])).rowcount==1
    def outcomes(self):
        with self.db() as db:
            rows=list(db.execute("SELECT day,kind,status,error,updated FROM runs WHERE status IN ('failed','unknown') ORDER BY updated DESC LIMIT 6"))
            rows+=list(db.execute("SELECT id AS activity_id,day,kind,status,error,updated FROM activities WHERE status IN ('failed','unknown') ORDER BY updated DESC LIMIT 6"))
            rows+=list(db.execute('SELECT day,kind,status,error,updated FROM failed_attempts ORDER BY updated DESC LIMIT 6'))
            rows+=list(db.execute('SELECT activity_id,day,kind,status,error,updated FROM activity_failures ORDER BY updated DESC LIMIT 6'))
        result=[]
        seen=set()
        for row in sorted(rows,key=lambda row:row['updated'],reverse=True):
            value=dict(row);key=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()[:24]
            if key in seen:continue
            seen.add(key)
            result.append(dict(id='outcome:'+key,note=str(row['day'])+' '+str(row['kind'])+' '+row['status']+' '+str(row['error']),**value))
        return result[:6]
    def activity_state(self,now=None):
        today=beijing_day(now)
        with self.db() as db:
            row=db.execute('SELECT status,kind FROM runs WHERE day=?',(today,)).fetchone()
            scheduled=[dict(r) for r in db.execute('SELECT status,kind,run_at FROM activities WHERE day=? ORDER BY run_at',(today,))]
        if self.model_schedule:
            completed=sum(r['status']=='completed' for r in scheduled)+int(bool(row and row['status']=='completed'))
            return dict(day=today,status='running' if any(r['status']=='started' for r in scheduled) else 'scheduled' if any(r['status']=='pending' for r in scheduled) else 'finished' if scheduled else 'not_planned',
                daily_activity_limit=None,activity_count_policy='model_planned',completed_today=completed,
                planned_activities=len(scheduled),can_start_reading_today=any(r['status']=='pending' and r['kind']=='reading' for r in scheduled),
                failed_reading_may_choose_writing=any(r['status']=='failed' and r['kind']=='reading' for r in scheduled))
        status=row['status'] if row else 'pending'
        return dict(day=today,status=status,kind=row['kind'] if row else None,daily_activity_limit=1,
            completed_today=int(status=='completed'),can_start_reading_today=status=='pending',
            failed_reading_may_choose_writing=status=='failed' and row['kind']=='reading')
    def records(self,limit=7):
        with self.db() as c:
            rows=c.execute("SELECT record,updated FROM runs WHERE status='completed' UNION ALL SELECT record,updated FROM activities WHERE status='completed' ORDER BY updated DESC LIMIT ?",(limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]
    def verified_records(self,limit=12):
        records=[]
        for record in self.records(limit):
            try:
                path=(self.root/record['artifact']).resolve()
                if (path.parent==(self.root/'works').resolve() and path.is_file()
                        and hashlib.sha256(path.read_bytes()).hexdigest()==record['sha256']
                        and 0<float(record['completed_at'])<=time.time()+5):records.append(record)
            except (KeyError,OSError,ValueError,TypeError):continue
        return records
    def count(self):
        with self.db() as c:return c.execute("SELECT (SELECT count(*) FROM runs WHERE status='completed')+(SELECT count(*) FROM activities WHERE status='completed')").fetchone()[0]
    def finish(self,day,record):
        table,column=self.target(day)
        with self.db() as c:c.execute("UPDATE "+table+" SET status='completed',record=?,updated=? WHERE "+column+"=? AND status='started'",(json.dumps(record,ensure_ascii=False),time.time(),day))
    def fail(self,day,error):
        code=failure_code(error)
        table,column=self.target(day)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT day,kind FROM "+table+" WHERE "+column+"=? AND status IN ('pending','started')",(day,)).fetchone()
            if row is None:return
            now=time.time();value=dict(day=row['day'],kind=row['kind'],status='failed',error=code,updated=now)
            if table=='activities':value['activity_id']=day
            key=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()[:24]
            if table=='activities':c.execute('INSERT OR IGNORE INTO activity_failures VALUES(?,?,?,?,?,?,?)',(key,day,row['day'],row['kind'],'failed',code,now))
            else:c.execute('INSERT OR IGNORE INTO failed_attempts VALUES(?,?,?,?,?,?)',(key,row['day'],row['kind'],'failed',code,now))
            c.execute("UPDATE "+table+" SET status='failed',error=?,updated=? WHERE "+column+"=?",(code,now,day))
    def public_context(self):
        for record in self.records(1):
            try:
                path=self.root/record['artifact']
                if path.parent!=self.root/'works' or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=record['sha256']:continue
                if time.time()-record['completed_at']>7*86400 or record['completed_at']>time.time()+5:continue
                return {k:record[k] for k in ('id','day','kind','note','thought','story','project','stage','source','completed_at')}
            except (KeyError,OSError,ValueError):continue
        return None
    def public_context_for_day(self, now=None):
        now = time.time() if now is None else now
        for record in self.records(60):
            try:
                completed = float(record['completed_at'])
                if not 0 < completed <= now or beijing_day(completed) != beijing_day(now):continue
                path = self.root / record['artifact']
                if path.resolve().parent != (self.root/'works').resolve() or not path.is_file():continue
                if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:continue
                value = {k:record[k] for k in ('id','kind','note','thought','story','project','stage','source','completed_at')}
                value['day'] = beijing_day(completed)
                return value
            except (KeyError, OSError, TypeError, ValueError):continue
        return None
    def public_image_for_day(self, now=None):
        public = self.public_context_for_day(now)
        if not public or public['kind'] != 'drawing':return None
        for record in self.records(60):
            if record.get('id') == public['id']:
                return str(self.root/record['artifact'])
        return None
    def cleanup(self):
        keep={r['artifact'] for r in self.records(60)}
        works=self.root/'works'
        if works.exists():
            for p in works.iterdir():
                if p.is_file() and p.relative_to(self.root).as_posix() not in keep:p.unlink()
