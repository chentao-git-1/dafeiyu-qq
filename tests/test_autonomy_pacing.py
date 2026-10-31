"""Focused autonomy, private work continuity and response pacing boundaries."""
import asyncio
import copy
import datetime as dt
import importlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import test_cognition as base
f=base.fixture
from data.plugins.astrbot_plugin_dafeiyu_draw.works import WorkHistory
from data.plugins.dafeiyu_dialogue.provider_contract import ProviderContracts, arm_contract
from data.plugins.dafeiyu_dialogue.generation import request_contract, claim_sources
from data.plugins.dafeiyu_dialogue.context_builder import compose
from data.plugins.dafeiyu_life.life import LifeStore, LifeEngine, TZ
from data.plugins.dafeiyu_life.planning import SelfJournal, choose_activity


class BackgroundContract(unittest.IsolatedAsyncioTestCase):
    async def test_copied_chat_system_is_not_a_summary_json_capability(self):
        class Provider:
            def _finally_convert_payload(self,payload):pass
            async def text_chat(self,**kwargs):
                await asyncio.sleep(0)
                payload={'messages':[dict(role='system',content=kwargs.get('system_prompt','')),*kwargs.get('contexts',[])]}
                self._finally_convert_payload(payload)
                return NS(payload=payload,tools_call_name=[],completion_text='result')
        provider=Provider();contracts=ProviderContracts();contracts.install(provider)
        nonce=arm_contract(True);prompt=request_contract('operator identity and permissions',{},False,nonce)
        history=[dict(role='system',content=prompt),dict(role='user',content='summarize the history')]
        before=copy.deepcopy(history)
        try:
            chat,summary=await asyncio.gather(provider.text_chat(system_prompt=prompt),provider.text_chat(contexts=history))
            self.assertEqual(chat.payload['response_format'],{'type':'json_object'})
            self.assertNotIn('response_format',summary.payload)
            self.assertNotIn('BEGIN_DAFEIYU_GENERATION',summary.payload['messages'][1]['content'])
            self.assertIn('operator identity and permissions',summary.payload['messages'][1]['content'])
            self.assertEqual(history,before)
        finally:contracts.close();arm_contract(False)


class OwnWorks(unittest.TestCase):
    def test_only_verified_same_person_same_session_work_is_visible_and_forgettable(self):
        with tempfile.TemporaryDirectory() as tmp:
            images=Path(tmp)/'images';images.mkdir();path=images/'drawing-one.png';path.write_bytes(b'actual generated artifact')
            works=WorkHistory(images);key=works.record(f.OWNER,f.ORIGIN,path,'太阳照着嫩芽')
            works.record(f.OWNER,f.ORIGIN,path,'太阳照着嫩芽')
            rows=works.recent(f.OWNER,f.ORIGIN)
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['id'],key)
            self.assertEqual(works.recent(f.FRIEND,f.ORIGIN),[])
            self.assertEqual(works.recent(f.OWNER,'dafeiyu-qq:GroupMessage:7000000101'),[])
            self.assertNotIn('artifact',rows[0]);self.assertEqual(rows[0]['delivery'],'sent')
            path.write_bytes(b'changed artifact');self.assertEqual(works.recent(f.OWNER,f.ORIGIN),[])
            self.assertEqual(works.forget(f.OWNER,f.ORIGIN),1)
            self.assertIsNone(works.record(f.OWNER,f.ORIGIN,path,'old in-flight result',expected_epoch=0))
            epoch=works.begin(f.OWNER,f.ORIGIN)
            self.assertIsNotNone(works.record(f.OWNER,f.ORIGIN,path,'new authorized request',expected_epoch=epoch))

    def test_story_is_not_injected_as_actual_current_activity(self):
        record=dict(id='life:old',day='2026-10-28',kind='writing',note='一段写好的散文',thought='光影很有意思',
            story='小芽的叶子涂鸦还压在盆沿',project='文字小练习',stage=1,source={},completed_at=time.time()-86400)
        work=dict(id='drawing:one',kind='drawing',description='太阳照着嫩芽',day='2026-10-31',completed_at=time.time())
        context=compose('operator',f.identity(),f.CatalogSnapshot(()),'在干嘛',life=record,works=[work],contract=True)
        self.assertNotIn(record['story'],context.prompt)
        self.assertEqual(context.evidence['verified_self']['conversation_works'],[work])
        self.assertIn('self:work:drawing:one',claim_sources(context.evidence))
        fiction=compose('operator',f.identity(),f.CatalogSnapshot(()),'接着写小屋故事',life=record,
            character={'fictional_world':{'place':'海边小屋'}},contract=True)
        self.assertEqual(fiction.evidence['character']['previous_story']['text'],record['story'])
        self.assertNotIn('story',fiction.evidence['verified_self']['verified_completed'])


class DrawingContinuity(unittest.IsolatedAsyncioTestCase):
    async def test_work_is_recorded_only_after_image_delivery(self):
        module=importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_draw.main')
        with tempfile.TemporaryDirectory() as tmp:
            draw=module.DafeiyuDraw.__new__(module.DafeiyuDraw)
            draw.config={'enabled':True,'api_key':'synthetic','cooldown_seconds':0};draw.lock=asyncio.Lock();draw.last={}
            draw.image_dir=Path(tmp)/'images';draw.image_dir.mkdir();draw.works=WorkHistory(draw.image_dir)
            draw._allowed=lambda event:True;draw._prune=lambda:None;draw.intent_context=NS(remember=lambda *args:None)
            e=f.event('画太阳照着嫩芽')
            with patch.object(module,'generate',AsyncMock(return_value=(b'actual image','.png',(1024,1024)))):
                self.assertEqual(await draw._draw(e,'太阳照着嫩芽'),'图片已发送。')
            self.assertEqual(len(draw.conversation_works(f.OWNER,f.ORIGIN)),1)
            failed=f.event('再画一个');failed.send=AsyncMock(side_effect=[None,RuntimeError('send failed')])
            with patch.object(module,'generate',AsyncMock(return_value=(b'another image','.png',(1024,1024)))):
                await draw._draw(failed,'另一张作品')
            self.assertEqual(len(draw.conversation_works(f.OWNER,f.ORIGIN)),1)


class AutonomousActivity(unittest.IsolatedAsyncioTestCase):
    async def test_opportunity_uses_the_same_daily_slot_and_can_choose_no_action(self):
        now=dt.datetime(2026,10,31,13,tzinfo=TZ).timestamp()
        with tempfile.TemporaryDirectory() as tmp, patch('data.plugins.dafeiyu_life.life.time.time',return_value=now):
            store=LifeStore(Path(tmp));store.schedule(now)
            with store.db() as db:db.execute('UPDATE runs SET run_at=?',(now+3600,))
            none=dict(kind='none',project_id='',expected_version=0,title='',question='',source_ids=[],reason='暂时没有想推进的方向')
            model=AsyncMock(return_value=NS(completion_text=json.dumps(none)))
            controller=NS(owner_policy_enabled=lambda flag:True,natural=lambda:NS(actor_owner=lambda:f.OWNER),metered_provider=lambda p,*a:p)
            ctx=NS(get_registered_star=lambda name:NS(activated=True,star_cls=controller),get_using_provider_async=AsyncMock(return_value=NS(text_chat=model)))
            engine=LifeEngine(store,ctx)
            self.assertEqual((await engine.advance())['status'],'waiting');model.assert_not_awaited()
            self.assertEqual((await engine.advance(opportunity=True))['status'],'chose_no_activity')
            await engine.advance(opportunity=True);await engine.advance(bootstrap=True)
            model.assert_awaited_once();self.assertEqual(store.count(),0)

    async def test_failed_choice_is_not_left_as_pending_or_retried_on_every_tick(self):
        now=dt.datetime(2026,10,31,13,tzinfo=TZ).timestamp()
        with tempfile.TemporaryDirectory() as tmp, patch('data.plugins.dafeiyu_life.life.time.time',return_value=now):
            store=LifeStore(Path(tmp));model=AsyncMock(return_value=NS(completion_text='invalid JSON'))
            controller=NS(owner_policy_enabled=lambda flag:True,natural=lambda:NS(actor_owner=lambda:f.OWNER),metered_provider=lambda p,*a:p)
            ctx=NS(get_registered_star=lambda name:NS(activated=True,star_cls=controller),get_using_provider_async=AsyncMock(return_value=NS(text_chat=model)))
            engine=LifeEngine(store,ctx)
            with self.assertRaises(ValueError):await engine.advance(opportunity=True)
            self.assertEqual(store.schedule()['status'],'failed');self.assertEqual(model.await_count,2)
            await engine.advance(opportunity=True);self.assertEqual(model.await_count,2)

    async def test_project_decision_repairs_format_once_without_contact_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            choice=dict(kind='writing',project_id='',expected_version=0,title='影子的边缘',question='用什么词写柔软的影子',source_ids=['operator:seed'],reason='想写一个具体意象')
            model=AsyncMock(side_effect=[NS(completion_text='{broken'),NS(completion_text=json.dumps(choice))])
            result=await choose_activity(NS(text_chat=model),SelfJournal(Path(tmp)),[],['reading','writing'])
            self.assertEqual(result,choice);self.assertEqual(model.await_count,2)
            self.assertEqual(set(json.loads(model.call_args.kwargs['prompt'])),{'operator_seed','self_state','recent_works','allowed_kinds','max_activities_today'})

    async def test_paused_and_night_activity_are_not_bypassed_by_opportunity(self):
        now=dt.datetime(2026,10,31,23,tzinfo=TZ).timestamp()
        with tempfile.TemporaryDirectory() as tmp, patch('data.plugins.dafeiyu_life.life.time.time',return_value=now):
            store=LifeStore(Path(tmp));provider=AsyncMock()
            ctrl=NS(owner_policy_enabled=lambda flag:True)
            engine=LifeEngine(store,NS(get_registered_star=lambda name:NS(activated=True,star_cls=ctrl),get_using_provider_async=provider))
            self.assertEqual((await engine.advance(opportunity=True))['status'],'waiting')
            store.set_enabled(False);self.assertFalse((await engine.advance(opportunity=True))['advanced'])
            provider.assert_not_awaited()

    async def test_failed_reading_can_choose_one_original_work_without_repeating_the_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=LifeStore(Path(tmp))
            first=dict(kind='reading',project_id='',expected_version=0,title='光的细节',question='叶子为什么透亮',source_ids=['operator:seed'],reason='想查一件事')
            second=dict(kind='writing',project_id='',expected_version=0,title='光的比喻',question='用原创文字表达透亮的感觉',source_ids=['operator:seed'],reason='缺少可核验文章，转为创作，不冒充知识')
            work=dict(note='光落进一滴水里，像一扇很小的窗。窗里没有房间，只有慢慢变亮的天。',thought='可以用小窗来写透亮的感觉。',story='窗台的小芽照着一滴水，影子很淡。',evidence='',
                reflection=dict(insight='小窗是一个可以继续发展的比喻',evidence='像一扇很小的窗',next_question='换成月光会怎样',status='active'))
            model=AsyncMock(side_effect=[NS(completion_text=json.dumps(obj)) for obj in (first,second,work)])
            ctrl=NS(owner_policy_enabled=lambda flag:True,natural=lambda:NS(actor_owner=lambda:f.OWNER),metered_provider=lambda p,*a:p)
            engine=LifeEngine(store,NS(get_registered_star=lambda name:NS(activated=True,star_cls=ctrl),get_using_provider_async=AsyncMock(return_value=NS(text_chat=model))))
            engine.reading=AsyncMock(side_effect=ValueError('No unread primary source available'))
            with self.assertRaises(ValueError):await engine.advance(bootstrap=True)
            result=await engine.advance(bootstrap=True)
            self.assertTrue(result['advanced']);self.assertEqual(result['kind'],'writing');self.assertEqual(result['adapted_from'],'reading')
            self.assertEqual(store.count(),1);self.assertEqual(len(store.verified_records()),1)
            self.assertEqual(len(SelfJournal(Path(tmp)).view()['reflections']),1)
            await engine.advance(bootstrap=True)
            engine.reading.assert_awaited_once();self.assertEqual(model.await_count,3)


class ReplyPacing(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=f.NativeHooks.asyncSetUp
    asyncTearDown=f.NativeHooks.asyncTearDown
    run_hooks=f.NativeHooks.run_hooks
    ready=base.SingleGeneration.ready

    async def test_long_casual_answer_is_reviewed(self):
        e,_=await self.ready('嗨，聊什么呢')
        long='我比较喜欢淡蓝色，让我觉得画面很干净。'*9
        short='我想试试淡蓝配橘黄，应该挺亮。'
        self.plugin.reviewed=AsyncMock(return_value=NS(segments=(short,)))
        r=NS(completion_text=base.envelope(long),tools_call_name=[])
        await self.plugin.review_response(e,r);self.assertEqual(r.completion_text,short);self.plugin.reviewed.assert_awaited_once()

    async def test_short_answer_needs_no_extra_call(self):
        e,_=await self.ready('哈哈');self.plugin.reviewed=AsyncMock()
        r=NS(completion_text=base.envelope('嘿嘿。'),tools_call_name=[])
        await self.plugin.review_response(e,r);self.assertEqual(r.completion_text,'嘿嘿。');self.plugin.reviewed.assert_not_awaited()

    async def test_requested_detailed_answer_is_preserved_when_semantically_accepted(self):
        e,_=await self.ready('详细解释这段代码的每一步，不要省略')
        answer='先保留原始输入和身份，再检查已有来源，等待结果后重新核对状态。'*6
        self.plugin.reviewed=AsyncMock(return_value=NS(segments=(answer,)))
        r=NS(completion_text=base.envelope(answer),tools_call_name=[])
        await self.plugin.review_response(e,r);self.assertEqual(r.completion_text,answer)
        self.assertTrue(e.get_extra('dafeiyu_generation_valid'))


if __name__=='__main__':unittest.main()
