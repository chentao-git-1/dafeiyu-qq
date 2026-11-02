"""Rebuildable lexical source index. Stores hashes/term hashes, never old source bodies."""
from contextlib import closing
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from .retrieval import terms

VERSION = 'lexical-cjk-2-3-v1'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SourceIndex:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(self.path), timeout=3)) as db, db:
            db.executescript('CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY,value TEXT);'
                'CREATE TABLE IF NOT EXISTS indexed(scope TEXT,id TEXT,signature TEXT,terms TEXT,PRIMARY KEY(scope,id));')
        self.path.chmod(0o600)

    @staticmethod
    def scope(turn):
        return fingerprint([turn.actor, turn.origin, turn.platform])

    def sync(self, turn, items, provider_version='none'):
        scope = self.scope(turn)
        rows = [item for item in items if item.visible_to(turn)]
        version = VERSION + ':' + str(provider_version)
        with closing(sqlite3.connect(str(self.path), timeout=3)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute("SELECT value FROM index_meta WHERE key='version'").fetchone()
            if not prior or prior[0] != version:
                db.execute('DELETE FROM indexed')
                db.execute("INSERT OR REPLACE INTO index_meta VALUES('version',?)", (version,))
            offered = {row.id for row in rows}
            for (eid,) in db.execute('SELECT id FROM indexed WHERE scope=?', (scope,)).fetchall():
                if eid not in offered:
                    db.execute('DELETE FROM indexed WHERE scope=? AND id=?', (scope, eid))
            for row in rows:
                signature = fingerprint(row.data())
                old = db.execute('SELECT signature FROM indexed WHERE scope=? AND id=?', (scope, row.id)).fetchone()
                if old and old[0] == signature:
                    continue
                tokens = [fingerprint(word) for word in terms(row.text)]
                db.execute('INSERT OR REPLACE INTO indexed VALUES(?,?,?,?)',
                           (scope, row.id, signature, json.dumps(tokens)))
        return rows

    def rank(self, turn, query, items, limit=16):
        keys = {fingerprint(t) for t in terms(query)}
        current = {row.id: row for row in items if row.visible_to(turn)}
        with closing(sqlite3.connect(str(self.path), timeout=3)) as db:
            cached = db.execute('SELECT id,signature,terms FROM indexed WHERE scope=?', (self.scope(turn),)).fetchall()
        docs = [(current[eid], set(json.loads(words))) for eid, sig, words in cached
                if eid in current and fingerprint(current[eid].data()) == sig]
        df = Counter(key for _, words in docs for key in words)
        scored = []
        for row, words in docs:
            common = keys & words
            if common:
                score = sum(math.log1p((len(docs) + 1) / (df[k] + .5)) for k in common) / math.sqrt(max(1, len(keys)))
                scored.append((score, row.recorded_at, row.id, row))
        scored.sort(key=lambda r: r[:3], reverse=True)
        return [row[-1] for row in scored[:limit]]

    def invalidate(self, ids):
        with closing(sqlite3.connect(str(self.path), timeout=3)) as db, db:
            db.executemany('DELETE FROM indexed WHERE id=?', [(eid,) for eid in ids])
