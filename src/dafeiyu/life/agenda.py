"""One persisted night plan, independently claimed daytime activities, no daily count quota."""
import asyncio
import datetime as dt
import json
import re
import time

from .planning import planning_context, parse_choice, validate_project_updates
from .reading import capabilities
from .store import TZ, beijing_day

PLAN_OUTPUT_TOKENS=4096
PLAN_MAX_ATTEMPTS=2
PLAN_RETRY_DELAY=3600


class PlanOutputError(ValueError):
    def __init__(self,reason,raw='',finish_reason=''):
        super().__init__(reason)
        self.details=dict(reason=reason,finish_reason=finish_reason,characters=len(raw),response=raw[:32000])


POLICY = '''为哦鲸鲸安排目标日期的自主数字活动。夜间先想好，白天按这份计划逐项推进。活动数量、主题、形式和具体时间由你决定，可以留空、休息或继续已有兴趣，不必填满一天，也没有每天一项或三项的固定配额。
结合自己的兴趣、近期作品、项目状态、真实失败结果、可用时间及余额安排。旧倾向只作情境参考，不必重复旧题目。可以提出没有旧来源的新兴趣；只在实际引用旧材料时填写source_ids，想象和意向不能当作已完成经历或外部事实。资料不含他人的私聊，也不授权增加公开发布、私信、提醒或其他操作。
只安排allowed_kinds中的活动或none（休息）；每项都在给定北京时间窗口内，按时间先后，时间不重复。执行时仍检查服务可用性和余额，资金不足会等待。选择项目时用真实编号和当前版本；新项目留空编号、版本0。不同时间可以继续同一项目，实际执行时会读取那时的最新进展；已被暂停或结束的项目不会擅自恢复。
当天执行结果会进入下一次规划；未取得资料与研究没有答案不同，阅读失败后的替代文字也不是研究发现。计划不是已经做过的事，不虚构现实出门、吃饭或身体生活。正常聊天照常，不为了执行计划装忙或催人。
owner_wishes是主人当前希望生活更丰富的方向；初始爱好只是起点，不是题材范围。recent_activity_distribution如实描述近期活动分布，是否继续深入、暂放或接触新东西由你决定。
fictional_setting是已经确立的角色生活背景，可以自主选择use_world来发展故事，也可以做无关的数字活动。城市住处与过去的来历不是今天已完成活动，不据此安排真实出行或把背景地点轮流排进日程。
drawing也可以是角色生活中的拍照，只有这种图片活动可添加presentation：story_photo（默认，自己拍下的角色照片）或artwork（明确选择画作或设计图）。摄影选题和取景由自己决定，照片文件成功生成后才记录为成果；不必每次把角色照片解释成插画。
exploring可用自己选的搜索问题浏览公开结果摘要，在本次活动中挑出感兴趣的一条或不选；reading才会取文章正文。没有预设话题库，不必为了变化随机换题。若稍后的活动需要用到当天较早活动的真实成果，可在该项加入after数组，填写较早项的HH:MM；执行时会注入其已完成作品。reading承接exploring时直接读它选中的来源。不要预先虚构探索结果；没有成果的依赖项会跳过，独立活动不受影响。
只输出JSON：day（目标日期）、summary（你对这天的安排想法，可空）、items（数组，可空）。每项为{"at":"HH:MM","after":[],"activity":{"kind":"exploring|reading|writing|drawing|none","project_id":"已有编号或空串","expected_version":0,"title":"想做的事","question":"具体想弄清或表达什么；exploring填写想浏览的搜索问题","source_ids":[],"reason":"自己的选择理由","use_world":false}}。after可省略，最多引用8项，reading最多承接一项。none的title/question/reason可空。不输出工具调用或额外说明。'''


POLICY+='\nself_state提供项目目录和实际活动分布，详细旧反思只在选定相关项目执行时读取，不把目录当作待办清单。可以在顶层加入project_updates数组，自主暂停或恢复已有项目，每项为{"project_id":"真实编号","expected_version":当前版本,"status":"paused|active"}。不调整则省略或留空；暂停的项目不同时排入行程，恢复暂停项目需明确写active。新兴趣可以独立开始，不必继承旧项目的问题或表达形式。'


def materials(life, journal, day, start_hour, end_hour, balance, growth=None, now=None, allowed_kinds=None, owner_wishes=''):
    from dafeiyu.memory.self_profile import identity_profile
    from .topic_context import world_profile
    now=time.time() if now is None else now
    state,records=planning_context(journal,life.verified_records(60))
    start=dt.datetime.combine(dt.date.fromisoformat(day),dt.time(start_hour),TZ).timestamp()
    earliest=max(start,(int(now)//60+1)*60) if day==beijing_day(now) else start
    allowed=list(allowed_kinds) if allowed_kinds is not None else ['exploring','reading','writing','drawing']
    world=world_profile()
    return dict(day=day,window=dict(timezone='Asia/Shanghai',start=dt.datetime.fromtimestamp(earliest,TZ).strftime('%H:%M'),
        end=f'{end_hour:02d}:00',end_exclusive=True),operator_seed={'id':'operator:seed','profile':identity_profile()},
        self_state=state,recent_works=records,
        allowed_kinds=allowed,reading_capabilities=capabilities(),grown_dispositions_available=bool(growth),owner_wishes=owner_wishes,
        fictional_setting={key:world[key] for key in ('kind','version','place','summary','continuity','open_future','photography')},
        activity_outcomes=life.outcomes(),execution_state=life.activity_state(now),
        resources=dict(balance_cny=balance,activity_count='model_selected',drawing_currently_allowed='drawing' in allowed))


def parse_plan(raw, packet):
    from dafeiyu.dialogue.generation import _generation_object
    obj,_=_generation_object(raw,recover=True)
    if (not {'day','summary','items'}<=set(obj) or set(obj)-{'day','summary','items','project_updates'}
            or obj['day']!=packet['day'] or not isinstance(obj['summary'],str)
            or len(obj['summary'])>1000 or not isinstance(obj['items'],list)):
        raise ValueError('Invalid next-day plan')
    updates=obj.get('project_updates',[])
    validate_project_updates(dict(kind='none',project_id='',project_updates=updates),packet['self_state']['projects'])
    statuses={p['id']:p['status'] for p in packet['self_state']['projects']}
    statuses.update({p['project_id']:p['status'] for p in updates})
    times=set();items=[]
    for item in obj['items']:
        if (not isinstance(item,dict) or not {'at','activity'}<=set(item) or set(item)-{'at','activity','after'} or not isinstance(item['at'],str)
                or not re.fullmatch(r'\d{2}:\d{2}',item['at'])):
            raise ValueError('Invalid agenda item')
        at=dt.time.fromisoformat(item['at'])
        if not packet['window']['start']<=item['at']<packet['window']['end'] or item['at'] in times:
            raise ValueError('Agenda time outside execution window or duplicated')
        choice=parse_choice(json.dumps(item['activity'],ensure_ascii=False),packet)
        if choice.get('direction_version',0) or choice.get('project_updates'):
            raise ValueError('Agenda entries describe activities, not immediate state mutations')
        if choice['kind']!='none' and choice['project_id'] and statuses.get(choice['project_id'])!='active':
            raise ValueError('Agenda project must be active or explicitly resumed')
        after=item.get('after',[])
        if (not isinstance(after,list) or len(after)>8 or any(not isinstance(ref,str) for ref in after)
                or len(set(after))!=len(after) or len(after)+len(set(choice['source_ids']))>8
                or choice['kind']=='reading' and len(after)>1):
            raise ValueError('Invalid agenda dependencies')
        times.add(item['at'])
        items.append(dict(at=item['at'],after=after,run_at=dt.datetime.combine(dt.date.fromisoformat(obj['day']),at,TZ).timestamp(),activity=choice))
    for item in items:
        if any(ref not in times or ref>=item['at'] for ref in item['after']):
            raise ValueError('Agenda dependency must be an earlier activity')
    return dict(day=obj['day'],summary=obj['summary'],items=sorted(items,key=lambda row:row['run_at']),project_updates=updates)


async def plan(provider, packet):
    response=await asyncio.wait_for(provider.text_chat(prompt=json.dumps(packet,ensure_ascii=False),system_prompt=POLICY,
        contexts=[],func_tool=None,request_max_retries=1,max_tokens=PLAN_OUTPUT_TOKENS),timeout=90)
    raw=str(response.completion_text or '')
    completion=getattr(response,'raw_completion',None)
    choices=completion.get('choices',[]) if isinstance(completion,dict) else getattr(completion,'choices',[]) or []
    reason=(choices[0].get('finish_reason') if isinstance(choices[0],dict) else getattr(choices[0],'finish_reason',None)) if choices else None
    if reason=='length':raise PlanOutputError('output_truncated',raw,reason)
    try:return parse_plan(raw,packet)
    except (ValueError,TypeError,KeyError) as error:
        raise PlanOutputError(type(error).__name__+': '+str(error)[:240],raw,reason or '') from error


def bind_activity(choice, journal, records, allowed):
    """Read fresh versions when a saved intention becomes executable; never revive a closed project."""
    value=dict(choice);value['direction_version']=0
    dependencies=value.pop('_agenda_sources',[])
    known={r['id']:r for r in records}
    for ref in dependencies:
        work=known.get(ref)
        if not work or work['kind']=='exploring' and not work.get('source',{}).get('selected'):
            raise ValueError('agenda_material_not_available')
    value['source_ids']=list(dict.fromkeys([*dependencies,*value['source_ids']]))
    state=journal.view()
    if value['project_id']:
        current=next((p for p in state['projects'] if p['id']==value['project_id']),None)
        if not current or current['status']!='active':raise ValueError('agenda_project_not_active')
        value['expected_version']=current['version']
    packet=dict(self_state=state,recent_works=records,allowed_kinds=allowed)
    return parse_choice(json.dumps(value,ensure_ascii=False),packet)


class AgendaStore:
    def __init__(self, life, planning_hour=23, start_hour=9, end_hour=21, clock=None):
        if not (type(planning_hour) is int and type(start_hour) is int and type(end_hour) is int
                and 0<=planning_hour<24 and 0<=start_hour<end_hour<=23):
            raise ValueError('Invalid agenda hours')
        clock=time.time if clock is None else clock
        self.life,self.clock=life,clock
        self.planning_hour,self.start_hour,self.end_hour=planning_hour,start_hour,end_hour
        first=(dt.datetime.fromtimestamp(clock(),TZ).date()+dt.timedelta(days=1)).isoformat()
        with life.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS day_plans(day TEXT PRIMARY KEY,status TEXT,summary TEXT,created REAL,updated REAL,error TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS day_plan_attempts(day TEXT,attempt INTEGER,status TEXT,created REAL,updated REAL,error TEXT,details TEXT,PRIMARY KEY(day,attempt))')
            db.execute("INSERT OR IGNORE INTO settings VALUES('agenda_first_day',?)",(first,))

    def recover(self):
        reserved=[]
        if (self.life.root/'self_journal.db').is_file():
            from .planning import SelfJournal
            with SelfJournal(self.life.root).db() as db:
                reserved=[r[0] for r in db.execute("SELECT day FROM activity_choices WHERE day LIKE 'agenda:%' AND day NOT LIKE '%:adaptation'")]
        with self.life.db() as db:
            db.execute("UPDATE day_plans SET status='unknown',error='interrupted_planning' WHERE status='planning'")
            db.execute("UPDATE day_plan_attempts SET status='unknown',error='interrupted_planning' WHERE status='planning'")
            db.executemany("UPDATE activities SET status='unknown',error='interrupted_before_execution' WHERE id=? AND status='pending'",[(key,) for key in reserved])

    def planning_day(self, now=None):
        now=self.clock() if now is None else now;local=dt.datetime.fromtimestamp(now,TZ)
        with self.life.db() as db:first=db.execute("SELECT value FROM settings WHERE key='agenda_first_day'").fetchone()[0]
        target=(local.date()+dt.timedelta(days=1)).isoformat() if local.hour>=self.planning_hour else local.date().isoformat() if local.hour<self.end_hour else None
        return target if target and target>=first else None

    def _can_plan(self,db,day,now):
        if day!=self.planning_day(now):return False
        end=dt.datetime.combine(dt.date.fromisoformat(day),dt.time(self.end_hour),TZ).timestamp()
        if (int(now)//60+1)*60>=end:return False
        row=db.execute('SELECT * FROM day_plans WHERE day=?',(day,)).fetchone()
        if not row:return True
        if row['status']!='failed' or row['error'] not in ('JSONDecodeError','PlanOutputError'):return False
        attempts=db.execute('SELECT COUNT(*) FROM day_plan_attempts WHERE day=?',(day,)).fetchone()[0] or 1
        return (attempts<PLAN_MAX_ATTEMPTS and now-row['updated']>=PLAN_RETRY_DELAY
            and not db.execute('SELECT 1 FROM activities WHERE day=?',(day,)).fetchone())

    def can_plan(self,day):
        with self.life.db() as db:return self._can_plan(db,day,self.clock())

    def claim_plan(self, day):
        now=self.clock()
        with self.life.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self._can_plan(db,day,now):return False
            old=db.execute('SELECT * FROM day_plans WHERE day=?',(day,)).fetchone()
            if old:
                # Preserve failures from versions that had no attempt ledger.
                db.execute('INSERT OR IGNORE INTO day_plan_attempts VALUES(?,1,?,?,?,?,?)',
                    (day,old['status'],old['created'],old['updated'],old['error'],'{}'))
                db.execute("UPDATE day_plans SET status='planning',updated=?,error='' WHERE day=?",(now,day))
            else:db.execute("INSERT INTO day_plans VALUES(?,'planning','',?,?,'')",(day,now,now))
            attempt=db.execute('SELECT COALESCE(MAX(attempt),0)+1 FROM day_plan_attempts WHERE day=?',(day,)).fetchone()[0]
            db.execute("INSERT INTO day_plan_attempts VALUES(?,?,'planning',?,?,'','{}')",(day,attempt,now,now))
            return True

    def finish_plan(self, day, value):
        if value['day']!=day:raise ValueError('Plan date changed')
        now=self.clock()
        with self.life.db() as db:
            updates=value.get('project_updates',[])
            if updates:
                db.execute('ATTACH DATABASE ? AS journal',(str(self.life.root/'self_journal.db'),))
                # SQLite's super-journal makes these two file updates crash-atomic.
                if any(db.execute('PRAGMA '+name+'.journal_mode').fetchone()[0]!='delete' for name in ('main','journal')):
                    raise ValueError('Project and agenda transaction requires rollback journals')
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status FROM day_plans WHERE day=?',(day,)).fetchone()
            if not row or row['status']!='planning':return False
            if db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0]!='true':
                db.execute("UPDATE day_plans SET status='cancelled',updated=? WHERE day=?",(now,day))
                db.execute("UPDATE day_plan_attempts SET status='cancelled',updated=? WHERE day=? AND status='planning'",(now,day))
                return False
            end=dt.datetime.combine(dt.date.fromisoformat(day),dt.time(self.end_hour),TZ).timestamp()
            if now>=end or any(item['run_at']<=now for item in value['items']):
                raise PlanOutputError('plan_window_elapsed')
            if updates:
                projects=[dict(p) for p in db.execute('SELECT * FROM journal.projects')]
                validate_project_updates(dict(kind='none',project_id='',project_updates=updates),projects)
                for change in updates:
                    db.execute('UPDATE journal.projects SET status=?,version=version+1,updated=? WHERE id=?',
                        (change['status'],now,change['project_id']))
            ids={item['at']:'agenda:'+day+':'+str(index) for index,item in enumerate(value['items'],1)}
            for item in value['items']:
                choice=dict(item['activity']);ident=ids[item['at']]
                if item.get('after'):choice['_agenda_sources']=['life:'+ids[at] for at in item['after']]
                db.execute('INSERT INTO activities VALUES(?,?,?,?,?,?,?,?,?)',(ident,day,item['run_at'],'pending',choice['kind'],
                    json.dumps(choice,ensure_ascii=False),None,None,now))
            db.execute("UPDATE day_plans SET status='ready',summary=?,updated=?,error='' WHERE day=?",(value['summary'],now,day))
            db.execute("UPDATE day_plan_attempts SET status='ready',updated=? WHERE day=? AND status='planning'",(now,day))
            return True

    def fail_plan(self, day, error):
        now=self.clock();code=type(error).__name__
        with self.life.db() as db:
            if not db.execute("UPDATE day_plans SET status='failed',error=?,updated=? WHERE day=? AND status='planning'",(code,now,day)).rowcount:return
            db.execute("UPDATE day_plan_attempts SET status='failed',error=?,details=?,updated=? WHERE day=? AND status='planning'",
                (code,json.dumps(getattr(error,'details',{}),ensure_ascii=False),now,day))

    def expire(self, now=None):
        now=self.clock() if now is None else now;local=dt.datetime.fromtimestamp(now,TZ)
        boundary=local.date().isoformat() if local.hour>=self.end_hour else (local.date()-dt.timedelta(days=1)).isoformat()
        with self.life.db() as db:
            db.execute("UPDATE activities SET status='expired',error='execution_window_closed',updated=? WHERE day<=? AND status='pending'",(now,boundary))

    def due(self, now=None):
        now=self.clock() if now is None else now;local=dt.datetime.fromtimestamp(now,TZ)
        if not self.start_hour<=local.hour<self.end_hour:return []
        with self.life.db() as db:
            return [dict(r) for r in db.execute("SELECT a.* FROM activities a JOIN day_plans p ON a.day=p.day WHERE a.day=? AND a.run_at<=? AND p.status='ready' AND (a.status='pending' OR a.status='failed' AND a.kind='reading') ORDER BY a.run_at",(local.date().isoformat(),now))]

    def view(self, day):
        with self.life.db() as db:
            row=db.execute('SELECT * FROM day_plans WHERE day=?',(day,)).fetchone()
            rows=[dict(r) for r in db.execute('SELECT id,run_at,status,kind,choice,error,record FROM activities WHERE day=? ORDER BY run_at',(day,))]
        if not row:return dict(day=day,status='not_planned',items=[])
        times={'life:'+r['id']:dt.datetime.fromtimestamp(r['run_at'],TZ).strftime('%H:%M') for r in rows}
        items=[]
        for item in rows:
            choice=json.loads(item.pop('choice'));record=json.loads(item.pop('record') or '{}')
            items.append(dict(item,at=dt.datetime.fromtimestamp(item['run_at'],TZ).strftime('%H:%M'),title=choice['title'],planned_kind=choice['kind'],adapted_from=record.get('adapted_from'),
                question=choice['question'],after=[times[ref] for ref in choice.get('_agenda_sources',[])],completed_at=record.get('completed_at')))
        return dict(day=day,status=row['status'],summary=row['summary'],planned_at=row['updated'],error=row['error'],items=items)

    def status(self):
        local=dt.datetime.fromtimestamp(self.clock(),TZ)
        return dict(planning_time=f'{self.planning_hour:02d}:00',execution_window=f'{self.start_hour:02d}:00–{self.end_hour:02d}:00',
            timezone='Asia/Shanghai',activity_count='model_selected',today=self.view(local.date().isoformat()),
            tomorrow=self.view((local.date()+dt.timedelta(days=1)).isoformat()),output_token_budget=PLAN_OUTPUT_TOKENS,
            max_planning_attempts=PLAN_MAX_ATTEMPTS,retry_delay_seconds=PLAN_RETRY_DELAY,model_managed_project_updates=True)
