"""Bot-owned intentions, independent of completed work and private conversations."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time

POLICY='''你在为自己的数字兴趣形成一个持续打算。资料只含自己的稳定画像、已核验作品、项目和全局性格倾向，不含任何人的私聊。自主决定想继续什么、换一个方向、暂时放下，或保持原样。可以从种子兴趣提出新问题；不按次数轮播，不为了显得有自主性硬凑。
这是意向，不是已执行的活动或对人的承诺。next_step写一个接下来想尝试的具体小步骤，只能属于reading、writing或drawing；实际执行仍由已有工具与预算检查。可以画、写、查证一个问题，不能增加联系人、发布、提醒、代码修改、身份、权限或支出授权；不把猜测写成外部知识或虚构身体经历。
只有实际完成的工具记录能完成一个打算。可以修订思路或pause，不自行宣称完成。参考已完成的步骤继续发展，避免重复研究同一个问题。新内容不足可hold，重新考虑间隔由自己决定，到时也可保持原样。
只输出JSON：decision(pursue/pause/hold)、expected_version(精确等于current.version，没有时0)、title(最多80字)、question(最多200字)、next_step(最多200字)、kind(reading/writing/drawing或空串)、source_ids(给出的资料编号数组)、source_quote(逐字摘自其中一项note)、reason(最多250字)、reconsider_after_minutes(整数1到1440)。pursue时标题、问题、小步骤、来源和摘录非空；pause或hold时这些内容可空。不能输出工具调用。'''


def packet(store,journal,growth=None):
    from data.plugins.dafeiyu_natural_chat.self_profile import self_profile
    works=store.verified_records(60)
    sources=[dict(id='operator:seed',note=json.dumps(self_profile(),ensure_ascii=False))]
    sources.extend({k:r[k] for k in ('id','day','kind','note','thought')} for r in works[:12])
    state=journal.view();known={r['id'] for r in works}
    state['reflections']=[r for r in state['reflections'] if r['work_id'] in known]
    state['projects']=[r for r in state['projects'] if all(s in known or s=='operator:seed' for s in r['sources'])]
    return dict(sources=sources,self_state=state,character_growth=growth or {})


def fingerprint(materials):
    return hashlib.sha256(json.dumps(materials,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def parse(raw,materials,current):
    from data.plugins.dafeiyu_dialogue.retrieval import json_object
    obj=json_object(raw)
    if set(obj)!={'decision','expected_version','title','question','next_step','kind','source_ids','source_quote','reason','reconsider_after_minutes'}:
        raise ValueError('Unexpected intention fields')
    if obj['decision'] not in ('pursue','pause','hold') or type(obj['expected_version']) is not int or obj['expected_version']!=(current or {}).get('version',0):
        raise ValueError('Intention revision changed')
    if type(obj['reconsider_after_minutes']) is not int or not 1<=obj['reconsider_after_minutes']<=1440:
        raise ValueError('Invalid intention timing')
    for key,limit in (('title',80),('question',200),('next_step',200),('kind',12),('source_quote',250),('reason',250)):
        if not isinstance(obj[key],str) or len(obj[key])>limit:raise ValueError('Invalid intention text')
    ids=obj['source_ids'];offered={r['id']:r['note'] for r in materials['sources']}
    if not isinstance(ids,list) or len(ids)>8 or any(not isinstance(i,str) or i not in offered for i in ids):
        raise ValueError('Unknown intention source')
    if obj['decision']=='pursue' and (obj['kind'] not in ('reading','writing','drawing')
            or not all(obj[k].strip() for k in ('title','question','next_step','source_quote')) or not ids
            or not any(obj['source_quote'] in offered[i] for i in ids)):
        raise ValueError('Intention lacks a source or a concrete step')
    if obj['decision']=='pause' and not current:raise ValueError('No intention to pause')
    return obj


class DirectionStore:
    def __init__(self,root,clock=time.time):
        self.path=Path(root)/'self_direction.db';self.clock=clock;self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS intentions(version INTEGER PRIMARY KEY,body TEXT,at REAL);
                CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY,status TEXT,fingerprint TEXT,revision INTEGER,at REAL,next_at REAL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);''')
        self.path.chmod(0o600)
    @contextmanager
    def db(self):
        db=sqlite3.connect(str(self.path),timeout=3);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    @staticmethod
    def _current(db):
        row=db.execute('SELECT body FROM intentions ORDER BY version DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None
    def current(self):
        with self.db() as db:return self._current(db)
    def visible(self,life=None,records=None):
        current=self.current()
        if not current:return None
        known={'operator:seed',*[r['id'] for r in (life.verified_records(60) if records is None else records)]}
        return dict(current,status='intent' if current['status']=='active' else current['status']) if all(s in known for s in current['source_ids']) else None
    @staticmethod
    def _append(db,value,now):
        version=(DirectionStore._current(db) or {}).get('version',0)+1
        value=dict(value,id='direction:'+str(version),version=version,updated=now)
        db.execute('INSERT INTO intentions VALUES(?,?,?)',(version,json.dumps(value,ensure_ascii=False),now))
        return value
    def claim(self,materials):
        now=self.clock();key=fingerprint(materials)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            last=db.execute('SELECT * FROM reviews ORDER BY id DESC LIMIT 1').fetchone()
            if last and (now-last['at']<300 or last['fingerprint']==key and now<last['next_at']):return None
            current=self._current(db);revision=(current or {}).get('version',0)
            ident=db.execute('INSERT INTO reviews(status,fingerprint,revision,at,next_at) VALUES(?,?,?,?,?)',
                ('started',key,revision,now,now+3600)).lastrowid
            return dict(id=ident,revision=revision,fingerprint=key,current=current,materials=materials)
    def finish(self,claim,choice,unchanged=True):
        now=self.clock()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM reviews WHERE id=?',(claim['id'],)).fetchone();current=self._current(db)
            if not row or row['status']!='started':return False
            if not unchanged or (current or {}).get('version',0)!=claim['revision'] or now-row['at']>120:
                db.execute("UPDATE reviews SET status='stale' WHERE id=?",(claim['id'],));return False
            choice=parse(json.dumps(choice,ensure_ascii=False),claim['materials'],current)
            if choice['decision']=='pursue':
                self._append(db,{k:choice[k] for k in ('title','question','next_step','kind','source_ids','reason')}|
                    dict(status='active',completed_work=None),now)
            elif choice['decision']=='pause':self._append(db,dict(current,status='paused'),now)
            db.execute('UPDATE reviews SET status=?,next_at=? WHERE id=?',
                (choice['decision'],now+choice['reconsider_after_minutes']*60,claim['id']))
            return True
    def fail(self,claim):
        with self.db() as db:db.execute("UPDATE reviews SET status='failed' WHERE id=? AND status='started'",(claim['id'],))
    def complete_step(self,version,record,life):
        actual=next((r for r in life.verified_records(60) if r['id']==record['id'] and r['sha256']==record['sha256']),None)
        if not actual:return False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');current=self._current(db)
            if (not current or current['version']!=version or current['status']!='active' or current['kind']!=actual['kind']
                    or actual['completed_at']<current['updated']):return False
            self._append(db,dict(current,status='done',completed_work=actual['id']),self.clock());return True
    def status(self):
        with self.db() as db:
            current=self._current(db);row=db.execute('SELECT status,next_at FROM reviews ORDER BY id DESC LIMIT 1').fetchone()
            return dict(version=(current or {}).get('version',0),state=(current or {}).get('status'),
                last_review=row['status'] if row else None,next_review_at=row['next_at'] if row else None,
                review_count=db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0])


async def choose(provider,claim):
    response=await asyncio.wait_for(provider.text_chat(prompt=json.dumps(dict(claim['materials'],current=claim['current']),ensure_ascii=False),
        system_prompt=POLICY,contexts=[],func_tool=None,request_max_retries=0),timeout=25)
    return parse(response.completion_text,claim['materials'],claim['current'])
