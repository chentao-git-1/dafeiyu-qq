"""Durable, owner-only initiative slots and reply gates; no transport here."""
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import random
import sqlite3
import time

TZ=dt.timezone(dt.timedelta(hours=8))
WINDOWS=((10,0,12,0),(18,0,20,30))

class InitiativeStore:
    def __init__(self,path,clock=time.time,rng=None):
        self.path=Path(path);self.clock=clock;self.rng=rng or random.SystemRandom()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY,morning REAL,evening REAL);
                CREATE TABLE IF NOT EXISTS attempts(day TEXT,slot INTEGER,source TEXT UNIQUE,kind TEXT,
                    status TEXT,at REAL,body TEXT,error TEXT,PRIMARY KEY(day,slot));
                CREATE TABLE IF NOT EXISTS incoming(mid TEXT PRIMARY KEY,at REAL);
                CREATE TABLE IF NOT EXISTS evaluations(day TEXT,slot INTEGER,epoch INTEGER,status TEXT,at REAL,PRIMARY KEY(day,slot));''')
            for key,value in {'activated_at':clock(),'last_activity':clock(),'last_push':0,'pending_at':0,'epoch':0}.items():
                db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',(key,json.dumps(value)))
            db.execute("UPDATE attempts SET status='unknown',error='interrupted' WHERE status IN ('generating','sending')")
        self.path.chmod(0o600)
    @contextmanager
    def connect(self):
        db=sqlite3.connect(str(self.path),timeout=3);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    @staticmethod
    def get(db,key):
        row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else 0
    @staticmethod
    def set(db,key,value):db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value)))
    @staticmethod
    def day(now):return dt.datetime.fromtimestamp(now,TZ).date().isoformat()
    @staticmethod
    def edges(day,slot):
        date=dt.date.fromisoformat(day);a,b,c,d=WINDOWS[slot]
        return (dt.datetime.combine(date,dt.time(a,b),TZ).timestamp(),dt.datetime.combine(date,dt.time(c,d),TZ).timestamp())
    def plan(self,now=None):
        now=self.clock() if now is None else now;day=self.day(now)
        with self.connect() as db:
            row=db.execute('SELECT * FROM days WHERE day=?',(day,)).fetchone()
            if row:return dict(row)
            slots=[]
            for slot in range(2):
                start,end=self.edges(day,slot);start=max(start,now+120)
                slots.append(self.rng.uniform(start,end) if start<end else -1)
            db.execute('INSERT OR IGNORE INTO days VALUES (?,?,?)',(day,*slots))
            return dict(db.execute('SELECT * FROM days WHERE day=?',(day,)).fetchone())
    def due(self,now=None):
        now=self.clock() if now is None else now;plan=self.plan(now)
        with self.connect() as db:
            used={r[0] for r in db.execute('SELECT slot FROM attempts WHERE day=?',(plan['day'],))}
        for slot,name in enumerate(('morning','evening')):
            if slot not in used and plan[name]>0 and plan[name]<=now<self.edges(plan['day'],slot)[1]:return (plan['day'],slot)
        return None
    def _automatic_care_ready(self,db,now):
        return not self.get(db,'pending_at') and now-self.get(db,'last_push')>=14400
    def _eligible(self,db,now):
        return (self._automatic_care_ready(db,now) and now-self.get(db,'last_activity')>=3600
            and db.execute('SELECT count(*) FROM attempts WHERE day=?',(self.day(now),)).fetchone()[0]<2)
    def eligible(self):
        with self.connect() as db:return self._eligible(db,self.clock())
    def epoch(self):
        with self.connect() as db:return self.get(db,'epoch')
    def used(self,source):
        with self.connect() as db:return db.execute('SELECT 1 FROM attempts WHERE source=?',(source,)).fetchone() is not None
    def reserve(self,day,slot,source,kind,epoch):
        now=self.clock()
        if slot not in (0,1) or day!=self.day(now):return False
        plan=self.plan(now);when=plan[('morning','evening')[slot]]
        if not 0<when<=now<self.edges(day,slot)[1]:return False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if day!=self.day(now) or not self._eligible(db,now) or self.get(db,'epoch')!=epoch:return False
            if kind=='care' and db.execute("SELECT 1 FROM attempts WHERE day=? AND kind='care'",(day,)).fetchone():return False
            try:db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)',(day,slot,source,kind,'generating',now,None,None))
            except sqlite3.IntegrityError:return False
        return True

    def claim_evaluation(self,day,slot,epoch):
        now=self.clock()
        if slot not in (0,1) or day!=self.day(now) or now>=self.edges(day,slot)[1]:return False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.get(db,'epoch')!=epoch or not self._eligible(db,now):return False
            return db.execute('INSERT OR IGNORE INTO evaluations VALUES(?,?,?,?,?)',
                (day,slot,epoch,'started',now)).rowcount==1

    def finish_evaluation(self,day,slot,status):
        with self.connect() as db:db.execute('UPDATE evaluations SET status=? WHERE day=? AND slot=?',(status,day,slot))

    def recent_sent(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT source,body,at FROM attempts WHERE status='sent' ORDER BY at DESC LIMIT 4")]
    def begin_send(self,day,slot,epoch,body):
        now=self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status FROM attempts WHERE day=? AND slot=?',(day,slot)).fetchone()
            # Reservation already counts toward the daily cap, so this check is
            # intentionally separate from _eligible().
            if (day!=self.day(now) or now>=self.edges(day,slot)[1] or not row or row[0]!='generating' or self.get(db,'epoch')!=epoch
                    or not self._automatic_care_ready(db,now) or now-self.get(db,'last_activity')<3600):return False
            db.execute("UPDATE attempts SET status='sending',body=? WHERE day=? AND slot=?",(body,day,slot))
            self.set(db,'pending_at',now);self.set(db,'last_activity',now);self.set(db,'last_push',now)
        return True
    def finish(self,day,slot,status,error=''):
        with self.connect() as db:db.execute('UPDATE attempts SET status=?,error=? WHERE day=? AND slot=?',(status,error[:100],day,slot))
    def observe_incoming(self,mid,stamp):
        now=self.clock()
        if not mid or not isinstance(stamp,(int,float)) or stamp>now+300:return False
        with self.connect() as db:
            if not db.execute('INSERT OR IGNORE INTO incoming VALUES (?,?)',(str(mid),now)).rowcount:return False
            self.set(db,'last_activity',now);self.set(db,'epoch',self.get(db,'epoch')+1)
            if int(stamp)>=int(self.get(db,'pending_at')):self.set(db,'pending_at',0)
            db.execute('DELETE FROM incoming WHERE at<?',(now-30*86400,))
            db.execute('DELETE FROM incoming WHERE mid NOT IN (SELECT mid FROM incoming ORDER BY at DESC LIMIT 2000)')
        return True
    def observe_outgoing(self):
        with self.connect() as db:
            self.set(db,'last_activity',self.clock());self.set(db,'epoch',self.get(db,'epoch')+1)
    def automatic_care_ready(self):
        with self.connect() as db:
            return self._automatic_care_ready(db,self.clock())
    def claim_automatic_care(self):
        now=self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self._automatic_care_ready(db,now):return False
            self.set(db,'pending_at',now);self.set(db,'last_activity',now);self.set(db,'last_push',now)
        return True
    def diagnose(self,kind,reason=''):
        with self.connect() as db:
            if kind=='check':self.set(db,'last_check_at',self.clock())
            elif kind=='skip':
                self.set(db,'last_skip_reason',reason[:100]);self.set(db,'last_skip_at',self.clock())
                self.set(db,'skip_count',self.get(db,'skip_count')+1)
            elif kind=='error':
                self.set(db,'last_error_type',reason[:100]);self.set(db,'last_error_at',self.clock())
                self.set(db,'error_count',self.get(db,'error_count')+1)
    def status(self):
        with self.connect() as db:
            day=self.day(self.clock())
            sent=db.execute("SELECT count(*) FROM attempts WHERE day=? AND status='sent'",(day,)).fetchone()[0]
            attempted=db.execute('SELECT count(*) FROM attempts WHERE day=?',(day,)).fetchone()[0]
            last=db.execute('SELECT status,error FROM attempts ORDER BY at DESC LIMIT 1').fetchone()
            return {'spontaneous_enabled':True,'daily_limit':2,'sent_today':sent,'attempts_today':attempted,
                'awaiting_reply':bool(self.get(db,'pending_at')),'windows_beijing':['10:00-12:00','18:00-20:30'],
                'last_check_at':self.get(db,'last_check_at'),'last_skip_reason':self.get(db,'last_skip_reason'),'last_skip_at':self.get(db,'last_skip_at'),
                'skip_count':self.get(db,'skip_count'),'last_error_type':self.get(db,'last_error_type'),
                'last_error_at':self.get(db,'last_error_at'),'error_count':self.get(db,'error_count'),
                'last_attempt_status':last['status'] if last else None,'last_attempt_error':last['error'] if last else None}
