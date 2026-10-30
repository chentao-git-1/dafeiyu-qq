"""Versioned, source-bound learning. No provider calls, scheduling or transport."""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import time

TZ = dt.timezone(dt.timedelta(hours=8))
KINDS = {'preference', 'event', 'fact'}
STATES = {'planned', 'ongoing', 'resolved', 'cancelled', 'active'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def proposals(value, human):
    if not isinstance(value, dict) or set(value) != {'privacy', 'items'}:
        raise ValueError('Invalid learning container')
    privacy, items = value['privacy'], value['items']
    if (not isinstance(privacy, dict) or set(privacy) != {'action', 'evidence', 'ids', 'subject', 'modality'}
            or privacy['action'] not in ('none', 'deny_turn', 'forget_selected', 'forget_all')
            or not isinstance(privacy['evidence'], str) or not isinstance(privacy['ids'], list)
            or len(privacy['ids']) > 50 or any(not isinstance(i, str) for i in privacy['ids'])):
        raise ValueError('Invalid privacy proposal')
    if privacy['action'] != 'none' and (not privacy['evidence'] or privacy['evidence'] not in human):
        raise ValueError('Privacy instruction has no current source')
    if (privacy['subject'], privacy['modality']) != ('speaker', 'direct'):
        privacy = {'action': 'none', 'evidence': '', 'ids': [], 'subject': 'speaker', 'modality': 'direct'}
    if privacy['action'] != 'forget_selected' and privacy['ids']:
        raise ValueError('Unexpected privacy target')
    if privacy['action'] == 'forget_selected' and not privacy['ids']:
        raise ValueError('Forgetting requires explicit offered IDs')
    if not isinstance(items, list) or len(items) > 5 or (privacy['action'] != 'none' and items):
        raise ValueError('Invalid or refused learning')
    accepted = []
    fields = {'kind', 'summary', 'condition', 'evidence', 'subject', 'modality', 'retention',
              'status', 'event_day', 'supersedes', 'expected_version'}
    for item in items:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError('Unexpected learning fields')
        if (item['kind'] not in KINDS or item['status'] not in STATES
                or any(not isinstance(item[k], str) for k in fields - {'expected_version'})
                or not 2 <= len(item['summary']) <= 400 or len(item['condition']) > 400
                or not 2 <= len(item['evidence']) <= 1200 or item['evidence'] not in human
                or type(item['expected_version']) is not int or item['expected_version'] < 0):
            raise ValueError('Invalid learning evidence')
        if item['kind'] == 'event' and item['status'] == 'active':
            raise ValueError('Event requires a temporal state')
        if item['kind'] != 'event' and item['status'] != 'active':
            raise ValueError('Only events carry progress')
        if item['event_day']:
            dt.date.fromisoformat(item['event_day'])
        # Subject/modal/retention decisions come from the main semantic generation.
        if (item['subject'], item['modality'], item['retention']) != ('speaker', 'direct', 'persistent'):
            continue
        accepted.append(item)
    return privacy, accepted


class LearningStore:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS learning_meta(owner TEXT PRIMARY KEY,epoch INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS learning_sources(
                    id TEXT PRIMARY KEY,owner TEXT,origin TEXT,mid TEXT,human_hash TEXT,epoch INTEGER,at REAL,revoked INTEGER DEFAULT 0,
                    UNIQUE(owner,origin,mid));
                CREATE TABLE IF NOT EXISTS learned(
                    id TEXT PRIMARY KEY,owner TEXT,origin TEXT,kind TEXT,payload TEXT,source_id TEXT,
                    version INTEGER,status TEXT,visibility TEXT,updated REAL,occurred_at TEXT);
                CREATE INDEX IF NOT EXISTS learned_owner_status ON learned(owner,status,updated);
                CREATE TABLE IF NOT EXISTS learned_versions(
                    entity_id TEXT,version INTEGER,payload TEXT,source_id TEXT,status TEXT,updated REAL,
                    PRIMARY KEY(entity_id,version));
                CREATE TABLE IF NOT EXISTS learning_commits(source_id TEXT PRIMARY KEY,proposal_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS context_privacy_clock(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER);
                INSERT OR IGNORE INTO context_privacy_clock VALUES(1,0);
                CREATE TABLE IF NOT EXISTS context_epochs(origin TEXT PRIMARY KEY,epoch INTEGER);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(str(self.path), timeout=3)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _epoch(db, owner):
        row = db.execute('SELECT epoch FROM learning_meta WHERE owner=?', (owner,)).fetchone()
        return int(row[0]) if row else 0

    def begin(self, turn, human):
        if not turn.message_id:
            raise ValueError('Learning requires a stable source message ID')
        sid = 'source:' + digest([turn.actor, turn.origin, turn.message_id])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM learning_sources WHERE id=?', (sid,)).fetchone()
            if old and old['human_hash'] != digest(human):
                raise ValueError('A source message changed')
            if old:
                return dict(old)
            epoch = self._epoch(db, turn.actor)
            db.execute('INSERT INTO learning_sources VALUES(?,?,?,?,?,?,?,0)',
                       (sid, turn.actor, turn.origin, turn.message_id, digest(human), epoch, turn.observed_at))
            return dict(db.execute('SELECT * FROM learning_sources WHERE id=?', (sid,)).fetchone())

    def current(self, owner, origin, include_events=True):
        with self.db() as db:
            rows = db.execute("SELECT l.* FROM learned l JOIN learning_sources s ON s.id=l.source_id "
                "WHERE l.owner=? AND l.status='current' AND s.revoked=0 AND "
                "((l.visibility='shared' AND instr(l.origin,?)=1) OR l.origin=?) ORDER BY l.updated DESC LIMIT 200",
                (owner,origin.split(':',1)[0]+':',origin)).fetchall()
        return [self.view(dict(row)) for row in rows if include_events or row['kind'] == 'preference']

    @staticmethod
    def view(row):
        return {k: row[k] for k in ('id', 'owner', 'origin', 'kind', 'version', 'visibility', 'updated', 'occurred_at')} | json.loads(row['payload'])

    def apply_privacy(self, source, privacy, offered_ids):
        action = privacy['action']
        if action == 'none':
            return 0
        selected = set(privacy['ids'])
        if not selected.issubset(set(offered_ids)):
            raise ValueError('Privacy target was not offered')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM learning_sources WHERE id=?', (source['id'],)).fetchone()
            if not row or row['owner'] != source['owner']:
                raise ValueError('Unknown privacy source')
            if row['revoked']:
                return 0
            epoch = self._epoch(db, source['owner']) + 1
            db.execute('INSERT OR REPLACE INTO learning_meta VALUES(?,?)', (source['owner'], epoch))
            db.execute('UPDATE learning_sources SET revoked=1 WHERE id=?', (source['id'],))
            if action == 'deny_turn':
                return 0
            # Legacy native histories lack source lineage; invalidate their disposable AI context.
            db.execute('UPDATE context_privacy_clock SET epoch=epoch+1 WHERE id=1')
            rows = db.execute('SELECT id,source_id FROM learned WHERE owner=?', (source['owner'],)).fetchall()
            ids = [r['id'] for r in rows if action == 'forget_all' or r['id'] in selected]
            for eid in ids:
                # Remove history and quoted evidence as well as the current projection.
                db.execute('DELETE FROM learned_versions WHERE entity_id=?', (eid,))
                db.execute('DELETE FROM learned WHERE id=?', (eid,))
            db.execute('DELETE FROM learning_commits WHERE source_id IN (SELECT id FROM learning_sources WHERE owner=?)', (source['owner'],))
            return len(ids)

    def context_epoch(self):
        with self.db() as db:return db.execute('SELECT epoch FROM context_privacy_clock WHERE id=1').fetchone()[0]

    def filter_context(self,origin,messages):
        with self.db() as db:
            epoch=db.execute('SELECT epoch FROM context_privacy_clock WHERE id=1').fetchone()[0]
            old=db.execute('SELECT epoch FROM context_epochs WHERE origin=?',(origin,)).fetchone()
        return ([] if (old[0] if old else 0)!=epoch else list(messages or [])),epoch

    def acknowledge_context(self,origin,epoch):
        with self.db() as db:
            if epoch==db.execute('SELECT epoch FROM context_privacy_clock WHERE id=1').fetchone()[0]:
                db.execute('INSERT OR REPLACE INTO context_epochs VALUES(?,?)',(origin,epoch))

    def commit(self, source, human, items, offered_versions, *, confirmed=False):
        if not confirmed or not items:
            return []
        if source['human_hash'] != digest(human):
            raise ValueError('Learning source mismatch')
        _, items = proposals({'privacy': {'action': 'none', 'evidence': '', 'ids': [],
            'subject': 'speaker', 'modality': 'direct'}, 'items': items}, human)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM learning_sources WHERE id=?', (source['id'],)).fetchone()
            if (not row or row['revoked'] or row['epoch'] != self._epoch(db, source['owner'])
                    or row['human_hash'] != source['human_hash']):
                return []
            if db.execute('SELECT 1 FROM learning_commits WHERE source_id=?', (source['id'],)).fetchone():
                return []
            saved = []
            for index, item in enumerate(items):
                old = None
                if item['supersedes']:
                    old = db.execute('SELECT * FROM learned WHERE id=?', (item['supersedes'],)).fetchone()
                    expected = offered_versions.get(item['supersedes'])
                    if (not old or old['owner'] != source['owner']
                            or (old['origin']!=source['origin'] and (old['kind']=='preference'
                                or old['origin'].split(':',1)[0]!=source['origin'].split(':',1)[0]))
                            or old['kind'] != item['kind'] or old['status'] != 'current'
                            or expected != item['expected_version'] or old['version'] != expected):
                        continue
                    eid, version = old['id'], old['version'] + 1
                    db.execute('INSERT OR IGNORE INTO learned_versions VALUES(?,?,?,?,?,?)',
                        (eid, old['version'], old['payload'], old['source_id'], old['status'], old['updated']))
                else:
                    if item['expected_version']:
                        continue
                    eid, version = 'learned:' + digest([source['id'], index])[:24], 1
                payload = json.dumps({k: item[k] for k in ('summary', 'condition', 'evidence', 'status', 'event_day')}, ensure_ascii=False)
                visibility = 'session' if item['kind'] == 'preference' else 'shared'
                db.execute('INSERT OR REPLACE INTO learned VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (eid, source['owner'], source['origin'], item['kind'], payload, source['id'], version,
                     'current', visibility, self.clock(), item['event_day']))
                saved.append(eid)
            db.execute('INSERT INTO learning_commits VALUES(?,?)', (source['id'], digest(items)))
            return saved

    def counts(self):
        with self.db() as db:
            return {'sources': db.execute('SELECT count(*) FROM learning_sources').fetchone()[0],
                    'entities': db.execute('SELECT count(*) FROM learned').fetchone()[0],
                    'versions': db.execute('SELECT count(*) FROM learned_versions').fetchone()[0]}

    def forget_entities(self,owner,expected):
        if not expected or len(expected)>50:raise ValueError('Select between 1 and 50 offered records')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for eid,version in expected.items():
                row=db.execute('SELECT owner,version FROM learned WHERE id=?',(eid,)).fetchone()
                if not row or row['owner']!=owner or row['version']!=version:raise ValueError('Owned record changed')
            db.execute('INSERT OR REPLACE INTO learning_meta VALUES(?,?)',(owner,self._epoch(db,owner)+1))
            db.execute('UPDATE context_privacy_clock SET epoch=epoch+1 WHERE id=1')
            for eid in expected:
                db.execute('DELETE FROM learned_versions WHERE entity_id=?',(eid,))
                db.execute('DELETE FROM learned WHERE id=?',(eid,))
            return len(expected)

    def reset_preferences(self,owner):
        with self.db() as db:rows=db.execute("SELECT id,version FROM learned WHERE owner=? AND kind='preference'",(owner,)).fetchall()
        return self.forget_entities(owner,{r['id']:r['version'] for r in rows}) if rows else 0
