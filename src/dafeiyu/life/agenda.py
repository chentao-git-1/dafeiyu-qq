"""One persisted night plan, independently claimed daytime activities, no daily count quota."""
import asyncio
import datetime as dt
import json
import re
import time

from .planning import interest_context, parse_choice
from .reading import capabilities
from .store import TZ, beijing_day


POLICY = '''为哦鲸鲸安排目标日期的自主数字活动。夜间先想好，白天按这份计划逐项推进。活动数量、主题、形式和具体时间由你决定，可以留空、休息或继续已有兴趣，不必填满一天，也没有每天一项或三项的固定配额。
结合自己的兴趣、近期作品、项目状态、真实失败结果、可用时间及余额安排。旧倾向只作情境参考，不必重复旧题目。可以提出没有旧来源的新兴趣；只在实际引用旧材料时填写source_ids，想象和意向不能当作已完成经历或外部事实。资料不含他人的私聊，也不授权增加公开发布、私信、提醒或其他操作。
只安排allowed_kinds中的活动或none（休息）；每项都在给定北京时间窗口内，按时间先后，时间不重复。执行时仍检查服务可用性和余额，资金不足会等待。选择项目时用真实编号和当前版本；新项目留空编号、版本0。不同时间可以继续同一项目，实际执行时会读取那时的最新进展；已被暂停或结束的项目不会擅自恢复。
当天执行结果会进入下一次规划；未取得资料与研究没有答案不同，阅读失败后的替代文字也不是研究发现。计划不是已经做过的事，不虚构现实出门、吃饭或身体生活。正常聊天照常，不为了执行计划装忙或催人。
owner_wishes是主人当前希望生活更丰富的方向；初始爱好只是起点，不是题材范围。recent_activity_distribution如实描述近期活动分布，是否继续深入、暂放或接触新东西由你决定。
fictional_setting是已经确立的角色生活背景，可以自主选择use_world来发展故事，也可以做无关的数字活动。城市住处与过去的来历不是今天已完成活动，不据此安排真实出行或把背景地点轮流排进日程。
drawing也可以是角色生活中的拍照，只有这种图片活动可添加presentation：story_photo（默认，自己拍下的角色照片）或artwork（明确选择画作或设计图）。摄影选题和取景由自己决定，照片文件成功生成后才记录为成果；不必每次把角色照片解释成插画。
exploring可用自己选的搜索问题浏览公开结果摘要，在本次活动中挑出感兴趣的一条或不选；reading才会取文章正文。没有预设话题库，不必为了变化随机换题。若稍后的活动需要用到当天较早活动的真实成果，可在该项加入after数组，填写较早项的HH:MM；执行时会注入其已完成作品。reading承接exploring时直接读它选中的来源。不要预先虚构探索结果；没有成果的依赖项会跳过，独立活动不受影响。
只输出JSON：day（目标日期）、summary（你对这天的安排想法，可空）、items（数组，可空）。每项为{"at":"HH:MM","after":[],"activity":{"kind":"exploring|reading|writing|drawing|none","project_id":"已有编号或空串","expected_version":0,"title":"想做的事","question":"具体想弄清或表达什么；exploring填写想浏览的搜索问题","source_ids":[],"reason":"自己的选择理由","use_world":false}}。after可省略，最多引用8项，reading最多承接一项。none的title/question/reason可空。不输出工具调用或额外说明。'''


def materials(life, journal, day, start_hour, end_hour, balance, growth=None, now=None, allowed_kinds=None, owner_wishes=''):
    from dafeiyu.memory.self_profile import self_profile
    from .topic_context import world_profile
    now=time.time() if now is None else now
    state,records=interest_context(journal,life.verified_records(60))
    start=dt.datetime.combine(dt.date.fromisoformat(day),dt.time(start_hour),TZ).timestamp()
    earliest=max(start,(int(now)//60+1)*60) if day==beijing_day(now) else start
    allowed=list(allowed_kinds) if allowed_kinds is not None else ['exploring','reading','writing','drawing']
    world=world_profile()
    return dict(day=day,window=dict(timezone='Asia/Shanghai',start=dt.datetime.fromtimestamp(earliest,TZ).strftime('%H:%M'),
        end=f'{end_hour:02d}:00',end_exclusive=True),operator_seed={'id':'operator:seed','profile':self_profile()},
        self_state=state,recent_works=[{k:r[k] for k in ('id','day','kind','note','thought','source','activity_context') if k in r} for r in records],
        allowed_kinds=allowed,reading_capabilities=capabilities(),grown_dispositions=growth or {},owner_wishes=owner_wishes,
        fictional_setting={key:world[key] for key in ('kind','version','place','summary','continuity','open_future','photography')},
        activity_outcomes=life.outcomes(),execution_state=life.activity_state(now),
        resources=dict(balance_cny=balance,activity_count='model_selected',drawing_currently_allowed='drawing' in allowed))


def parse_plan(raw, packet):
    from dafeiyu.dialogue.retrieval import json_object
    obj=json_object(raw)
    if (set(obj)!={'day','summary','items'} or obj['day']!=packet['day'] or not isinstance(obj['summary'],str)
            or len(obj['summary'])>1000 or not isinstance(obj['items'],list)):
        raise ValueError('Invalid next-day plan')
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
    return dict(day=obj['day'],summary=obj['summary'],items=sorted(items,key=lambda row:row['run_at']))


async def plan(provider, packet):
    response=await asyncio.wait_for(provider.text_chat(prompt=json.dumps(packet,ensure_ascii=False),system_prompt=POLICY,
        contexts=[],func_tool=None,request_max_retries=1),timeout=45)
    return parse_plan(response.completion_text,packet)


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
            db.execute("INSERT OR IGNORE INTO settings VALUES('agenda_first_day',?)",(first,))

    def recover(self):
        reserved=[]
        if (self.life.root/'self_journal.db').is_file():
            from .planning import SelfJournal
            with SelfJournal(self.life.root).db() as db:
                reserved=[r[0] for r in db.execute("SELECT day FROM activity_choices WHERE day LIKE 'agenda:%' AND day NOT LIKE '%:adaptation'")]
        with self.life.db() as db:
            db.execute("UPDATE day_plans SET status='unknown',error='interrupted_planning' WHERE status='planning'")
            db.executemany("UPDATE activities SET status='unknown',error='interrupted_before_execution' WHERE id=? AND status='pending'",[(key,) for key in reserved])

    def planning_day(self, now=None):
        now=self.clock() if now is None else now;local=dt.datetime.fromtimestamp(now,TZ)
        with self.life.db() as db:first=db.execute("SELECT value FROM settings WHERE key='agenda_first_day'").fetchone()[0]
        target=(local.date()+dt.timedelta(days=1)).isoformat() if local.hour>=self.planning_hour else local.date().isoformat() if local.hour<self.end_hour else None
        return target if target and target>=first else None

    def claim_plan(self, day):
        now=self.clock()
        with self.life.db() as db:
            return db.execute("INSERT OR IGNORE INTO day_plans VALUES(?,'planning','',?,?,'')",(day,now,now)).rowcount==1

    def finish_plan(self, day, value):
        if value['day']!=day:raise ValueError('Plan date changed')
        now=self.clock()
        with self.life.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status FROM day_plans WHERE day=?',(day,)).fetchone()
            if not row or row['status']!='planning':return False
            if not self.life.enabled():
                db.execute("UPDATE day_plans SET status='cancelled',updated=? WHERE day=?",(now,day));return False
            ids={item['at']:'agenda:'+day+':'+str(index) for index,item in enumerate(value['items'],1)}
            for item in value['items']:
                choice=dict(item['activity']);ident=ids[item['at']]
                if item.get('after'):choice['_agenda_sources']=['life:'+ids[at] for at in item['after']]
                db.execute('INSERT INTO activities VALUES(?,?,?,?,?,?,?,?,?)',(ident,day,item['run_at'],'pending',choice['kind'],
                    json.dumps(choice,ensure_ascii=False),None,None,now))
            db.execute("UPDATE day_plans SET status='ready',summary=?,updated=? WHERE day=?",(value['summary'],now,day))
            return True

    def fail_plan(self, day, error):
        with self.life.db() as db:db.execute("UPDATE day_plans SET status='failed',error=?,updated=? WHERE day=? AND status='planning'",
            (type(error).__name__,self.clock(),day))

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
            tomorrow=self.view((local.date()+dt.timedelta(days=1)).isoformat()))
