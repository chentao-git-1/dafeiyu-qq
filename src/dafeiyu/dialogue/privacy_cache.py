"""Invalidate untracked derived caches without deleting another person's source rows."""
from contextlib import closing
import sqlite3
import time


def privacy_cutoff(path,origin):
    if not path.is_file():return 0
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.3)) as db:
            db.execute('PRAGMA query_only=ON')
            row=db.execute('SELECT at FROM privacy_barriers WHERE platform=?',(origin.split(':',1)[0],)).fetchone()
            return float(row[0]) if row else 0
    except (sqlite3.Error,ValueError,TypeError):return time.time()
