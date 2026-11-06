"""Per-person follow-through from verified public comments, with durable send claims."""
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time


class SocialReply(str):
    """A completed decision may deliberately have no public reply."""


def source_key(post_tid,comment_id,parent_id,peer):
    return hashlib.sha256(f'{post_tid}:{parent_id or 0}:{comment_id}:{peer}'.encode()).hexdigest()


class SocialIntentStore:
    def __init__(self,path,clock=time.time):
        self.path,self.clock=Path(path),clock;self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS social_meta(key TEXT PRIMARY KEY,value REAL);
                CREATE TABLE IF NOT EXISTS social_peers(peer TEXT PRIMARY KEY,epoch INTEGER DEFAULT 0,
                    last_in REAL DEFAULT 0,last_out REAL DEFAULT 0,pending_since REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS social_incoming(peer TEXT,mid TEXT,at REAL,PRIMARY KEY(peer,mid));
                CREATE TABLE IF NOT EXISTS social_intents(id TEXT PRIMARY KEY,peer TEXT NOT NULL,source TEXT NOT NULL,
                    requested INTEGER NOT NULL,status TEXT NOT NULL,due REAL NOT NULL,attempts INTEGER DEFAULT 0,
                    failures INTEGER DEFAULT 0,decision TEXT NOT NULL DEFAULT '{}',epoch INTEGER DEFAULT 0,
                    created REAL NOT NULL,updated REAL NOT NULL,error TEXT DEFAULT '',message_id TEXT DEFAULT '');
                CREATE INDEX IF NOT EXISTS social_due ON social_intents(status,due);
            ''')
            db.execute("INSERT OR IGNORE INTO social_meta VALUES ('activated_at',?)",(clock(),))
        self.path.chmod(0o600)

    def recover(self):
        # Only the runtime owner calls recovery, not read/administration clients.
        with self.connect() as db:
            db.execute("UPDATE social_intents SET status='unknown',decision='{}',error='interrupted' WHERE status='sending'")
            db.execute("UPDATE social_intents SET status=CASE WHEN failures<1 THEN 'pending' ELSE 'failed' END,failures=failures+1,due=?,error='interrupted' WHERE status='evaluating'",(self.clock()+3600,))

    @contextmanager
    def connect(self):
        db=sqlite3.connect(str(self.path),timeout=3);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    @staticmethod
    def unpack(row):
        if not row:return None
        value=dict(row);value['source']=json.loads(value['source']);value['decision']=json.loads(value['decision']);return value

    def observe(self,source,requested=False):
        now=self.clock();peer=str(source['peer']);stamp=source['comment_at']
        if (not re.fullmatch(r'[1-9]\d{4,11}',peer) or not source.get('post_tid') or not source.get('comment_id')
                or not isinstance(stamp,(int,float)) or isinstance(stamp,bool) or not math.isfinite(stamp)
                or not 0<stamp<=now+5 or not str(source.get('comment','')).strip()):return None
        key=source_key(source['post_tid'],source['comment_id'],source.get('parent_id'),peer)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT id FROM social_intents WHERE id=?',(key,)).fetchone()
            if old:return key
            if not requested and stamp<=db.execute("SELECT value FROM social_meta WHERE key='activated_at'").fetchone()[0]:return None
            db.execute('INSERT OR IGNORE INTO social_peers(peer) VALUES (?)',(peer,))
            db.execute('UPDATE social_peers SET epoch=epoch+1,pending_since=CASE WHEN pending_since<=? THEN 0 ELSE pending_since END WHERE peer=?',(stamp,peer))
            db.execute('INSERT INTO social_intents(id,peer,source,requested,status,due,created,updated) VALUES (?,?,?,?,?,?,?,?)',
                (key,peer,json.dumps(source,ensure_ascii=False),int(requested),'pending',now,now,now))
        return key

    def get(self,key):
        with self.connect() as db:return self.unpack(db.execute('SELECT * FROM social_intents WHERE id=?',(key,)).fetchone())

    def peer(self,peer):
        with self.connect() as db:
            row=db.execute('SELECT * FROM social_peers WHERE peer=?',(str(peer),)).fetchone()
        return dict(row) if row else dict(peer=str(peer),epoch=0,last_in=0,last_out=0,pending_since=0)

    def incoming(self,peer,mid,at):
        now=self.clock()
        if not mid or not isinstance(at,(int,float)) or not math.isfinite(at) or not now-300<=at<=now+5:return
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('INSERT OR IGNORE INTO social_incoming VALUES (?,?,?)',(str(peer),str(mid),now)).rowcount:return
            db.execute('INSERT OR IGNORE INTO social_peers(peer) VALUES (?)',(str(peer),))
            db.execute('UPDATE social_peers SET epoch=epoch+1,last_in=?,pending_since=CASE WHEN pending_since<=? THEN 0 ELSE pending_since END WHERE peer=?',(now,at,str(peer)))
            db.execute('DELETE FROM social_incoming WHERE at<?',(now-30*86400,))

    def outgoing(self,peer):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO social_peers(peer) VALUES (?)',(str(peer),))
            db.execute('UPDATE social_peers SET epoch=epoch+1,last_out=? WHERE peer=?',(self.clock(),str(peer)))

    def due(self):
        with self.connect() as db:
            return [self.unpack(r) for r in db.execute("SELECT * FROM social_intents WHERE status IN ('pending','deferred','ready') AND due<=? ORDER BY due,created LIMIT 8",(self.clock(),))]

    def claim(self,key,epoch):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT i.*,p.epoch AS current_epoch FROM social_intents i JOIN social_peers p ON p.peer=i.peer WHERE i.id=?',(key,)).fetchone()
            if not row or row['status'] not in ('pending','deferred','ready') or row['due']>self.clock() or row['current_epoch']!=epoch:return False
            db.execute("UPDATE social_intents SET status='evaluating',attempts=attempts+1,epoch=?,updated=? WHERE id=?",(epoch,self.clock(),key));return True

    def decide(self,key,decision,epoch):
        now=self.clock();state='declined' if decision['action']=='none' else 'ready' if decision['action']=='contact' else 'deferred'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT i.status,p.epoch FROM social_intents i JOIN social_peers p ON p.peer=i.peer WHERE i.id=?',(key,)).fetchone()
            if not row or row['status']!='evaluating':return False
            if row['epoch']!=epoch:
                db.execute("UPDATE social_intents SET status='deferred',decision='{}',due=?,updated=?,error='conversation_changed' WHERE id=?",(now+60,now,key));return False
            db.execute('UPDATE social_intents SET status=?,decision=?,due=?,updated=?,error=? WHERE id=?',
                (state,json.dumps(decision,ensure_ascii=False),decision['due'] or now,now,'',key));return True

    def failed(self,key,error):
        with self.connect() as db:
            db.execute("UPDATE social_intents SET status=CASE WHEN failures<1 THEN 'pending' ELSE 'failed' END,failures=failures+1,decision='{}',due=?,updated=?,error=? WHERE id=? AND status='evaluating'",
                (self.clock()+3600,self.clock(),str(error)[:100],key))

    def changed(self,key):
        with self.connect() as db:
            db.execute("UPDATE social_intents SET status='deferred',decision='{}',due=?,updated=?,error='conversation_changed' WHERE id=? AND status='evaluating'",(self.clock()+60,self.clock(),key))

    def active_for(self,peer):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM social_intents WHERE peer=? AND status IN ('pending','evaluating','deferred','ready','sending')",(str(peer),)).fetchone())

    def postpone(self,key,until,reason):
        with self.connect() as db:
            db.execute("UPDATE social_intents SET due=?,updated=?,error=? WHERE id=? AND status IN ('pending','deferred','ready')",(until,self.clock(),reason,key))

    def begin_send(self,key,epoch):
        now=self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT i.*,p.epoch AS current_epoch,p.pending_since FROM social_intents i JOIN social_peers p ON p.peer=i.peer WHERE i.id=?',(key,)).fetchone()
            if not row or row['status']!='ready' or row['current_epoch']!=epoch or row['pending_since'] or row['due']>now:return False
            db.execute("UPDATE social_intents SET status='sending',updated=? WHERE id=?",(now,key))
            db.execute('UPDATE social_peers SET pending_since=?,last_out=?,epoch=epoch+1 WHERE peer=?',(now,now,row['peer']));return True

    def finish_send(self,key,message_id='',error=''):
        with self.connect() as db:
            row=db.execute("SELECT decision FROM social_intents WHERE id=? AND status='sending'",(key,)).fetchone()
            if not row:return
            decision=json.loads(row[0]);decision['private_message']=''
            db.execute('UPDATE social_intents SET status=?,decision=?,message_id=?,error=?,updated=? WHERE id=?',
                ('sent' if message_id else 'unknown',json.dumps(decision,ensure_ascii=False),str(message_id),str(error)[:100],self.clock(),key))

    def cancel(self,key,reason):
        with self.connect() as db:
            db.execute("UPDATE social_intents SET status='cancelled',decision='{}',error=?,updated=? WHERE id=? AND status NOT IN ('sent','sending','unknown')",(reason,self.clock(),key))

    def forget(self,peer):
        with self.connect() as db:
            db.execute("UPDATE social_intents SET status=CASE WHEN status IN ('sent','sending','unknown') THEN status ELSE 'cancelled' END,decision='{}',error='forgotten' WHERE peer=?",(str(peer),))
            db.execute('UPDATE social_peers SET epoch=epoch+1 WHERE peer=?',(str(peer),))

    def status(self):
        with self.connect() as db:
            return dict(states=dict(db.execute('SELECT status,count(*) FROM social_intents GROUP BY status')),
                waiting_contacts=db.execute('SELECT count(*) FROM social_peers WHERE pending_since>0').fetchone()[0],
                model_evaluations=db.execute('SELECT COALESCE(sum(attempts),0) FROM social_intents').fetchone()[0])
