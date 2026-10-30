"""Read existing stores without creating, pruning, migrating or touching records."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .contracts import CatalogSnapshot, MemoryItem


class MemoryCatalog:
    def __init__(self, root, long_term_path=None, include_long_term=True):
        self.root = Path(root)
        self.long_term_path = Path(long_term_path) if long_term_path else (
            self.root / 'plugin_data/astrbot_plugin_memory_system/memory_system.db')
        self.include_long_term = include_long_term

    @staticmethod
    def _read(path, sql, params):
        if not path.is_file():
            return []
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.3)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            return [dict(row) for row in db.execute(sql, params)]

    def read(self, turn, now):
        items, unavailable = [], []
        sources = (
            ('long_term', self.long_term_path,
             "SELECT * FROM memories WHERE merged_into IS NULL AND visibility='shared' "
             "AND scope_id IN (?,?) ORDER BY updated_at DESC LIMIT 200",
             (turn.platform + ':shared', 'legacy:shared')),
            ('episodes', self.root / 'plugin_data/dafeiyu_natural_chat/episodes.db',
             'SELECT * FROM episodes ORDER BY updated DESC LIMIT 200', ()),
            ('exchanges', self.root / 'plugin_data/dafeiyu_natural_chat/interaction.db',
             'SELECT * FROM exchanges WHERE qq=? AND origin=? AND at BETWEEN ? AND ? '
             'ORDER BY at DESC LIMIT 100', (turn.actor, turn.origin, now - 30 * 86400, now)),
            ('learned', self.root / 'plugin_data/dafeiyu_dialogue/learning.db',
             "SELECT l.* FROM learned l JOIN learning_sources s ON s.id=l.source_id "
             "WHERE l.status='current' AND s.revoked=0 AND "
             "((l.visibility='shared' AND instr(l.origin,?)=1) OR (l.owner=? AND l.origin=?)) "
             "ORDER BY l.updated DESC LIMIT 200", (turn.platform + ':', turn.actor, turn.origin)),
        )
        for source, path, sql, params in sources:
            if source == 'long_term' and not self.include_long_term:
                continue
            try:
                for row in self._read(path, sql, params):
                    if source == 'long_term':
                        item = MemoryItem('long_term:' + str(row['id']), str(row['user_id']),
                            row['session_id'], row['visibility'], row['type'], row['content'],
                            row['updated_at'], 'long_term_record', row['scope_id'])
                    elif source == 'episodes':
                        # Existing episodes are intentionally shared, with original ownership.
                        if not row['origin'].startswith(turn.platform + ':'):
                            continue
                        if now - row['updated'] > (180 if row['kind'] == 'event' else 3650) * 86400:
                            continue
                        if row['phase'] in ('resolved', 'cancelled') and now - row['updated'] > 90 * 86400:
                            continue
                        body = json.dumps({k: row[k] for k in ('topic', 'detail', 'evidence', 'event_day')}, ensure_ascii=False)
                        item = MemoryItem('episode:' + str(row['id']), row['owner'], row['origin'],
                            'shared', row['kind'], body, row['updated'], 'human_evidence',
                            turn.platform + ':shared', status=row['phase'])
                    elif source == 'exchanges':
                        body = json.dumps({'human': row['human'], 'reply': row['reply']}, ensure_ascii=False)
                        item = MemoryItem('exchange:' + str(row['id']), row['qq'], row['origin'],
                            'session', 'exchange', body, row['at'], 'confirmed_exchange')
                    else:
                        body = json.loads(row['payload'])
                        if row['kind'] == 'event' and now - row['updated'] > 180 * 86400:
                            continue
                        body['version'] = row['version']
                        item = MemoryItem(row['id'], row['owner'], row['origin'], row['visibility'], row['kind'],
                            json.dumps(body, ensure_ascii=False), row['updated'], 'versioned_human_evidence',
                            turn.platform + ':shared' if row['visibility'] == 'shared' else '', status=body['status'])
                    if item.visible_to(turn) and 0 < item.recorded_at <= now + 5 and len(item.text) <= 3000:
                        items.append(item)
            except (sqlite3.Error, KeyError, TypeError, ValueError):
                unavailable.append(source)
        return CatalogSnapshot(tuple(items), tuple(unavailable))
