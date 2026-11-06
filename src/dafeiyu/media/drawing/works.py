"""Verified generated works, scoped to the requesting person and conversation."""
from contextlib import closing
import datetime as dt
import hashlib
from pathlib import Path
import sqlite3
import time


class WorkHistory:
    def __init__(self, image_dir, clock=time.time):
        self.image_dir=Path(image_dir).resolve();self.path=self.image_dir.parent/'works.db';self.clock=clock

    @staticmethod
    def schema(db):
        db.execute('CREATE TABLE IF NOT EXISTS works(id TEXT PRIMARY KEY,actor TEXT,origin TEXT,artifact TEXT,sha TEXT,description TEXT,completed REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS scopes(actor TEXT,origin TEXT,epoch INTEGER,PRIMARY KEY(actor,origin))')

    def begin(self, actor, origin):
        with closing(sqlite3.connect(str(self.path),timeout=3)) as db,db:
            self.schema(db)
            db.execute('INSERT OR IGNORE INTO scopes VALUES(?,?,0)',(str(actor),str(origin)))
            epoch=db.execute('SELECT epoch FROM scopes WHERE actor=? AND origin=?',(str(actor),str(origin))).fetchone()[0]
        self.path.chmod(0o600)
        return epoch

    def artifact(self, path):
        path=Path(path).resolve()
        if path.parent!=self.image_dir or not path.is_file() or not 0<path.stat().st_size<=8*1024*1024:
            raise ValueError('Work artifact is unavailable')
        return path,hashlib.sha256(path.read_bytes()).hexdigest()

    def record(self, actor, origin, path, description, expected_epoch=0):
        path,sha=self.artifact(path);now=self.clock()
        key='drawing:'+hashlib.sha256((str(actor)+'\n'+str(origin)+'\n'+path.name).encode()).hexdigest()[:24]
        with closing(sqlite3.connect(str(self.path),timeout=3)) as db,db:
            self.schema(db);db.execute('BEGIN IMMEDIATE')
            epoch=db.execute('SELECT epoch FROM scopes WHERE actor=? AND origin=?',(str(actor),str(origin))).fetchone()
            if (epoch[0] if epoch else 0)!=expected_epoch:return None
            db.execute('INSERT OR IGNORE INTO works VALUES(?,?,?,?,?,?,?)',
                (key,str(actor),str(origin),path.name,sha,str(description)[:1500],now))
            db.execute('DELETE FROM works WHERE completed<?',(now-7*86400,))
            db.execute('DELETE FROM works WHERE id IN (SELECT id FROM works ORDER BY completed DESC LIMIT -1 OFFSET 1000)')
        self.path.chmod(0o600)
        return key

    def recent(self, actor, origin):
        if not self.path.is_file():return []
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
            rows=db.execute('SELECT * FROM works WHERE actor=? AND origin=? ORDER BY completed DESC LIMIT 6',
                (str(actor),str(origin))).fetchall()
        result=[]
        for row in rows:
            try:
                if not 0<=self.clock()-row['completed']<=7*86400:continue
                path,sha=self.artifact(self.image_dir/row['artifact'])
                if sha!=row['sha']:continue
                result.append(dict(id=row['id'],kind='drawing',description=row['description'],
                    day=dt.datetime.fromtimestamp(row['completed'],dt.timezone(dt.timedelta(hours=8))).date().isoformat(),
                    completed_at=row['completed'],delivery='sent',visibility='this_person_and_session',
                    description_is_generation_request_not_visual_inspection=True))
            except (OSError,ValueError):continue
        return result

    def forget(self, actor, origin):
        if not self.path.is_file():return 0
        with closing(sqlite3.connect(str(self.path),timeout=3)) as db,db:
            self.schema(db);db.execute('BEGIN IMMEDIATE')
            db.execute('PRAGMA secure_delete=ON')
            db.execute('INSERT OR IGNORE INTO scopes VALUES(?,?,0)',(str(actor),str(origin)))
            db.execute('UPDATE scopes SET epoch=epoch+1 WHERE actor=? AND origin=?',(str(actor),str(origin)))
            return db.execute('DELETE FROM works WHERE actor=? AND origin=?',(str(actor),str(origin))).rowcount
