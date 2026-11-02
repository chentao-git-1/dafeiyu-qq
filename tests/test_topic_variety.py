"""On-demand self context, independent content and checked initiative; no real sends."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
import test_cognition as base
from test_personality_growth import seed_works
from test_execution_autonomy import generated,context
from data.plugins.dafeiyu_dialogue.generation import claim_sources
from data.plugins.dafeiyu_life.planning import SelfJournal
from data.plugins.dafeiyu_life.life import LifeStore,LifeEngine
from data.plugins.dafeiyu_companion.spontaneous_store import InitiativeStore
f=base.fixture


class OnDemandContext(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=f.NativeHooks.asyncSetUp
    asyncTearDown=f.NativeHooks.asyncTearDown
    run_hooks=f.NativeHooks.run_hooks
    ready=base.SingleGeneration.ready

    def install_life(self):
        store=seed_works(self.root/'own-life');journal=SelfJournal(store.root)
        journal.begin_project(dict(kind='writing',project_id='',title='手帐独有旧项目',question='旧册数不应每轮播报',source_ids=['operator:seed']))
        lookup=self.context.get_registered_star
        own=NS(store=store,public_context=store.public_context)
        self.context.get_registered_star=lambda name:NS(activated=True,star_cls=own) if name=='dafeiyu_life' else lookup(name)
        return store

    async def test_greeting_has_identity_without_works_interests_world_or_old_projects(self):
        self.install_life();e,req=await self.ready('你好，在吗')
        evidence=e.get_extra('dafeiyu_dialogue_evidence')
        self.assertTrue(evidence['self_details_on_demand']);self.assertIsNone(evidence['verified_self']['verified_completed'])
        self.assertNotIn('likes',evidence['character']['stable_profile'])
        self.assertIsNone(evidence['character']['fictional_world']);self.assertNotIn('self_thoughts',evidence)
        self.assertNotIn('手帐独有旧项目',req.system_prompt);self.assertNotIn('手帐与数字插画',req.system_prompt)
        self.assertIn('name',evidence['character']['stable_profile']);self.assertIn('likes',self.natural.dialogue_character()['stable_profile'])
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited();e.send.assert_not_awaited()

    async def test_explicit_read_keeps_original_interests_and_story_and_never_runs_an_activity(self):
        store=self.install_life();before=store.count();e,_=await self.ready('想听听你的兴趣和故事')
        likes=json.loads(await self.plugin.self_details(e,'interests'))
        self.assertIn('手帐与数字插画',likes['data']['stable_profile']['likes'])
        story=json.loads(await self.plugin.self_details(e,'fiction'))
        self.assertEqual(story['data']['fictional_world']['kind'],'fictional_world')
        self.assertFalse(story['executes_activities']);self.assertTrue(story['read_only'])
        self.assertEqual(store.count(),before);e.send.assert_not_awaited()
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    async def test_late_read_supplies_current_claims_and_tampered_work_is_not_cached(self):
        store=self.install_life();e,_=await self.ready('你最近做过什么')
        result=json.loads(await self.plugin.self_details(e,'works'))
        work=result['data']['verified_completed'];key='self:completed:'+work['id']
        self.assertIn(key,result['claim_evidence']);self.assertIn(key,e.get_extra('dafeiyu_claim_sources'))
        self.context.get_using_provider_async.return_value=NS(text_chat=AsyncMock(return_value=NS(completion_text='{"decision":"accept","issues":[],"revisions":[]}')))
        response=NS(completion_text=base.envelope('最近写了一段文字。',claims=[dict(kind='self_activity',span='写了一段文字',evidence_ids=[key])]),tools_call_name=[])
        await self.plugin.review_response(e,response);self.assertEqual(response.completion_text,'最近写了一段文字。')
        record=store.records(1)[0];(store.root/record['artifact']).write_bytes(b'changed')
        result=json.loads(await self.plugin.self_details(e,'works'))
        self.assertIsNone(result['data']['verified_completed']);self.assertNotIn(key,result['claim_evidence'])

    async def test_unselected_event_cannot_read_self_details(self):
        e=f.event('看看你的故事');result=json.loads(await self.plugin.self_details(e,'fiction'))
        self.assertTrue(result['unavailable']);self.assertNotIn('data',result);e.send.assert_not_awaited()

    async def test_social_label_does_not_skip_relevance_review_when_history_primes_old_topic(self):
        self.install_life();e,_=await self.ready('在吗')
        e.set_extra('dafeiyu_dialogue_history',[dict(role='assistant',content='旧作品的故事还没有结束')])
        body='在的，我正在想小屋那页手帐怎么画。'
        revised=dict(decision='revise',issues=[dict(segment_id=0,kind='unhelpful_style',span='我正在想小屋那页手帐怎么画')],
            revisions=[dict(segment_id=0,text='在呢。')])
        call=AsyncMock(side_effect=[NS(completion_text=json.dumps(revised)),NS(completion_text='{"supported":true,"violations":[]}')])
        self.context.get_using_provider_async.return_value=NS(text_chat=call)
        raw=json.loads(base.envelope(body));raw['focus']=['social']
        response=NS(completion_text=json.dumps(raw),tools_call_name=[])
        await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,'在呢。');self.assertEqual(call.await_count,2)
        e.send.assert_not_awaited()


class IndependentContent(unittest.IsolatedAsyncioTestCase):
    async def test_world_is_optional_and_new_writing_is_not_required_to_use_journal_format(self):
        for use_world in (False,True):
            with self.subTest(use_world=use_world),tempfile.TemporaryDirectory() as tmp:
                life=LifeStore(Path(tmp));provider=NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(generated(),ensure_ascii=False))))
                selected=dict(kind='writing',project_id='',expected_version=0,title='人物沉默的作用',question='写一小段人物对话，留一点没说出的意思',
                    source_ids=['operator:seed'],reason='从表达方式发展新方向',direction_version=0,use_world=use_world)
                with patch('data.plugins.dafeiyu_life.planning.choose_activity',AsyncMock(return_value=selected)):
                    self.assertTrue((await LifeEngine(life,context(provider)).advance(bootstrap=True))['advanced'])
                record=life.records(1)[0]
                self.assertEqual(bool(record['story']),use_world);self.assertEqual(record['content_nature'],'original_text')
                request=provider.text_chat.call_args.kwargs
                self.assertNotIn('只为自己做数字手帐',request['system_prompt'])
                self.assertEqual('窗台上叫小芽的小绿植' in request['prompt'],use_world)


class InitiativeReview(unittest.IsolatedAsyncioTestCase):
    initiative_fixture=base.BackgroundExecution.initiative_fixture

    async def test_incoming_during_independent_review_cancels_even_an_accepted_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            entered,release=asyncio.Event(),asyncio.Event()
            selected=dict(candidate_id='life:today',text='透明的边缘挺有意思。',source_quote='透明的边缘',reason='真实作品',related_message_ids=[],activity_day='2026-10-30')
            async def generate(**kwargs):
                if '当前在独立复核' in kwargs.get('system_prompt',''):
                    entered.set();await release.wait()
                    return NS(completion_text='{"facts_supported":true,"worth_sending":true,"new_information":"透明的边缘","comparisons":[],"reason":"新的作品感想"}')
                return NS(completion_text=json.dumps(selected))
            call=AsyncMock(side_effect=generate);send=AsyncMock()
            engine,store=await self.initiative_fixture(tmp,call,send)
            task=asyncio.create_task(engine.tick_semantic());await entered.wait()
            store.observe_incoming('new-human-message',engine.clock());release.set();await task
            send.assert_not_awaited();self.assertEqual(store.status()['attempts_today'],0)

    async def test_intention_cannot_be_sent_as_completed_research_and_no_fallback_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=dict(id='direction:9',kind='intention',day='2026-10-30',note='想查一份植物资料',version=9,status='intent')
            selected=dict(candidate_id=source['id'],text='我已经查了植物资料。',source_quote=source['note'],reason='分享新打算',related_message_ids=[],activity_day=source['day'])
            revised=dict(facts_supported=False,worth_sending=False,new_information='',comparisons=[],reason='只有打算，没有执行证据')
            call=AsyncMock(side_effect=[NS(completion_text=json.dumps(selected)),NS(completion_text=json.dumps(revised))])
            send=AsyncMock();engine,store=await self.initiative_fixture(tmp,call,send)
            async def sources():yield source
            engine.candidates=sources;await engine.tick_semantic();await engine.tick_semantic()
            send.assert_not_awaited();self.assertEqual(call.await_count,2);self.assertEqual(store.status()['attempts_today'],0)
            packet=json.loads(call.call_args.kwargs['prompt'])
            self.assertIsNone(packet['materials']['verified_self']['verified_completed'])
            self.assertEqual(packet['materials']['self_thoughts']['intentions'][0]['status'],'intent')
            with store.connect() as db:self.assertEqual(db.execute('SELECT status FROM evaluations').fetchone()[0],'filtered')

    async def test_new_source_version_with_same_topic_can_be_declined_without_retrying(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=dict(id='direction:2',kind='intention',day='2026-10-30',note='查植物向光的感受部位',version=2,status='intent')
            selected=dict(candidate_id=source['id'],text='我还想查植物向光的感受部位。',source_quote=source['note'],reason='新版打算',related_message_ids=[],activity_day=source['day'])
            declined=dict(facts_supported=True,worth_sending=True,new_information='植物向光的感受部位',
                comparisons=[dict(source='direction:1',relation='same_information')],reason='版本变化，实际还是同一个打算')
            call=AsyncMock(side_effect=[NS(completion_text=json.dumps(selected)),NS(completion_text=json.dumps(declined))])
            send=AsyncMock();engine,store=await self.initiative_fixture(tmp,call,send)
            with store.connect() as db:db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)',
                ('2026-10-29',0,'direction:1','intention','sent',engine.clock()-86400,'想查植物向光的感受部位',''))
            async def sources():yield source
            engine.candidates=sources;await engine.tick_semantic();await engine.tick_semantic()
            send.assert_not_awaited();self.assertEqual(call.await_count,2)
            previous=json.loads(call.call_args.kwargs['prompt'])['materials']['recent_own_shares']
            self.assertEqual(previous[0]['source'],'direction:1')

    async def test_prior_private_care_text_is_not_exposed_as_new_topic_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=InitiativeStore(Path(tmp)/'push.db')
            with store.connect() as db:
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)',('2026-10-30',0,'private:event','care','sent',1,'应当留在原会话的独有私事',''))
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)',('2026-10-30',1,'own:work','life','sent',2,'我写的一段原创文字',''))
            rows=store.recent_sent();self.assertEqual(rows[0]['body'],'我写的一段原创文字');self.assertIsNone(rows[1]['body'])


if __name__=='__main__':unittest.main()
