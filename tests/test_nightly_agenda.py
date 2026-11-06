"""Night planning, multi-activity execution and recovery; fake models and transports only."""
import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import test_cognition as base
from test_execution_autonomy import generated
from dafeiyu.life.agenda import AgendaStore, materials, parse_plan
from dafeiyu.life.planning import SelfJournal
from dafeiyu.life.reading import ReadingError
from dafeiyu.dialogue.growth import GrowthStore, GLOBAL, own_experiences
from data.plugins.dafeiyu_life.life import LifeStore, LifeEngine, TZ


def stamp(day=6,hour=23,minute=0):
    return dt.datetime(2026,11,day,hour,minute,tzinfo=TZ).timestamp()


def choice(title='一段新的文字', **changes):
    return dict(kind='writing',project_id='',expected_version=0,title=title,question='用自己的方式写这个想法',
        source_ids=[],reason='现在有兴趣试试',use_world=False)|changes


def day_plan(count=3,day='2026-11-07',**changes):
    return dict(day=day,summary='这天想做的几件事',items=[dict(at=f'{9+i:02d}:00',activity=choice('活动'+str(i))) for i in range(count)])|changes


def response(value):
    return NS(completion_text=json.dumps(value,ensure_ascii=False))


def fixture(root,now,provider,balance=None):
    balance=balance if balance is not None else ['2.00']
    controller=NS(owner_policy_enabled=lambda flag:True,natural=lambda:NS(actor_owner=lambda:base.OWNER),
        metered_provider=lambda p,*args:p,growth_profile=AsyncMock(return_value={}))
    watcher=NS(watch=NS(status=lambda:dict(last_balance_cny=balance[0],last_available=True,last_checked_at=now[0])))
    context=NS(get_registered_star=lambda name:NS(activated=True,star_cls={'dafeiyu_dialogue':controller,
        'astrbot_plugin_dafeiyu_balance':watcher}.get(name)),get_using_provider_async=AsyncMock(return_value=provider),
        register_web_api=lambda *args:None)
    life=LifeStore(root,model_schedule=True);engine=LifeEngine(life,context,agenda_enabled=True)
    engine._agenda=AgendaStore(life,clock=lambda:now[0])
    return life,engine


class AgendaPersistence(unittest.TestCase):
    def test_count_is_model_selected_and_times_are_real_beijing_daytime(self):
        with tempfile.TemporaryDirectory() as tmp:
            life=LifeStore(Path(tmp),model_schedule=True);journal=SelfJournal(life.root)
            packet=materials(life,journal,'2026-11-07',9,21,'2.00',now=stamp())
            for count in (0,1,3,5):
                self.assertEqual(len(parse_plan(json.dumps(day_plan(count)),packet)['items']),count)
            self.assertIsNone(packet['execution_state']['daily_activity_limit'])
            for at in ('08:59','21:00','23:00','09:70'):
                value=day_plan(1);value['items'][0]['at']=at
                with self.subTest(at=at),self.assertRaises(ValueError):parse_plan(json.dumps(value),packet)
            value=day_plan(2);value['items'][1]['at']='09:00'
            with self.assertRaises(ValueError):parse_plan(json.dumps(value),packet)
            with self.assertRaises(ValueError):parse_plan(json.dumps(day_plan(1,day='2026-11-08')),packet)

    def test_planning_waits_for_23_and_missed_night_can_recover_before_daytime(self):
        with tempfile.TemporaryDirectory() as tmp:
            life=LifeStore(Path(tmp));now=[stamp(hour=22,minute=59)];store=AgendaStore(life,clock=lambda:now[0])
            self.assertIsNone(store.planning_day())
            now[0]=stamp();self.assertEqual(store.planning_day(),'2026-11-07')
            now[0]=stamp(7,8);reopened=AgendaStore(life,clock=lambda:now[0])
            self.assertEqual(reopened.planning_day(),'2026-11-07')
            now[0]=stamp(7,21);self.assertIsNone(reopened.planning_day())
            now[0]=stamp(7,23);self.assertEqual(reopened.planning_day(),'2026-11-08')

    def test_plan_claim_and_interruption_survive_restart_without_rebilling(self):
        with tempfile.TemporaryDirectory() as tmp:
            life=LifeStore(Path(tmp));now=[stamp()];store=AgendaStore(life,clock=lambda:now[0])
            self.assertTrue(store.claim_plan('2026-11-07'));self.assertFalse(store.claim_plan('2026-11-07'))
            reopened=AgendaStore(life,clock=lambda:now[0]);reopened.recover()
            self.assertEqual(reopened.view('2026-11-07')['status'],'unknown')
            self.assertFalse(reopened.claim_plan('2026-11-07'))

    def test_two_failed_items_at_the_same_time_keep_separate_failure_histories(self):
        with tempfile.TemporaryDirectory() as tmp:
            life=LifeStore(Path(tmp),model_schedule=True);now=[stamp()];store=AgendaStore(life,clock=lambda:now[0])
            packet=materials(life,SelfJournal(life.root),'2026-11-07',9,21,'2.00',now=now[0])
            value=day_plan(2)
            for item in value['items']:item['activity']['kind']='reading'
            store.claim_plan('2026-11-07');store.finish_plan('2026-11-07',parse_plan(json.dumps(value),packet))
            with patch('dafeiyu.life.store.time.time',return_value=stamp(7,10)):
                for item in store.view('2026-11-07')['items']:
                    life.claim(item['id'],'reading');life.fail(item['id'],ReadingError('fetch','timeout'))
                    failed=life.activity(item['id']);life.claim(item['id'],'writing',failed_at=failed['updated'])
                    life.finish(item['id'],{'kind':'writing'})
            failures=life.outcomes();self.assertEqual(len(failures),2)
            self.assertEqual(len({r['activity_id'] for r in failures}),2)
            self.assertEqual(len({r['id'] for r in failures}),2)


class AgendaExecution(unittest.IsolatedAsyncioTestCase):
    async def test_one_night_call_and_three_daytime_works_without_daily_quota_or_replanning(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp(hour=22,minute=59)]
            provider=NS(text_chat=AsyncMock(side_effect=[response(day_plan()),*[response(generated()) for _ in range(3)]]))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider)
                self.assertFalse(await engine.consider_direction());provider.text_chat.assert_not_awaited()
                now[0]=stamp();self.assertTrue(await engine.consider_direction())
                self.assertEqual(own_experiences(life,now=now[0]),[])
                self.assertFalse(await engine.consider_direction());self.assertEqual(provider.text_chat.await_count,1)
                self.assertFalse((await engine.advance(opportunity=True))['advanced'])
                now[0]=stamp(7,8,59);self.assertFalse((await engine.advance())['advanced'])
                for hour in (9,10,11):
                    now[0]=stamp(7,hour);self.assertTrue((await engine.advance())['advanced'])
                    self.assertFalse((await engine.advance())['advanced'])
                self.assertEqual(provider.text_chat.await_count,4)
                self.assertEqual(life.count(),3)
                self.assertEqual(len({r['id'] for r in life.records()}),3)
                self.assertEqual(len({r['artifact'] for r in life.records()}),3)
                self.assertEqual(len(life.verified_records()),3)
                self.assertEqual(life.activity_state()['completed_today'],3)
                self.assertEqual(life.public_context_for_day(now[0])['id'],life.records(1)[0]['id'])
                experiences=own_experiences(life,now=now[0]);self.assertEqual(len(experiences),3)
                self.assertEqual(len({r['episode'] for r in experiences}),3)
                growth=GrowthStore(Path(tmp)/'growth.db',clock=lambda:now[0]);growth.reconcile(GLOBAL,experiences)
                self.assertIsNone(growth.claim(GLOBAL))
                self.assertTrue(all(i['status']=='completed' for i in engine.agenda_status()['today']['items']))

    async def test_same_project_uses_new_version_and_model_pause_stops_later_item(self):
        for pause in (False,True):
            with self.subTest(pause=pause),tempfile.TemporaryDirectory() as tmp:
                now=[stamp()];provider=NS(text_chat=AsyncMock())
                with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                    life,engine=fixture(Path(tmp),now,provider);journal=SelfJournal(life.root)
                    project=journal.begin_project(choice())
                    value=day_plan(2)
                    for item in value['items']:item['activity']=choice(project_id=project['id'],expected_version=1)
                    first=generated();first['reflection']=dict(first['reflection'],status='paused' if pause else 'active')
                    provider.text_chat.side_effect=[response(value),response(first),response(generated())]
                    await engine.plan_next_day();now[0]=stamp(7,9)
                    self.assertTrue((await engine.advance())['advanced'])
                    now[0]=stamp(7,10);result=await engine.advance()
                    self.assertEqual(result['advanced'],not pause)
                    self.assertEqual(life.count(),1 if pause else 2)
                    self.assertEqual(provider.text_chat.await_count,2 if pause else 3)

    async def test_balance_wait_and_expired_day_never_execute_outside_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];balance=['0.50'];provider=NS(text_chat=AsyncMock(return_value=response(day_plan(1))))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider,balance)
                self.assertFalse(await engine.plan_next_day());provider.text_chat.assert_not_awaited()
                balance[0]='2.00';self.assertTrue(await engine.plan_next_day())
                now[0]=stamp(7,9);balance[0]='0.50'
                self.assertEqual((await engine.advance())['status'],'waiting_for_resources')
                self.assertEqual(engine.agenda_status()['today']['items'][0]['status'],'pending')
                now[0]=stamp(7,21);balance[0]='2.00';await engine.advance(opportunity=True)
                self.assertEqual(engine.agenda_status()['today']['items'][0]['status'],'expired')
                now[0]=stamp(8,10);self.assertFalse((await engine.advance())['advanced'])
                self.assertEqual(life.count(),0);provider.text_chat.assert_awaited_once()

    async def test_reading_failure_adapts_once_and_does_not_block_the_next_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];value=day_plan(2);value['items'][0]['activity']['kind']='reading'
            provider=NS(text_chat=AsyncMock(side_effect=[response(value),response(choice('另写一段')),response(generated()),response(generated())]))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider);engine.reading=AsyncMock(side_effect=ReadingError('fetch','timeout'))
                await engine.plan_next_day();now[0]=stamp(7,9)
                with self.assertRaises(ReadingError):await engine.advance()
                self.assertTrue((await engine.advance())['advanced'])
                self.assertFalse((await engine.advance())['advanced'])
                now[0]=stamp(7,10);self.assertTrue((await engine.advance())['advanced'])
                self.assertEqual(life.count(),2);self.assertEqual(provider.text_chat.await_count,4)
                engine.reading.assert_awaited_once()
                self.assertEqual(life.outcomes()[0]['error'],'reading_fetch_timeout')

    async def test_interrupted_item_is_unknown_after_restart_and_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];provider=NS(text_chat=AsyncMock(side_effect=[response(day_plan(2)),asyncio.CancelledError(),response(generated())]))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider);await engine.plan_next_day();now[0]=stamp(7,9)
                with self.assertRaises(asyncio.CancelledError):await engine.advance()
                life,engine=fixture(Path(tmp),now,provider);engine.agenda_store().recover()
                self.assertEqual(engine.agenda_status()['today']['items'][0]['status'],'unknown')
                self.assertFalse((await engine.advance())['advanced']);self.assertEqual(provider.text_chat.await_count,2)
                now[0]=stamp(7,10);self.assertTrue((await engine.advance())['advanced'])
                self.assertEqual(provider.text_chat.await_count,3);self.assertEqual(life.count(),1)

    async def test_invalid_plan_is_recorded_once_without_repeated_model_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];provider=NS(text_chat=AsyncMock(return_value=NS(completion_text='invalid json')))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider)
                with self.assertRaises(ValueError):await engine.plan_next_day()
                self.assertFalse(await engine.plan_next_day())
                now[0]=stamp(7,9);self.assertFalse(await engine.plan_next_day())
                provider.text_chat.assert_awaited_once();self.assertEqual(life.count(),0)
                self.assertEqual(engine.agenda_status()['today']['status'],'failed')

    async def test_empty_day_and_rest_items_cost_no_execution_calls(self):
        for empty in (False,True):
            with self.subTest(empty=empty),tempfile.TemporaryDirectory() as tmp:
                now=[stamp()];value=day_plan(0 if empty else 1)
                if not empty:value['items'][0]['activity']['kind']='none'
                provider=NS(text_chat=AsyncMock(return_value=response(value)))
                with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                    life,engine=fixture(Path(tmp),now,provider);await engine.plan_next_day();now[0]=stamp(7,9)
                    self.assertFalse((await engine.advance())['advanced'])
                    self.assertFalse((await engine.advance())['advanced'])
                    self.assertEqual(life.count(),0);provider.text_chat.assert_awaited_once()

    async def test_model_can_schedule_two_drawings_without_a_fixed_seven_activity_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];value=day_plan(2)
            for item in value['items']:item['activity']['kind']='drawing'
            provider=NS(text_chat=AsyncMock(side_effect=[response(value),response(generated()),response(generated())]))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider)
                art=Path(tmp)/'fake-art.jpg';art.write_bytes(b'isolated-image-fixture'*100)
                drawing=NS(generate_for_qzone=AsyncMock(return_value=str(art)))
                lookup=engine.context.get_registered_star
                engine.context.get_registered_star=lambda name:NS(activated=True,star_cls=drawing) if name=='astrbot_plugin_dafeiyu_draw' else lookup(name)
                await engine.plan_next_day()
                for hour in (9,10):
                    now[0]=stamp(7,hour);self.assertTrue((await engine.advance())['advanced'])
                self.assertEqual(drawing.generate_for_qzone.await_count,2)
                self.assertEqual(life.count(),2);self.assertEqual(provider.text_chat.await_count,3)

    async def test_reserved_but_unstarted_item_recovers_without_blocking_later_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];provider=NS(text_chat=AsyncMock(side_effect=[response(day_plan(2)),response(generated())]))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider);await engine.plan_next_day()
                first=engine.agenda_status()['tomorrow']['items'][0]['id']
                SelfJournal(life.root).reserve_choice(first,{'kind':'pending'})
                life,engine=fixture(Path(tmp),now,provider);engine.agenda_store().recover()
                now[0]=stamp(7,10);self.assertTrue((await engine.advance())['advanced'])
                items=engine.agenda_status()['today']['items']
                self.assertEqual([r['status'] for r in items],['unknown','completed'])
                self.assertEqual(provider.text_chat.await_count,2)

    async def test_pausing_during_provider_lookup_prevents_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];provider=NS(text_chat=AsyncMock(return_value=response(day_plan(1))))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider);await engine.plan_next_day();now[0]=stamp(7,9)
                async def lookup(origin):life.set_enabled(False);return provider
                engine.context.get_using_provider_async.side_effect=lookup
                self.assertEqual((await engine.advance())['status'],'paused')
                self.assertEqual(life.count(),0);provider.text_chat.assert_awaited_once()

    async def test_window_is_rechecked_after_awaiting_the_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];value=day_plan(1);value['items'][0]['at']='20:59'
            provider=NS(text_chat=AsyncMock(return_value=response(value)))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider);await engine.plan_next_day();now[0]=stamp(7,20,59)
                async def lookup(origin):now[0]=stamp(7,21);return provider
                engine.context.get_using_provider_async.side_effect=lookup
                self.assertEqual((await engine.advance())['status'],'execution_window_closed')
                self.assertEqual(engine.agenda_status()['today']['items'][0]['status'],'expired')
                provider.text_chat.assert_awaited_once();self.assertEqual(life.count(),0)

    async def test_astrbot_config_enables_agenda_without_calling_a_provider(self):
        from data.plugins.dafeiyu_life import main as adapter
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp(hour=16)];provider=NS(text_chat=AsyncMock())
            _,engine=fixture(Path(tmp)/'fixture',now,provider)
            with patch.object(adapter,'get_astrbot_data_path',return_value=tmp):
                plugin=adapter.JingjingLife(engine.context,{'nightly_agenda':True,'planning_hour':23,'execution_start_hour':9,'execution_end_hour':21})
                state=plugin.engine.agenda_status()
                self.assertEqual(state['planning_time'],'23:00');self.assertEqual(state['execution_window'],'09:00–21:00')
                self.assertEqual(state['activity_count'],'model_selected');self.assertTrue(plugin.store.model_schedule)
                provider.text_chat.assert_not_awaited()


class AgendaReadOnly(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.fixture.NativeHooks.asyncSetUp
    asyncTearDown=base.fixture.NativeHooks.asyncTearDown
    run_hooks=base.fixture.NativeHooks.run_hooks
    ready=base.SingleGeneration.ready

    async def test_chat_reads_saved_agenda_without_planning_or_running_an_activity(self):
        now=[stamp()];provider=NS(text_chat=AsyncMock())
        life,engine=fixture(self.root/'agenda-life',now,provider)
        packet=materials(life,SelfJournal(life.root),'2026-11-07',9,21,'2.00',now=now[0])
        agenda=engine.agenda_store();agenda.claim_plan('2026-11-07');agenda.finish_plan('2026-11-07',parse_plan(json.dumps(day_plan(3)),packet))
        lookup=self.context.get_registered_star
        own=NS(store=life,engine=engine,public_context=life.public_context)
        self.context.get_registered_star=lambda name:NS(activated=True,star_cls=own) if name=='dafeiyu_life' else lookup(name)
        event,_=await self.ready('你明天安排了什么')
        result=json.loads(await self.plugin.self_details(event,'agenda'))
        self.assertEqual(len(result['data']['tomorrow']['items']),3)
        self.assertTrue(result['read_only']);self.assertFalse(result['executes_activities'])
        self.assertEqual(life.count(),0);provider.text_chat.assert_not_awaited();event.send.assert_not_awaited()


if __name__=='__main__':unittest.main()
