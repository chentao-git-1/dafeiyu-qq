"""Shared recall with explicit ownership/origin and owner-protected mutations."""
import json
import re
import sqlite3
from .database import MemoryDatabase, VALID_TYPES
from .scope import current_scope, requested_visibility, scope_from_values, owner_name, origin_kind, MEMORY_ADMIN, using_scope
from .utils import now_ts, parse_tags
from .quality import QualityDatabaseMixin

class ScopedMemoryDatabase(QualityDatabaseMixin, MemoryDatabase):
    async def initialize(self):
        await super().initialize()
        async with self._lock:
            cursor=await self._conn.execute('PRAGMA table_info(memories)')
            names={row[1] for row in await cursor.fetchall()}
            for name,definition in (('visibility',"TEXT NOT NULL DEFAULT ''"),('scope_id',"TEXT NOT NULL DEFAULT ''"),
                    ('owner_name',"TEXT NOT NULL DEFAULT ''"),('origin_kind',"TEXT NOT NULL DEFAULT ''"),('merged_into','INTEGER')):
                if name not in names:await self._conn.execute('ALTER TABLE memories ADD COLUMN '+name+' '+definition)
            cursor=await self._conn.execute("SELECT id,user_id,session_id,visibility,scope_id,owner_name,origin_kind FROM memories WHERE visibility!='shared' OR scope_id='' OR owner_name='' OR origin_kind=''")
            for row in await cursor.fetchall():
                scope=scope_from_values(row[1],row[2])
                await self._conn.execute('UPDATE memories SET visibility=?,scope_id=?,owner_name=?,origin_kind=? WHERE id=?',
                    ('shared',scope.platform+':shared',owner_name(row[1],row[5]),row[6] or origin_kind(row[2],row[3]),row[0]))
            await self._conn.execute('CREATE INDEX IF NOT EXISTS idx_memory_visibility ON memories(user_id,visibility,scope_id)')
            await self._conn.execute('''CREATE TABLE IF NOT EXISTS memory_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,memory_id INTEGER NOT NULL,
                previous_record TEXT NOT NULL,changed_at REAL NOT NULL,action TEXT NOT NULL)''')
            await self._conn.execute('''CREATE TRIGGER IF NOT EXISTS memory_history_cleanup
                AFTER DELETE ON memories BEGIN
                DELETE FROM memory_history WHERE memory_id=OLD.id; END''')
            await self.initialize_quality()
            await self._conn.commit()

    @staticmethod
    def _scope_query(sql,params):
        scope=current_scope.get()
        clause='merged_into IS NULL'
        values=[]
        if scope is not None:
            clause+=" AND visibility='shared' AND scope_id IN (?,?)"
            values=[scope.platform+':shared','legacy:shared']
        sql,count=re.subn(r'\bFROM\s+memories\b','FROM (SELECT * FROM memories WHERE '+clause+') AS memories',sql,count=1,flags=re.I)
        if not count:raise RuntimeError('Invalid shared memory SELECT')
        return sql,tuple(values)+tuple(params)

    async def _fetchall(self,sql,params=()):
        sql,params=self._scope_query(sql,params)
        return await super()._fetchall(sql,params)

    async def _fetchone(self,sql,params=()):
        sql,params=self._scope_query(sql,params)
        return await super()._fetchone(sql,params)

    @staticmethod
    def _write_scope(user_id,session_id):
        scope=current_scope.get() or scope_from_values(user_id,session_id)
        if current_scope.get() is not None and str(user_id)!=scope.user_id:raise ValueError('不能写入其他人的记忆')
        return 'shared',scope.platform+':shared'

    async def add_memory(self,user_id,content,memory_type='user_fact',tags=None,importance=0.5,session_id='',source='auto'):
        visibility,key=self._write_scope(user_id,session_id)
        now=now_ts()
        tags_json=json.dumps(parse_tags(tags),ensure_ascii=False)
        scope=current_scope.get()
        display_name=owner_name(user_id,scope.sender_name if scope else '')
        source_kind=origin_kind(session_id,scope.kind if scope else '')
        async with self._lock:
            cursor=await self._conn.execute('SELECT id FROM memories WHERE merged_into IS NULL AND user_id=? AND content=? AND visibility=? AND scope_id=? LIMIT 1',
                (str(user_id),content,visibility,key))
            previous=await cursor.fetchone()
            if previous:
                await self._conn.execute('UPDATE memories SET tags=?,importance=?,updated_at=?,source=?,owner_name=? WHERE id=?',
                    (tags_json,float(importance),now,source,display_name,previous[0]))
                await self._conn.commit()
                return int(previous[0]),False
            cursor=await self._conn.execute('''INSERT INTO memories
                (user_id,session_id,content,type,tags,importance,source,created_at,updated_at,last_accessed,access_count,visibility,scope_id,owner_name,origin_kind)
                VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)''',
                (str(user_id),session_id,content,memory_type if memory_type in VALID_TYPES else 'user_fact',tags_json,float(importance),source,now,now,now,visibility,key,display_name,source_kind))
            await self._conn.commit()
            return int(cursor.lastrowid),True

    async def _visible(self,memory_id):
        return current_scope.get() is None or bool(await self.get(int(memory_id)))

    async def touch(self,memory_id):
        if await self._visible(memory_id):return await super().touch(memory_id)

    async def _editable(self,memory_id):
        scope=current_scope.get()
        record=await self.get(int(memory_id))
        return bool(record) and (scope is None or scope.user_id in (str(record['user_id']),MEMORY_ADMIN))

    async def update_importance(self,memory_id,importance):
        if not await self._editable(memory_id):return False
        return await super().update_importance(memory_id,importance)

    async def delete_memory(self,memory_id):
        if not await self._editable(memory_id):return False
        return await super().delete_memory(memory_id)

    async def clear(self,user_id,session_id=None):
        scope=current_scope.get()
        if scope is None:return await super().clear(user_id,session_id)
        if str(user_id)!=scope.user_id:return 0
        async with self._lock:
            cursor=await self._conn.execute('DELETE FROM memories WHERE user_id=? AND visibility=? AND scope_id=?',
                (scope.user_id,'shared',scope.platform+':shared'))
            await self._conn.commit()
            return cursor.rowcount

    async def clear_all(self):
        if current_scope.get() is not None:raise ValueError('聊天中不能清空全体用户记忆')
        return await super().clear_all()

    async def import_memories(self,records):
        scope=current_scope.get()
        if scope is not None and scope.user_id==MEMORY_ADMIN:
            with using_scope(None):return await super().import_memories(records)
        if scope is not None:
            records=[{**record,'user_id':scope.user_id,'session_id':scope.key} for record in records]
        return await super().import_memories(records)

    async def _archive(self,record,action):
        await self._conn.execute('INSERT INTO memory_history(memory_id,previous_record,changed_at,action) VALUES (?,?,?,?)',
            (record['id'],json.dumps(record,ensure_ascii=False),now_ts(),action))

    async def correct(self,memory_id,content):
        content=str(content or '').strip()
        if not content or len(content)>2000:raise ValueError('更正内容需要1到2000字')
        record=await self.get(int(memory_id))
        if not record or not await self._editable(memory_id):raise ValueError('只能更正自己的记忆；管理员可管理全体记忆')
        async with self._lock:
            await self._archive(record,'correct')
            await self._conn.execute('UPDATE memories SET content=?,updated_at=? WHERE id=?',(content,now_ts(),int(memory_id)))
            await self._conn.commit()
        return record['visibility']

    async def set_visibility(self,memory_id,value):
        raise ValueError('当前采用带归属和来源的共享记忆，不再设置私信或群聊读取隔离')

    async def recall_candidates(self,user_id,limit=300):
        return await self._fetchall('SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?',(max(1,int(limit)),))

    async def list_memories(self,user_id,session_id=None,page=1,page_size=10):
        if current_scope.get() is None:return await super().list_memories(user_id,session_id,page,page_size)
        where='1=1';params=[]
        if session_id:where+=' AND session_id=?';params.append(session_id)
        total=await self._fetchone('SELECT COUNT(*) AS c FROM memories WHERE '+where,tuple(params))
        rows=await self._fetchall('SELECT * FROM memories WHERE '+where+' ORDER BY created_at DESC LIMIT ? OFFSET ?',
            tuple(params+[int(page_size),(max(1,int(page))-1)*int(page_size)]))
        return rows,int(total['c']) if total else 0

    async def search(self,user_id,keyword='',memory_type=None,tags=None,min_importance=0.0,limit=50):
        if current_scope.get() is None:return await super().search(user_id,keyword,memory_type,tags,min_importance,limit)
        where='importance>=?';params=[float(min_importance)]
        if keyword:
            where+=' AND (content LIKE ? OR owner_name LIKE ? OR user_id LIKE ?)';params.extend(['%'+keyword+'%']*3)
        if memory_type:where+=' AND type=?';params.append(memory_type)
        for tag in tags or []:where+=' AND tags LIKE ?';params.append('%"'+tag+'"%')
        return await self._fetchall('SELECT * FROM memories WHERE '+where+' ORDER BY importance DESC,updated_at DESC LIMIT ?',tuple(params+[int(limit)]))

    async def stats(self,user_id):
        if current_scope.get() is None:return await super().stats(user_id)
        rows=await self._fetchall('SELECT type,COUNT(*) AS c FROM memories GROUP BY type')
        totals=await self._fetchone('SELECT COUNT(*) AS total,IFNULL(AVG(importance),0) AS avg_importance,IFNULL(SUM(access_count),0) AS accesses FROM memories')
        return {'total':int(totals['total']),'by_type':{row['type']:row['c'] for row in rows},
            'avg_importance':round(float(totals['avg_importance']),3),'total_accesses':int(totals['accesses'])}

    async def export_all(self,user_id=None):
        scope=current_scope.get()
        return await super().export_all(None if scope is not None and scope.user_id==MEMORY_ADMIN else user_id)
