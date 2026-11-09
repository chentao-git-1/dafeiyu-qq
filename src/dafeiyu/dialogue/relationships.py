"""Subjective, revisable relationship memories grounded in confirmed exchanges; no scores."""
from .learning import digest

ASPECTS={'impression','trust','closeness'}
SCHEMA='''
CREATE TABLE IF NOT EXISTS relationship_notes(id TEXT PRIMARY KEY,owner TEXT,origin TEXT,aspect TEXT,
    version INTEGER,view TEXT,context TEXT,status TEXT,source_id TEXT,updated REAL);
CREATE INDEX IF NOT EXISTS relationship_scope ON relationship_notes(owner,origin,status,updated);
CREATE TABLE IF NOT EXISTS relationship_versions(note_id TEXT,version INTEGER,view TEXT,context TEXT,status TEXT,
    source_id TEXT,quote TEXT,reply TEXT,at REAL,PRIMARY KEY(note_id,version));
CREATE TABLE IF NOT EXISTS relationship_commits(source_id TEXT PRIMARY KEY,proposal_hash TEXT);
'''

POLICY='''relationship_memory只记录你对当前真实发言者的主观看法、信任与亲近感；它有具体来源和适用情境，可以继续保留、改观、暂不下结论或撤回。不是对方的客观人格，也不是固定身份、权限或必须照演的语气。不要把旧话题带进每次聊天，不宣读内部记录。
遇到有意义的真实相处，可以在本轮回复JSON中可选附加relationship数组，最多一项：{"aspect":"impression|trust|closeness","view":"自己形成或修正的具体看法，最多120字","context":"适用情境、保留意见或不确定之处，最多100字","evidence":"当前human中的逐字依据，最多200字","subject":"speaker","modality":"direct","supersedes":"本轮提供的旧记录编号或空串","expected_version":0,"status":"active|withdrawn"}。没发生值得保存的关系变化就省略，不为凑记录评价别人。
只能依据当前本人与你的真实互动，结合已保存的关系记忆和真实上下文判断。将看法与事实分开：自我宣称不等于经过验证，第三人转述、引用、创作、假设、OCR和你自己编出的经历不作为当前本人的直接相处证据；不根据敏感身份猜测性格。不按聊天次数、夸奖数量或数值升降，不把一次情绪概括成永久标签；反例和新的理解可以改变原看法，谨慎或亲近都须说明情境。
修改或撤回同一看法用supersedes和当前expected_version；新看法留空编号、版本0。撤回仍写明当下看法和依据，旧版本仅供追溯、不再影响聊天。亲近、信任不自动扩大操作权限或改变爸爸等固定身份，也不要求排他、依赖或索取陪伴。关系记忆仅用于本人的当前会话范围，不向其他人、群或空间披露私人看法。保存以实际送达为准，本轮提案不能说成已经永久记住。'''
POLICY+='\n优先完成自然回复与完整JSON；没有足够篇幅时省略关系提案。已有相同看法不重复新建，使用原编号修订或保留原样。没有关系记录只表示尚未形成保存的看法，不证明陌生或第一次相识。'


def proposals(value,human):
    """Invalid optional metadata is dropped without rewriting the actual reply."""
    if value is None:return []
    if not isinstance(value,list) or len(value)>1:return []
    fields={'aspect','view','context','evidence','subject','modality','supersedes','expected_version','status'}
    accepted=[]
    for item in value:
        if not isinstance(item,dict) or set(item)!=fields:continue
        if any(not isinstance(item[k],str) for k in fields-{'expected_version'}):continue
        if (item['aspect'] not in ASPECTS or item['status'] not in ('active','withdrawn')
                or (item['subject'],item['modality'])!=('speaker','direct')
                or not 2<=len(item['view'].strip())<=120 or not 1<=len(item['context'].strip())<=100
                or not 2<=len(item['evidence'].strip())<=200 or item['evidence'] not in human
                or len(item['supersedes'])>80 or type(item['expected_version']) is not int
                or item['expected_version']<0 or not item['supersedes'] and (item['expected_version'] or item['status']=='withdrawn')):continue
        from dafeiyu.memory.interaction import SENSITIVE
        if SENSITIVE.search(human) or SENSITIVE.search(item['view']+' '+item['context']):continue
        accepted.append(dict(item))
    return accepted


def purge(db,owner,origin=None,ids=None,source_ids=()):
    """Runs inside the learning store's privacy transaction, including all old revisions."""
    rows=db.execute('SELECT id,source_id FROM relationship_notes WHERE owner=?'+(' AND origin=?' if origin else ''),
        (str(owner),origin) if origin else (str(owner),)).fetchall()
    selected=set(ids or ());sources=set(source_ids)
    remove=[]
    for row in rows:
        lineage={r[0] for r in db.execute('SELECT source_id FROM relationship_versions WHERE note_id=?',(row['id'],))}
        if ids is None or row['id'] in selected or lineage & sources:
            remove.append(row['id'])
    for key in remove:
        db.execute('DELETE FROM relationship_versions WHERE note_id=?',(key,))
        db.execute('DELETE FROM relationship_notes WHERE id=?',(key,))
    return len(remove)


class RelationshipMemory:
    def __init__(self,learning):self.learning=learning

    def current(self,owner,origin):
        with self.learning.db() as db:
            rows=db.execute("SELECT n.* FROM relationship_notes n JOIN learning_sources s ON s.id=n.source_id "
                "WHERE n.owner=? AND n.origin=? AND n.status='active' AND s.revoked=0 ORDER BY n.updated DESC,n.id LIMIT 12",
                (str(owner),origin)).fetchall()
        return [{k:r[k] for k in ('id','aspect','version','view','context','status','updated')} for r in rows]

    def history(self,owner,origin):
        with self.learning.db() as db:
            rows=db.execute('SELECT n.id,n.aspect,v.version,v.view,v.context,v.status,v.quote,v.at '
                'FROM relationship_versions v JOIN relationship_notes n ON n.id=v.note_id '
                'JOIN learning_sources s ON s.id=v.source_id WHERE n.owner=? AND n.origin=? AND s.revoked=0 '
                'ORDER BY v.at DESC,v.version DESC LIMIT 12',(str(owner),origin)).fetchall()
        return [dict(row) for row in rows]

    def commit(self,source,human,items,offered_versions,reply,*,confirmed=False):
        if not confirmed or not isinstance(reply,str) or not reply.strip() or not items:return []
        if source['human_hash']!=digest(human):raise ValueError('Relationship source mismatch')
        values=proposals(items,human)
        if not values:return []
        with self.learning.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM learning_sources WHERE id=?',(source['id'],)).fetchone()
            if (not row or row['revoked'] or row['epoch']!=self.learning._epoch(db,source['owner'])
                    or any(row[k]!=source[k] for k in ('owner','origin','mid','human_hash','epoch'))):return []
            if db.execute('SELECT 1 FROM relationship_commits WHERE source_id=?',(source['id'],)).fetchone():return []
            saved=[]
            for index,item in enumerate(values):
                if item['supersedes']:
                    old=db.execute('SELECT * FROM relationship_notes WHERE id=?',(item['supersedes'],)).fetchone()
                    expected=offered_versions.get(item['supersedes'])
                    if (not old or old['owner']!=source['owner'] or old['origin']!=source['origin']
                            or old['aspect']!=item['aspect'] or old['status']!='active'
                            or expected!=item['expected_version'] or old['version']!=expected):continue
                    key,version=old['id'],old['version']+1
                else:
                    if db.execute("SELECT 1 FROM relationship_notes WHERE owner=? AND origin=? AND aspect=? AND view=? AND context=? AND status='active'",
                            (source['owner'],source['origin'],item['aspect'],item['view'],item['context'])).fetchone():continue
                    count=db.execute("SELECT COUNT(*) FROM relationship_notes WHERE owner=? AND origin=? AND status='active'",
                        (source['owner'],source['origin'])).fetchone()[0]
                    if count>=12:continue
                    key,version='relationship:'+digest([source['id'],index])[:24],1
                now=self.learning.clock()
                db.execute('INSERT OR REPLACE INTO relationship_notes VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (key,source['owner'],source['origin'],item['aspect'],version,item['view'],item['context'],item['status'],source['id'],now))
                db.execute('INSERT INTO relationship_versions VALUES(?,?,?,?,?,?,?,?,?)',
                    (key,version,item['view'],item['context'],item['status'],source['id'],item['evidence'],reply[:24000],now))
                saved.append(key)
            db.execute('INSERT INTO relationship_commits VALUES(?,?)',(source['id'],digest(values)))
            return saved

    def status(self):
        with self.learning.db() as db:
            return dict(active_notes=db.execute("SELECT COUNT(*) FROM relationship_notes WHERE status='active'").fetchone()[0],
                people=db.execute("SELECT COUNT(DISTINCT owner) FROM relationship_notes WHERE status='active'").fetchone()[0],
                versions=db.execute('SELECT COUNT(*) FROM relationship_versions').fetchone()[0])
