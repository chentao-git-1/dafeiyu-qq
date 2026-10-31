"""Source-linked persistent projects and reflection, without contacts or publishing."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time


POLICY = '''为AI角色选择一次自己的数字活动。输入仅含操作员种子偏好、真实作品、带来源反思和持续项目，不含人的私聊。结合尚未解决的问题、已做过什么和现在可用的材料，选择继续项目、开一个有理由的新方向，或者暂不行动。不要按次数轮播、随机抽签或为了“有生活”硬凑。
operator_seed.id也是明确可引用的兴趣来源；尚无作品或项目时，可以从自己的兴趣提出一个具体问题，开始原创文字或受控阅读，不必先有成品才允许创作。创作构思不要求外部事实证据；阅读问题会交给工具查证，不提前编知识或网址。已有项目值得继续时接着推进，不重复建立同一个方向。none仍然是允许的真实选择，不把没有历史成品误解成无法自主开始。
只输出JSON：{"kind":"reading|writing|drawing|none","project_id":"已有项目编号或空串","expected_version":0,"title":"项目标题","question":"这次具体想弄清或表达什么","source_ids":["实际提供的来源编号"],"reason":"简短依据"}。新项目expected_version为0；继续原项目保留其编号和版本。none时其他文字可为空，source_ids为空。阅读只给研究问题，不生成网址；工具会受控搜索和核验。绘画只有allowed_kinds允许才选择。没有合适材料、没有想推进的事或预算不足可none。不得把未执行的选择说成已完成，不生成联系、发布、改权限或花费的新授权。'''


class SelfJournal:
    def __init__(self, root, clock=time.time):
        self.root, self.clock = Path(root), clock
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

    def decision(self, day):
        with self.db() as db:
            row = db.execute('SELECT decision FROM activity_choices WHERE day=?', (day,)).fetchone()
        return json.loads(row[0]) if row else None

    def reserve_choice(self, day, choice):
        with self.db() as db:
            return db.execute('INSERT OR IGNORE INTO activity_choices VALUES(?,?,?)',
                (day, json.dumps(choice, ensure_ascii=False), self.clock())).rowcount == 1

    def save_choice(self, day, choice):
        with self.db() as db:
            db.execute('UPDATE activity_choices SET decision=? WHERE day=?',
                (json.dumps(choice, ensure_ascii=False), day))

    def begin_project(self, choice):
        if choice['kind'] == 'none':
            return None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if choice['project_id']:
                old = db.execute('SELECT * FROM projects WHERE id=?', (choice['project_id'],)).fetchone()
                if not old or old['version'] != choice['expected_version'] or old['status'] != 'active':
                    raise ValueError('Project version changed')
                return dict(old)
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
            or reflection['status'] not in ('active', 'completed')):
        raise ValueError('Reflection lacks an attributed basis')


def parse_choice(raw, packet):
    from data.plugins.dafeiyu_dialogue.retrieval import json_object
    choice = json_object(raw)
    if set(choice) != {'kind', 'project_id', 'expected_version', 'title', 'question', 'source_ids', 'reason'}:
        raise ValueError('Unexpected activity decision')
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
    if choice['kind'] == 'none':
        return choice
    if not choice['title'] or not choice['question'] or not choice['source_ids']:
        raise ValueError('Activity lacks a reason or question')
    if choice['project_id']:
        old = next((p for p in packet['self_state']['projects'] if p['id'] == choice['project_id']), None)
        if not old or old['version'] != choice['expected_version'] or old['status'] != 'active':
            raise ValueError('Unknown project')
    elif choice['expected_version'] != 0:
        raise ValueError('New project has no old version')
    return choice


async def choose_activity(provider, journal, records, allowed_kinds):
    from data.plugins.dafeiyu_natural_chat.self_profile import self_profile
    from data.plugins.dafeiyu_dialogue.review import structured_result
    packet = {'operator_seed': {'id':'operator:seed','profile':self_profile()}, 'self_state': journal.view(),
        'recent_works': [{k: r[k] for k in ('id', 'day', 'kind', 'note', 'thought', 'source')} for r in records],
        'allowed_kinds': list(allowed_kinds), 'max_activities_today': 1}
    async def call(prompt,policy):
        response=await provider.text_chat(prompt=prompt,system_prompt=policy,contexts=[],func_tool=None,request_max_retries=1)
        return response.completion_text
    return await structured_result(call,packet,POLICY,lambda raw:parse_choice(raw,packet),25)
