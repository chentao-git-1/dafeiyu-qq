"""Persistent, source-linked personality versions, separate from identity and permissions."""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import time

TZ=dt.timezone(dt.timedelta(hours=8))
GLOBAL='global'


def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def day(at):return dt.datetime.fromtimestamp(at,TZ).date().isoformat()
def relation(owner,origin):return 'relationship:'+digest([str(owner),origin])[:32]


def own_experiences(store,now=None,retained=()):
    """Only actual private-free activity outcomes and verified owned work; no chat logs."""
    now=time.time() if now is None else now
    if store is None or not getattr(store,'path',None):return []
    root=Path(store.root).resolve();path=Path(store.path)
    if not path.is_file():return []
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3);db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        runs=[dict(r) for r in db.execute('SELECT day,status,kind,record,error,updated FROM runs ORDER BY day DESC LIMIT 16')]
        has_agenda=bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='activities'").fetchone())
        if has_agenda:runs.extend(dict(r) for r in db.execute("SELECT id AS activity_id,day,status,kind,record,error,updated FROM activities WHERE status IN ('completed','failed') ORDER BY updated DESC LIMIT 32"))
        known={r.get('activity_id') or r['day'] for r in runs}
        for old in retained:
            key=old['reference'].get('activity_id') or old['reference'].get('day')
            if key and key not in known:
                if old['reference'].get('activity_id'):
                    row=db.execute('SELECT id AS activity_id,day,status,kind,record,error,updated FROM activities WHERE id=?',(key,)).fetchone() if has_agenda else None
                else:row=db.execute('SELECT day,status,kind,record,error,updated FROM runs WHERE day=?',(key,)).fetchone()
                if row:runs.append(dict(row));known.add(key)
    finally:db.close()
    rows=[]
    for run in runs:
        try:
            if run['status']=='completed':
                record=json.loads(run['record']);artifact=(root/record['artifact']).resolve();at=float(record['completed_at'])
                if artifact.parent!=(root/'works').resolve() or not 0<at<=now+5:continue
                if record['kind'] not in ('reading','writing','drawing'):continue
                exists=artifact.is_file()
                if exists and (artifact.stat().st_size>4*1024*1024 or hashlib.sha256(artifact.read_bytes()).hexdigest()!=record['sha256']):continue
                if exists and artifact.suffix=='.json':
                    content=json.loads(artifact.read_text(encoding='utf8'))
                    if content.get('note')!=record.get('note'):continue
                payload=dict(activity=record['kind'],outcome='completed',note=str(record.get('note',''))[:500],
                    thought=str(record.get('thought',''))[:250],nature='verified_digital_work_not_physical_experience')
                if record.get('adapted_from')=='reading':payload['activity_context']='alternative_after_failed_reading'
                reference=dict(day=run['day'],status='completed',sha256=record['sha256'],completed_at=at)
                if run.get('activity_id'):reference['activity_id']=run['activity_id']
                reflection=record.get('reflection')
                if (isinstance(reflection,dict) and isinstance(reflection.get('evidence'),str)
                        and reflection['evidence'] and reflection['evidence'] in record.get('note','')):
                    payload['reflection']={k:str(reflection.get(k,''))[:250] for k in ('insight','next_question')}
                # Normal work-file retention must not erase long-term dispositions.
                # Only a previously witnessed attestation with unchanged metadata may survive cleanup.
                if not exists and not any(old['reference']==reference and old['payload']==payload for old in retained):continue
            elif run['status']=='failed' and run['kind'] in ('reading','writing','drawing'):
                at=float(run['updated'])
                if not 0<at<=now+5:continue
                payload=dict(activity=run['kind'],outcome='not_completed',error_type=str(run['error'] or 'unknown')[:80])
                reference=dict(day=run['day'],status='failed',kind=run['kind'],error=run['error'],updated=at)
                if run.get('activity_id'):reference['activity_id']=run['activity_id']
            else:continue
            rows.append(dict(scope=GLOBAL,owner='',origin='',episode='own:'+(run.get('activity_id') or run['day']),at=at,day=day(at),
                reference=reference,payload=payload))
        except (OSError,ValueError,TypeError,KeyError):continue
    return rows


def preference_experiences(learning,owner,origin,now=None,retained=()):
    """Confirmed, independently verified preferences stay in this one private relation."""
    now=time.time() if now is None else now
    if ':FriendMessage:' not in origin or origin.rsplit(':',1)[-1]!=str(owner):return []
    result=[]
    with learning.db() as db:
        columns="SELECT l.id,l.version,l.payload,l.source_id,s.human_hash,s.at FROM learned l "
        query=(columns+
            "JOIN learning_sources s ON l.source_id=s.id WHERE l.owner=? AND l.origin=? AND l.kind='preference' "
            "AND l.status='current' AND s.revoked=0 AND s.owner=l.owner AND s.origin=l.origin "
            "ORDER BY l.updated DESC LIMIT 32")
        rows=list(db.execute(query,(str(owner),origin)));found={r['id'] for r in rows}
        for old in retained:
            eid=old['reference'].get('entity_id')
            if eid and eid not in found:
                row=db.execute(columns+"JOIN learning_sources s ON l.source_id=s.id WHERE l.id=? AND l.owner=? AND l.origin=? "
                    "AND l.kind='preference' AND l.status='current' AND s.revoked=0 AND s.owner=l.owner AND s.origin=l.origin",
                    (eid,str(owner),origin)).fetchone()
                if row:rows.append(row);found.add(eid)
    for row in rows:
        value=json.loads(row['payload']);at=float(row['at'])
        if not 0<at<=now+5:continue
        result.append(dict(scope=relation(owner,origin),owner=str(owner),origin=origin,episode=row['source_id'],at=at,day=day(at),
            reference=dict(entity_id=row['id'],version=row['version'],source_id=row['source_id'],human_hash=row['human_hash']),
            payload=dict(kind='confirmed_interaction_preference',summary=value['summary'],condition=value['condition'])))
    return result


class GrowthStore:
    def __init__(self,path,clock=time.time):
        self.path,self.clock=Path(path),clock;self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS growth_scopes(scope TEXT PRIMARY KEY,owner TEXT,origin TEXT,epoch INTEGER,revision INTEGER);
                CREATE TABLE IF NOT EXISTS growth_experiences(id TEXT PRIMARY KEY,scope TEXT,episode TEXT,at REAL,day TEXT,reference TEXT,payload TEXT,
                    UNIQUE(scope,episode));
                CREATE TABLE IF NOT EXISTS growth_reviews(id TEXT PRIMARY KEY,scope TEXT,day TEXT,status TEXT,epoch INTEGER,revision INTEGER,
                    evidence_ids TEXT,at REAL,error TEXT);
                CREATE TABLE IF NOT EXISTS growth_versions(scope TEXT,version INTEGER,traits TEXT,sources TEXT,reason TEXT,at REAL,action TEXT,
                    PRIMARY KEY(scope,version));
                CREATE TABLE IF NOT EXISTS growth_budget(day TEXT PRIMARY KEY,count INTEGER);
                CREATE TABLE IF NOT EXISTS growth_cooldowns(scope TEXT PRIMARY KEY,at REAL);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(str(self.path),timeout=3);db.row_factory=sqlite3.Row;db.execute('PRAGMA secure_delete=ON')
        try:
            with db:yield db
        finally:db.close()

    @staticmethod
    def _scope(db,scope,owner='',origin=''):
        db.execute('INSERT OR IGNORE INTO growth_scopes VALUES(?,?,?,0,0)',(scope,owner,origin))
        if owner:db.execute('UPDATE growth_scopes SET owner=?,origin=? WHERE scope=?',(owner,origin,scope))
        return dict(db.execute('SELECT * FROM growth_scopes WHERE scope=?',(scope,)).fetchone())

    @staticmethod
    def _current(db,scope):
        meta=db.execute('SELECT * FROM growth_scopes WHERE scope=?',(scope,)).fetchone()
        ids={r[0] for r in db.execute('SELECT id FROM growth_experiences WHERE scope=?',(scope,))}
        for row in db.execute('SELECT * FROM growth_versions WHERE scope=? ORDER BY version DESC',(scope,)):
            if set(json.loads(row['sources']))<=ids:
                return dict(revision=meta['revision'],version=row['version'],tendencies=json.loads(row['traits']))
        return dict(revision=meta['revision'] if meta else 0,version=0,tendencies=[])

    @staticmethod
    def _remove(db,scope,ids):
        if not ids:return
        for row in list(db.execute('SELECT version,sources FROM growth_versions WHERE scope=?',(scope,))):
            if set(json.loads(row['sources'])) & ids:db.execute('DELETE FROM growth_versions WHERE scope=? AND version=?',(scope,row['version']))
        db.executemany('DELETE FROM growth_experiences WHERE id=?',[(i,) for i in ids])
        db.execute('UPDATE growth_scopes SET epoch=epoch+1,revision=revision+1 WHERE scope=?',(scope,))

    def epoch(self,scope,owner='',origin=''):
        with self.db() as db:return self._scope(db,scope,owner,origin)['epoch']

    def reconcile(self,scope,rows,owner='',origin='',expected_epoch=None):
        assert all(r['scope']==scope and r['owner']==owner and r['origin']==origin for r in rows)
        prepared={}
        for row in rows:
            value=dict(row);value['id']='experience:'+digest([scope,row['episode'],row['reference'],row['payload']])[:32]
            prepared[value['id']]=value
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            meta=db.execute('SELECT epoch FROM growth_scopes WHERE scope=?',(scope,)).fetchone()
            if expected_epoch is not None and (meta['epoch'] if meta else 0)!=expected_epoch:return self._current(db,scope)
            self._scope(db,scope,owner,origin)
            old={r[0] for r in db.execute('SELECT id FROM growth_experiences WHERE scope=?',(scope,))}
            self._remove(db,scope,old-set(prepared))
            for value in prepared.values():
                db.execute('INSERT OR IGNORE INTO growth_experiences VALUES(?,?,?,?,?,?,?)',
                    (value['id'],scope,value['episode'],value['at'],value['day'],json.dumps(value['reference'],ensure_ascii=False),
                     json.dumps(value['payload'],ensure_ascii=False)))
            return self._current(db,scope)

    def scopes(self):
        with self.db() as db:return [dict(r) for r in db.execute("SELECT * FROM growth_scopes WHERE scope='global' OR owner<>''")]

    def retained(self,scope):
        with self.db() as db:
            ids={i for r in db.execute('SELECT sources FROM growth_versions WHERE scope=? ORDER BY version DESC LIMIT 12',(scope,))
                for i in json.loads(r['sources'])}
            return [dict(id=r['id'],reference=json.loads(r['reference']),payload=json.loads(r['payload'])) for r in
                db.execute('SELECT id,reference,payload FROM growth_experiences WHERE scope=?',(scope,)) if r['id'] in ids]

    def current(self,scope):
        with self.db() as db:return self._current(db,scope)

    def claim(self,scope):
        now=self.clock()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');meta=self._scope(db,scope);current=self._current(db,scope)
            last=db.execute('SELECT * FROM growth_reviews WHERE scope=? ORDER BY at DESC LIMIT 1',(scope,)).fetchone()
            cooldown=db.execute('SELECT at FROM growth_cooldowns WHERE scope=?',(scope,)).fetchone()
            if cooldown and now-cooldown['at']<86400:return None
            budget=db.execute('SELECT count FROM growth_budget WHERE day=?',(day(now),)).fetchone()
            if budget and budget['count']>=2:return None
            used=set(json.loads(last['evidence_ids'])) if last else set()
            all_rows=[dict(r) for r in db.execute('SELECT * FROM growth_experiences WHERE scope=? ORDER BY at DESC LIMIT 32',(scope,))]
            fresh=[r for r in all_rows if r['id'] not in used]
            if (len({r['episode'] for r in fresh})<3 or len({r['day'] for r in fresh})<2
                    or max((r['at'] for r in fresh),default=0)-min((r['at'] for r in fresh),default=0)<86400):return None
            key='growth-review:'+digest([scope,meta['epoch'],current['revision'],now])[:32]
            ids=[r['id'] for r in all_rows]
            db.execute('INSERT INTO growth_reviews VALUES(?,?,?,?,?,?,?,?,?)',
                (key,scope,day(now),'pending',meta['epoch'],current['revision'],json.dumps(ids),now,''))
            db.execute('INSERT INTO growth_budget VALUES(?,1) ON CONFLICT(day) DO UPDATE SET count=count+1',(day(now),))
            db.execute('INSERT OR REPLACE INTO growth_cooldowns VALUES(?,?)',(scope,now))
            return dict(id=key,scope=scope,epoch=meta['epoch'],revision=current['revision'],
                packet=dict(scope='global' if scope==GLOBAL else 'relationship',current=current,
                    new_evidence_ids=[r['id'] for r in fresh],experiences=[
                        {k:r[k] for k in ('id','episode','at','day')}|{'details':json.loads(r['payload'])} for r in all_rows]))

    def finish(self,claim,proposal=None,accepted=False,error=''):
        if accepted and proposal:
            from .growth_policy import parse_proposal
            proposal=parse_proposal(json.dumps(proposal,ensure_ascii=False),claim['packet'])
        now=self.clock();scope=claim['scope']
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');meta=self._scope(db,scope)
            review=db.execute('SELECT status FROM growth_reviews WHERE id=?',(claim['id'],)).fetchone()
            if not review or review['status']!='pending':return False
            if (meta['epoch']!=claim['epoch'] or meta['revision']!=claim['revision']):
                db.execute("UPDATE growth_reviews SET status='stale' WHERE id=?",(claim['id'],));return False
            state='failed' if error else ('held' if proposal and proposal['decision']=='hold' else 'rejected')
            if accepted and proposal and proposal['decision']=='update':
                ids=set(proposal['evidence_ids'])
                actual={r[0] for r in db.execute('SELECT id FROM growth_experiences WHERE scope=?',(scope,))}
                if not ids<=actual:raise ValueError('Personality evidence changed')
                traits=self._current(db,scope)['tendencies'];target=proposal['target_id']
                if target and target not in {t['id'] for t in traits}:raise ValueError('Personality target changed')
                if not target and len(traits)>=4:raise ValueError('Personality disposition budget exceeded')
                trait={k:proposal[k] for k in ('tendency','when','counterweight','evidence_ids')}
                trait['id']=target or 'disposition:'+digest([scope,claim['id']])[:20]
                traits=[t for t in traits if t['id']!=target]+[trait]
                sources=sorted({i for t in traits for i in t['evidence_ids']})
                version=meta['revision']+1
                db.execute('INSERT INTO growth_versions VALUES(?,?,?,?,?,?,?)',
                    (scope,version,json.dumps(traits,ensure_ascii=False),json.dumps(sources),proposal['reason'],now,'growth'))
                db.execute('UPDATE growth_scopes SET revision=? WHERE scope=?',(version,scope));state='applied'
                db.execute('DELETE FROM growth_versions WHERE scope=? AND version NOT IN '
                    '(SELECT version FROM growth_versions WHERE scope=? ORDER BY version DESC LIMIT 12)',(scope,scope))
            db.execute('UPDATE growth_reviews SET status=?,error=? WHERE id=?',(state,error[:100],claim['id']))
            return state=='applied'

    def forget(self,owner):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            scopes=[r[0] for r in db.execute('SELECT scope FROM growth_scopes WHERE owner=?',(str(owner),))]
            for scope in scopes:
                for table in ('growth_versions','growth_experiences','growth_reviews'):
                    db.execute('DELETE FROM '+table+' WHERE scope=?',(scope,))
                db.execute("UPDATE growth_scopes SET owner='',origin='',epoch=epoch+1,revision=revision+1 WHERE scope=?",(scope,))
            return len(scopes)

    def rollback(self,scope,expected,target):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');meta=self._scope(db,scope)
            if meta['revision']!=expected:raise ValueError('Personality version changed')
            traits=[];sources=[]
            if target:
                row=db.execute('SELECT * FROM growth_versions WHERE scope=? AND version=?',(scope,target)).fetchone()
                if not row:raise ValueError('Unknown personality version')
                traits=json.loads(row['traits']);sources=json.loads(row['sources'])
                actual={r[0] for r in db.execute('SELECT id FROM growth_experiences WHERE scope=?',(scope,))}
                if not set(sources)<=actual:raise ValueError('Rollback sources no longer available')
            version=meta['revision']+1;now=self.clock()
            db.execute('INSERT INTO growth_versions VALUES(?,?,?,?,?,?,?)',
                (scope,version,json.dumps(traits,ensure_ascii=False),json.dumps(sources),'Explicitly requested rollback',now,'rollback'))
            db.execute('UPDATE growth_scopes SET epoch=epoch+1,revision=? WHERE scope=?',(version,scope))
            ids=[r[0] for r in db.execute('SELECT id FROM growth_experiences WHERE scope=?',(scope,))]
            db.execute('INSERT INTO growth_reviews VALUES(?,?,?,?,?,?,?,?,?)',
                ('rollback:'+digest([scope,version]),scope,day(now),'manual',meta['epoch']+1,version,json.dumps(ids),now,''))
            db.execute('INSERT OR REPLACE INTO growth_cooldowns VALUES(?,?)',(scope,now))
            return version

    def history(self,scope):
        with self.db() as db:
            return [dict(version=r['version'],at=r['at'],action=r['action'],reason=r['reason'],
                tendencies=json.loads(r['traits'])) for r in db.execute(
                    'SELECT * FROM growth_versions WHERE scope=? ORDER BY version DESC LIMIT 12',(scope,))]

    def status(self):
        with self.db() as db:
            return dict(experiences=db.execute('SELECT count(*) FROM growth_experiences').fetchone()[0],
                active_scopes=db.execute("SELECT count(*) FROM growth_scopes WHERE scope='global' OR owner<>''").fetchone()[0],
                applied_versions=db.execute("SELECT count(*) FROM growth_versions WHERE action='growth'").fetchone()[0],
                reviews={r[0]:r[1] for r in db.execute('SELECT status,count(*) FROM growth_reviews GROUP BY status')})


def perspective(state):
    return dict(version=state['version'],tendencies=[{k:t[k] for k in ('tendency','when','counterweight')} for t in state['tendencies']])
