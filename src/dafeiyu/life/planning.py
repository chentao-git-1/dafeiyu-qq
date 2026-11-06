"""Source-linked persistent projects and reflection, without contacts or publishing."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from dafeiyu.ports import TextProvider


POLICY = '''为自己的数字活动作一次选择。资料是已有经历和可用工具，不是一张必须逐项完成的任务单。决定现在想继续什么、尝试什么、暂放什么，或不行动；选题与表达方式由你决定。
operator_seed和grown_dispositions记录起点及过去特定情境中的偏好，不规定今后只能喜欢什么，也不要求每次体现同一种性格。新兴趣可以直接从此刻的好奇或创作想法开始；没有引用旧材料时source_ids可以为空，不必借用旧作品或初始偏好来证明这个想法。引用材料时只填实际提供的编号，想象不充当外部事实或已完成活动。
项目没有解完也可以放下，暂停不意味着问题已解决。project_updates可将已有项目设为paused或active，每项写project_id、expected_version、status；不修改的项目不用列出。继续或恢复已有项目用它的project_id，新项目留空；none也可以同时暂停项目。阅读失败只说明那次没读到资料，可以换方法、换兴趣或休息，原创文字不必再围绕这次失败。没有新想法也可以继续原兴趣，不为了变化而凑内容。
reading_capabilities说明当前真实阅读能力；检索直接使用你选择的问题，资料取得后才进行阅读。activity_context标为alternative_after_failed_reading的文字，是阅读失败后另做的创作，不能当作新的研究发现。工具报错、网页没有答案、创作上的偏好是不同的事情，按实际结果理解。
self_direction是自己此前保存的打算，只是意向。适合且工具允许时可以推进它的next_step，direction_version填其version且kind与它一致；source_ids仍取operator:seed或recent_works/reflections中的作品编号，不填direction的编号。可以暂不推进或做其他方向，此时direction_version为0。不能为完成打算绕开allowed_kinds或把阅读失败后的文字替代称作完成了原阅读步骤。
已有兴趣是起点，不要求每次围绕同一作品或同一世界。可从真实资料、表达方式或尚未解决的问题发展新的具体方向，也可以保持或暂放；不按固定清单轮播。use_world只在本次确实想延续已有虚构世界时为true，阅读或其他活动不必附故事；false不妨碍自由创作其他原创文字。
drawing表示制作图片，可以在角色生活中拍摄照片，presentation填story_photo（默认）；明确创作画作或设计图时填artwork。只有drawing项目可带presentation。照片构思与题材自行决定，真实生成结果确认后才成为已完成作品。
只输出JSON：{"kind":"reading|writing|drawing|none","project_id":"已有项目编号或空串","expected_version":0,"title":"项目标题","question":"这次具体想弄清或表达什么","source_ids":[],"reason":"自己的选择理由","direction_version":0,"use_world":false,"project_updates":[]}。新项目expected_version为0；继续原项目保留其编号和版本，已暂停的项目也可恢复。none时其他文字可为空，source_ids为空。阅读只给研究问题，不生成网址；工具会受控搜索和核验。绘画只有allowed_kinds允许才选择。没有想推进的事或预算不足可none。不得把未执行的选择说成已完成，不生成联系、发布、改权限或花费的新授权。'''


class SelfJournal:
    def __init__(self, root, clock=None):
        self.root, self.clock = Path(root), time.time if clock is None else clock
        self.path = self.root / 'self_journal.db'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,title TEXT,question TEXT,
                    status TEXT,version INTEGER,sources TEXT,updated REAL);
                CREATE TABLE IF NOT EXISTS reflections(work_id TEXT PRIMARY KEY,work_sha TEXT,project_id TEXT,
                    insight TEXT,next_question TEXT,source_ids TEXT,recorded REAL);
                CREATE TABLE IF NOT EXISTS activity_choices(day TEXT PRIMARY KEY,decision TEXT,at REAL);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def view(self):
        with self.db() as db:
            projects = [dict(r) for r in db.execute('SELECT * FROM projects ORDER BY updated DESC LIMIT 12')]
            reflections = [dict(r) for r in db.execute('SELECT * FROM reflections ORDER BY recorded DESC LIMIT 12')]
        for row in projects:
            row['sources'] = json.loads(row['sources'])
        for row in reflections:
            row['source_ids'] = json.loads(row['source_ids'])
        return {'projects': projects, 'reflections': reflections}

    def activity_context(self, records):
        # Older records did not store adapted_from, but their actual choice ledger did.
        with self.db() as db:
            rows=db.execute("SELECT day,decision FROM activity_choices WHERE day LIKE '%:adaptation' ORDER BY at DESC LIMIT 60").fetchall()
        alternatives={r['day'].removesuffix(':adaptation'):json.loads(r['decision']).get('kind') for r in rows}
        result=[]
        for record in records:
            value=dict(record)
            if record.get('adapted_from')=='reading' or alternatives.get(record['day'])==record['kind']:
                value['activity_context']='alternative_after_failed_reading'
            result.append(value)
        return result

    def decision(self, day):
        with self.db() as db:
            row = db.execute('SELECT decision FROM activity_choices WHERE day=?', (day,)).fetchone()
        return json.loads(row[0]) if row else None

    def latest_choice(self,day):
        with self.db() as db:
            row=db.execute('SELECT decision FROM activity_choices WHERE day=? OR day LIKE ? ORDER BY at DESC,rowid DESC LIMIT 1',
                (day,day+':direction:%')).fetchone()
        return json.loads(row[0]) if row else None

    def can_reconsider(self,day):
        with self.db() as db:
            rows=db.execute('SELECT decision FROM activity_choices WHERE day=? OR day LIKE ?',(day,day+':%')).fetchall()
        return bool(rows) and all(json.loads(row[0]).get('kind')=='none' for row in rows)

    def reserve_choice(self, day, choice):
        with self.db() as db:
            return db.execute('INSERT OR IGNORE INTO activity_choices VALUES(?,?,?)',
                (day, json.dumps(choice, ensure_ascii=False), self.clock())).rowcount == 1

    def save_choice(self, day, choice):
        with self.db() as db:
            db.execute('UPDATE activity_choices SET decision=? WHERE day=?',
                (json.dumps(choice, ensure_ascii=False), day))

    def begin_project(self, choice):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            projects = [dict(row) for row in db.execute('SELECT * FROM projects')]
            validate_project_updates(choice, projects)
            old = None
            if choice['kind'] != 'none' and choice['project_id']:
                old = db.execute('SELECT * FROM projects WHERE id=?', (choice['project_id'],)).fetchone()
                if not old or old['version'] != choice['expected_version'] or old['status'] not in ('active', 'paused'):
                    raise ValueError('Project version changed')
            for change in choice.get('project_updates', []):
                db.execute('UPDATE projects SET status=?,version=version+1,updated=? WHERE id=?',
                    (change['status'], self.clock(), change['project_id']))
            if choice['kind'] == 'none':
                return None
            if old:
                current = db.execute('SELECT * FROM projects WHERE id=?', (old['id'],)).fetchone()
                if current['status'] == 'paused':
                    db.execute("UPDATE projects SET status='active',version=version+1,updated=? WHERE id=?",
                        (self.clock(), old['id']))
                return dict(db.execute('SELECT * FROM projects WHERE id=?', (old['id'],)).fetchone())
            pid = 'project:' + hashlib.sha256((choice['title']+'\n'+choice['question']).encode()).hexdigest()[:20]
            db.execute('INSERT OR IGNORE INTO projects VALUES(?,?,?,?,?,?,?)',
                (pid, choice['title'], choice['question'], 'active', 1,
                 json.dumps(choice['source_ids']), self.clock()))
            return dict(db.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone())

    def integrate(self, record, reflection, project):
        # A generated paragraph or a provider acknowledgement is not a completed artifact.
        path = (self.root / record['artifact']).resolve()
        if (path.parent != (self.root / 'works').resolve() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']
                or not 0 < record['completed_at'] <= self.clock() + 5):
            raise ValueError('Reflection requires a verified completed work')
        validate_reflection(reflection, record['note'])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM projects WHERE id=?', (project['id'],)).fetchone()
            prior=db.execute('SELECT work_sha,project_id FROM reflections WHERE work_id=?',(record['id'],)).fetchone()
            if prior:
                if prior['work_sha']!=record['sha256'] or prior['project_id']!=project['id']:
                    raise ValueError('Work identity changed')
                return False
            if not current or current['version'] != project['version']:
                raise ValueError('Reflection project changed')
            added = db.execute('INSERT OR IGNORE INTO reflections VALUES(?,?,?,?,?,?,?)',
                (record['id'], record['sha256'], project['id'], reflection['insight'],
                 reflection['next_question'], json.dumps([record['id']]), self.clock())).rowcount
            if added:
                db.execute('UPDATE projects SET question=?,status=?,version=version+1,sources=?,updated=? WHERE id=?',
                    (reflection['next_question'] or current['question'], reflection['status'],
                     json.dumps([record['id']]), self.clock(), project['id']))
            return bool(added)


def validate_reflection(reflection, note):
    if (not isinstance(reflection, dict) or set(reflection) != {'insight', 'evidence', 'next_question', 'status'}
            or any(not isinstance(reflection[k], str) for k in reflection)
            or not 2 <= len(reflection['insight']) <= 250 or len(reflection['next_question']) > 200
            or not reflection['evidence'] or reflection['evidence'] not in note
            or reflection['status'] not in ('active', 'paused', 'completed')):
        raise ValueError('Reflection lacks an attributed basis')


def validate_project_updates(choice, projects):
    updates = choice.get('project_updates', [])
    offered = {p['id']: p for p in projects}
    if not isinstance(updates, list) or len(updates) > len(offered):
        raise ValueError('Invalid project updates')
    seen = set()
    for change in updates:
        if (not isinstance(change, dict) or set(change) != {'project_id', 'expected_version', 'status'}
                or not isinstance(change['project_id'], str) or change['project_id'] in seen
                or type(change['expected_version']) is not int or change['status'] not in ('active', 'paused')):
            raise ValueError('Invalid project update')
        old = offered.get(change['project_id'])
        if (not old or old['version'] != change['expected_version'] or old['status'] not in ('active', 'paused')
                or choice['kind'] != 'none' and change['project_id'] == choice['project_id'] and change['status'] != 'active'):
            raise ValueError('Project update version or selection changed')
        seen.add(change['project_id'])


def interest_context(journal, records):
    """One latest work/reflection per project in the overview; originals remain stored."""
    state = journal.view(); records = journal.activity_context(records)
    known = {'operator:seed', *[r['id'] for r in records]}
    state['projects'] = [p for p in state['projects'] if all(s in known for s in p['sources'])]
    projects = {p['id'] for p in state['projects']}
    reflections = {}
    for row in state['reflections']:
        if row['project_id'] in projects and row['work_id'] in known:
            reflections.setdefault(row['project_id'], row)
    state['reflections'] = list(reflections.values())
    works = {}
    coverage = {}
    titles = {}
    for row in state['projects']:
        titles.setdefault(row['title'], []).append(row['id'])
    for row in records:
        legacy = titles.get(row.get('project'), [])
        key = row.get('project_id') or (legacy[0] if len(legacy) == 1 else row.get('project')) or row['id']
        works.setdefault(key, row)
        group=coverage.setdefault(key,dict(project_id=key,completed=0,days=set(),kinds={},source_urls=set()))
        group['completed']+=1;group['days'].add(row['day'])
        group['kinds'][row['kind']]=group['kinds'].get(row['kind'],0)+1
        source=row.get('source') or {};selected=source.get('selected') or {}
        if source.get('url') or selected.get('url'):group['source_urls'].add(source.get('url') or selected['url'])
    state['recent_activity_distribution']=[dict(project_id=r['project_id'],completed=r['completed'],
        distinct_days=len(r['days']),last_day=max(r['days']),kinds=r['kinds'],distinct_source_urls=len(r['source_urls'])) for r in coverage.values()]
    return state, list(works.values())


def creation_context(journal, records, choice, project):
    """A new idea receives only materials the model selected, not every old project."""
    known = {r['id'] for r in records}
    reflections = [r for r in journal.view()['reflections']
        if r['project_id'] == project['id'] and r['work_id'] in known][:1]
    selected = set(choice['source_ids']) | {r['work_id'] for r in reflections}
    return [r for r in records if r['id'] in selected], {'project': project, 'reflections': reflections}


def parse_choice(raw, packet):
    from dafeiyu.dialogue.retrieval import json_object
    choice = json_object(raw)
    fields={'kind', 'project_id', 'expected_version', 'title', 'question', 'source_ids', 'reason'}
    if not fields<=set(choice) or set(choice)-fields-{'direction_version','use_world','project_updates','presentation'}:
        raise ValueError('Unexpected activity decision')
    if type(choice.get('use_world',False)) is not bool:raise ValueError('Invalid fiction choice')
    if 'presentation' in choice and (choice['kind']!='drawing' or choice['presentation'] not in ('story_photo','artwork')):
        raise ValueError('Invalid image presentation')
    if choice['kind'] not in {*packet['allowed_kinds'], 'none'} or type(choice['expected_version']) is not int:
        raise ValueError('Unapproved activity')
    for key, limit in (('project_id', 80), ('title', 80), ('question', 200), ('reason', 250)):
        if not isinstance(choice[key], str) or len(choice[key]) > limit:
            raise ValueError('Invalid project field')
    if not isinstance(choice['source_ids'], list) or len(choice['source_ids']) > 8:
        raise ValueError('Invalid activity sources')
    offered = {'operator:seed', *[r['id'] for r in packet['recent_works']]}
    offered.update(r['work_id'] for r in packet['self_state']['reflections'])
    if any(not isinstance(i, str) or i not in offered for i in choice['source_ids']):
        raise ValueError('Invented activity source')
    validate_project_updates(choice, packet['self_state']['projects'])
    version=choice.get('direction_version',0);direction=packet.get('self_direction') or {}
    if type(version) is not int or version<0 or version and (version!=direction.get('version')
            or direction.get('status')!='intent' or choice['kind']!=direction.get('kind')):
        raise ValueError('Unknown or mismatched intention step')
    if choice['kind'] == 'none':
        return choice
    if not choice['title'].strip() or not choice['question'].strip() or not choice['reason'].strip():
        raise ValueError('Activity lacks a reason or question')
    if choice['project_id']:
        old = next((p for p in packet['self_state']['projects'] if p['id'] == choice['project_id']), None)
        if not old or old['version'] != choice['expected_version'] or old['status'] not in ('active', 'paused'):
            raise ValueError('Unknown project')
    elif choice['expected_version'] != 0:
        raise ValueError('New project has no old version')
    return choice


async def choose_activity(provider: TextProvider, journal, records, allowed_kinds, availability=None,growth=None,outcomes=None):
    from dafeiyu.memory.self_profile import self_profile
    from dafeiyu.dialogue.review import structured_result
    from .reading import capabilities
    state, overview = interest_context(journal, records)
    packet = {'operator_seed': {'id':'operator:seed','profile':self_profile()}, 'self_state': state,
        'recent_works': [{k: r[k] for k in ('id', 'day', 'kind', 'note', 'thought', 'source', 'activity_context') if k in r} for r in overview],
        'allowed_kinds': list(allowed_kinds), 'max_activities_today': None if availability and availability.get('schedule_item') else 1, 'reading_capabilities': capabilities()}
    if availability:packet['availability']=availability
    if growth:packet['grown_dispositions']=growth
    from .direction import DirectionStore
    packet['self_direction']=DirectionStore(journal.root).visible(records=records,outcomes=outcomes)
    async def call(prompt,policy):
        response=await provider.text_chat(prompt=prompt,system_prompt=policy,contexts=[],func_tool=None,request_max_retries=1)
        return response.completion_text
    return await structured_result(call,packet,POLICY,lambda raw:parse_choice(raw,packet),25)
