"""Interest relevance, atomic project choices and real adapter wire budgets; offline only."""
import asyncio
from collections import Counter
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch

from test_nightly_agenda import choice,day_plan,fixture,response,stamp
import test_topic_variety as topic
from dafeiyu.dialogue.self_context import default_character,relevant_reflections,relevant_growth
from dafeiyu.life.agenda import AgendaStore,materials,parse_plan,plan,PlanOutputError,PLAN_OUTPUT_TOKENS
from dafeiyu.life.planning import SelfJournal,creation_context
from data.plugins.dafeiyu_life.life import LifeStore
from data.plugins.dafeiyu_dialogue.metering import MeteredProvider
from data.plugins.dafeiyu_dialogue.provider_contract import ProviderContracts,STRUCTURED_MARKER
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from openai.types.chat import ChatCompletion


class InterestContext(unittest.TestCase):
    def test_global_project_tendencies_are_on_demand_but_personal_preferences_survive(self):
        old=dict(version=4,tendencies=[dict(tendency='手帐要保留空白',when='研究植物',counterweight='仍然查证')])
        personal=dict(version=2,tendencies=[dict(tendency='认真讨论时说清楚依据')])
        original=dict(stable_profile=dict(name='鲸鲸',likes=['手帐']),growth=dict(global_=old))
        original['growth']={'global':old,'with_current_person':personal};before=copy.deepcopy(original)
        projected=default_character(original)
        self.assertEqual(projected['growth']['global']['tendencies'],[])
        self.assertTrue(projected['growth']['global']['details_available'])
        self.assertEqual(projected['growth']['with_current_person'],personal)
        self.assertEqual(original,before)
        self.assertEqual(relevant_growth(original['growth'],'植物')['global']['tendencies'],old['tendencies'])
        self.assertEqual(relevant_growth(original['growth'],'音乐')['global']['tendencies'],[])

    def test_old_reflection_only_matches_requested_project_without_topic_bans(self):
        state=dict(projects=[dict(id='p1',title='植物光线',question='待查旧问题',status='paused')],
            reflections=[dict(work_id='w1',project_id='p1',insight='手帐留白',next_question='仍待查证'),
                         dict(work_id='w2',project_id='p2',insight='音乐和节奏',next_question='速度如何影响表达')],
            intentions=[dict(title='当下想法')])
        self.assertEqual(relevant_reflections(state,'')['reflections'],[])
        self.assertEqual(relevant_reflections(state,'今天午餐')['reflections'],[])
        self.assertEqual(relevant_reflections(state,'植物光线')['reflections'][0]['work_id'],'w1')
        self.assertEqual(relevant_reflections(state,'音乐')['reflections'][0]['work_id'],'w2')
        self.assertEqual(relevant_reflections(state,'')['intentions'],state['intentions'])

    def test_planning_gets_project_index_and_selected_execution_keeps_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            life=LifeStore(Path(tmp));journal=SelfJournal(life.root)
            project=journal.begin_project(choice('手帐里的植物',question='独有旧问题'))
            with journal.db() as db:
                db.execute('INSERT INTO reflections VALUES(?,?,?,?,?,?,?)',
                    ('w1','hash',project['id'],'独有旧反思','独有旧追问','[]',stamp()))
            record=dict(id='w1',day='2026-11-06',kind='writing',project_id=project['id'],project=project['title'],
                note='独有作品全文',thought='独有旧想法',source={})
            with patch.object(life,'verified_records',return_value=[record]):
                packet=materials(life,journal,'2026-11-07',9,21,'2.00',growth={'global':'独有全局倾向'},now=stamp())
            text=json.dumps(packet,ensure_ascii=False)
            self.assertIn(project['title'],text)
            for old in ('独有旧问题','独有旧反思','独有旧追问','独有作品全文','独有旧想法','独有全局倾向'):
                self.assertNotIn(old,text)
            self.assertNotIn('likes',packet['operator_seed']['profile'])
            self.assertEqual(parse_plan(json.dumps(day_plan(1)),packet)['items'][0]['activity']['source_ids'],[])
            selected=choice(project_id=project['id'],expected_version=1)
            works,context=creation_context(journal,[record],selected,project)
            self.assertEqual(works,[record]);self.assertEqual(context['reflections'][0]['insight'],'独有旧反思')
            new=journal.begin_project(choice('另一个兴趣'))
            self.assertEqual(creation_context(journal,[record],choice('另一个兴趣'),new)[0],[])


class CurrentInterestTools(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=topic.OnDemandContext.asyncSetUp
    asyncTearDown=topic.OnDemandContext.asyncTearDown
    run_hooks=topic.OnDemandContext.run_hooks
    ready=topic.OnDemandContext.ready
    install_life=topic.OnDemandContext.install_life

    async def test_current_thoughts_do_not_read_old_project_and_explicit_query_finds_paused_interest(self):
        store=self.install_life();journal=SelfJournal(store.root)
        project=journal.view()['projects'][0]
        journal.begin_project(choice(kind='none',project_updates=[dict(project_id=project['id'],expected_version=1,status='paused')]))
        event,_=await self.ready('你有什么想法')
        current=json.loads(await self.plugin.self_details(event,'thoughts'))['data']
        self.assertEqual(current['projects'],[])
        related=json.loads(await self.plugin.self_details(event,'thoughts','手帐独有旧项目'))['data']
        self.assertEqual(related['projects'][0]['status'],'paused')
        interests=json.loads(await self.plugin.self_details(event,'interests'))['data']
        self.assertTrue(interests['initial_interests_optional'])
        self.assertEqual(interests['current_projects'][0]['status'],'paused')
        self.assertNotIn('question',interests['current_projects'][0])
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()
        event.send.assert_not_awaited()


class PlanTransactions(unittest.TestCase):
    def setup_store(self,tmp):
        life=LifeStore(Path(tmp),model_schedule=True);now=[stamp()];store=AgendaStore(life,clock=lambda:now[0])
        journal=SelfJournal(life.root);project=journal.begin_project(choice('可以暂放的旧兴趣'))
        packet=materials(life,journal,'2026-11-07',9,21,'2.00',now=now[0])
        return life,now,store,journal,project,packet

    def test_model_can_pause_old_project_while_starting_new_interest_then_resume_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            life,now,store,journal,project,packet=self.setup_store(tmp)
            change=dict(project_id=project['id'],expected_version=1,status='paused')
            value=parse_plan(json.dumps(day_plan(1,project_updates=[change])),packet)
            self.assertTrue(store.claim_plan(value['day']));self.assertTrue(store.finish_plan(value['day'],value))
            self.assertEqual(journal.view()['projects'][0]['status'],'paused')
            self.assertEqual(len(store.view(value['day'])['items']),1)
            now[0]=stamp(7,23);packet=materials(life,journal,'2026-11-08',9,21,'2.00',now=now[0])
            next_plan=day_plan(1,day='2026-11-08')
            next_plan['items'][0]['activity']=choice(project_id=project['id'],expected_version=2)
            with self.assertRaises(ValueError):parse_plan(json.dumps(next_plan),packet)
            next_plan['project_updates']=[dict(change,expected_version=2,status='active')]
            value=parse_plan(json.dumps(next_plan),packet)
            self.assertTrue(store.claim_plan(value['day']));self.assertTrue(store.finish_plan(value['day'],value))
            self.assertEqual(journal.view()['projects'][0]['status'],'active')
            self.assertEqual(journal.view()['projects'][0]['version'],3)

    def test_cannot_pause_a_project_and_schedule_it_in_same_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,_,_,_,project,packet=self.setup_store(tmp)
            value=day_plan(1,project_updates=[dict(project_id=project['id'],expected_version=1,status='paused')])
            value['items'][0]['activity']=choice(project_id=project['id'],expected_version=1)
            with self.assertRaises(ValueError):parse_plan(json.dumps(value),packet)

    def test_project_and_agenda_roll_back_together_when_activity_insert_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            life,_,store,journal,project,packet=self.setup_store(tmp)
            value=parse_plan(json.dumps(day_plan(1,project_updates=[dict(project_id=project['id'],expected_version=1,status='paused')])),packet)
            store.claim_plan(value['day'])
            with life.db() as db:
                db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON activities BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END")
            with self.assertRaises(sqlite3.IntegrityError):store.finish_plan(value['day'],value)
            self.assertEqual(journal.view()['projects'][0]['status'],'active')
            self.assertEqual(journal.view()['projects'][0]['version'],1)
            self.assertEqual(store.view(value['day'])['items'],[])
            self.assertEqual(store.view(value['day'])['status'],'planning')

    def test_stale_project_update_and_elapsed_schedule_are_not_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,now,store,journal,project,packet=self.setup_store(tmp)
            update=dict(project_id=project['id'],expected_version=1,status='paused')
            value=parse_plan(json.dumps(day_plan(1,project_updates=[update])),packet)
            store.claim_plan(value['day']);journal.begin_project(choice(kind='none',project_updates=[update]))
            with self.assertRaises(ValueError):store.finish_plan(value['day'],value)
            self.assertEqual(store.view(value['day'])['items'],[])
            now[0]=stamp(7,9)
            with self.assertRaises(PlanOutputError):store.finish_plan(value['day'],dict(value,project_updates=[]))
            self.assertEqual(store.view(value['day'])['items'],[])


class PlanRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_old_failed_day_gets_one_remaining_window_plan_with_failure_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];provider=NS(text_chat=AsyncMock())
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider)
                with life.db() as db:db.execute("INSERT INTO day_plans VALUES(?,'failed','',?,?,'JSONDecodeError')",('2026-11-07',now[0],now[0]))
                now[0]=stamp(7,12);value=day_plan(1);value['items'][0]['at']='13:00'
                provider.text_chat.return_value=response(value)
                self.assertTrue(await engine.plan_next_day());provider.text_chat.assert_awaited_once()
                packet=json.loads(provider.text_chat.call_args.kwargs['prompt'])
                self.assertEqual(packet['window']['start'],'12:01')
                self.assertFalse(await engine.plan_next_day());self.assertEqual(life.count(),0)
                with life.db() as db:attempts=[dict(r) for r in db.execute('SELECT * FROM day_plan_attempts ORDER BY attempt')]
                self.assertEqual([r['status'] for r in attempts],['failed','ready'])
                self.assertEqual(attempts[0]['error'],'JSONDecodeError')

    async def test_known_invalid_output_keeps_private_diagnostics_and_never_accepts_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];value=day_plan(0)
            provider=NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(value),raw_completion={'choices':[{'finish_reason':'length'}]})))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider)
                with self.assertRaises(PlanOutputError):await engine.plan_next_day()
                self.assertFalse(await engine.plan_next_day());provider.text_chat.assert_awaited_once()
                self.assertEqual(engine.agenda_status()['tomorrow']['status'],'failed')
                with life.db() as db:details=json.loads(db.execute('SELECT details FROM day_plan_attempts').fetchone()[0])
                self.assertEqual(details['finish_reason'],'length');self.assertEqual(details['reason'],'output_truncated')
                self.assertEqual(json.loads(details['response']),value)
                self.assertNotIn('response',engine.agenda_status()['tomorrow'])

    async def test_only_unambiguous_complete_json_can_be_locally_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            life=LifeStore(Path(tmp));packet=materials(life,SelfJournal(life.root),'2026-11-07',9,21,'2',now=stamp())
            raw=json.dumps(day_plan(1))
            for valid in (raw+'}',raw+raw,'```json\n'+raw+'\n```'):
                provider=NS(text_chat=AsyncMock(return_value=NS(completion_text=valid)))
                self.assertEqual(len((await plan(provider,packet))['items']),1);provider.text_chat.assert_awaited_once()
            for invalid in (raw[:-4],raw+json.dumps(day_plan(2)),raw.replace('"summary":','"summary":"conflict","summary":')):
                provider=NS(text_chat=AsyncMock(return_value=NS(completion_text=invalid)))
                with self.assertRaises(PlanOutputError):await plan(provider,packet)
                provider.text_chat.assert_awaited_once()

    async def test_unknown_interruption_and_transport_failure_do_not_automatically_rebill(self):
        for failure in ('unknown','failed'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                now=[stamp()];provider=NS(text_chat=AsyncMock())
                with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                    life,engine=fixture(Path(tmp),now,provider)
                    with life.db() as db:db.execute('INSERT INTO day_plans VALUES(?,?,?,?,?,?)',
                        ('2026-11-07',failure,'',now[0],now[0],'TimeoutError'))
                    now[0]=stamp(7,12);self.assertFalse(await engine.plan_next_day());provider.text_chat.assert_not_awaited()


class PlanningWireBudget(unittest.IsolatedAsyncioTestCase):
    async def test_real_adapter_overrides_512_after_config_merge_without_changing_concurrent_chat(self):
        provider=ProviderOpenAIOfficial({'id':'synthetic','type':'openai_chat_completion','model':'synthetic',
            'key':['synthetic'],'modalities':['text'],'custom_extra_body':{'max_tokens':512}}, {})
        original=provider._apply_provider_specific_request_overrides;config=copy.deepcopy(provider.provider_config)
        contracts=ProviderContracts();contracts.install(provider);stats=Counter();requests=[]
        async def create(**kwargs):
            await asyncio.sleep(0);requests.append(copy.deepcopy(kwargs))
            return ChatCompletion(id='synthetic',object='chat.completion',created=0,model='synthetic',
                choices=[dict(index=0,finish_reason='stop',message=dict(role='assistant',content='{}'))])
        try:
            with patch.object(provider.client.chat.completions,'create',new=AsyncMock(side_effect=create)):
                await asyncio.gather(MeteredProvider(provider,stats,'agenda').text_chat(prompt='plan',system_prompt='JSON'),
                    provider.text_chat(prompt='chat',system_prompt=STRUCTURED_MARKER))
                planned=next(p for p in requests if p.get('max_tokens')==PLAN_OUTPUT_TOKENS)
                ordinary=next(p for p in requests if p.get('max_tokens')!=PLAN_OUTPUT_TOKENS)
                self.assertNotIn('max_tokens',planned['extra_body'])
                self.assertEqual(planned['response_format'],{'type':'json_object'})
                self.assertEqual(ordinary['extra_body']['max_tokens'],512)
                self.assertNotIn('response_format',ordinary)
                await MeteredProvider(provider,stats,'activity').text_chat(prompt='activity',system_prompt='JSON')
                self.assertEqual(requests[-1]['extra_body']['max_tokens'],512)
                self.assertEqual(provider.provider_config,config)
                self.assertEqual(stats['agenda_calls'],1)
        finally:
            contracts.close();self.assertEqual(provider._apply_provider_specific_request_overrides,original)
            await provider.terminate()


if __name__=='__main__':unittest.main()
