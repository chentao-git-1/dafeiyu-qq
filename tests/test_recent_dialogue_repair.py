"""Focused regressions from actual dialogue review; temporary storage and captured sends."""
import asyncio,importlib,json,time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
import test_cognition as base
f=base.fixture
from data.plugins.dafeiyu_dialogue.history_evidence import historical_tools
from data.plugins.dafeiyu_dialogue.generation import claim_sources
from data.plugins.dafeiyu_dialogue.review import UNAVAILABLE,verify_revision
from data.plugins.dafeiyu_runtime import diagnostics


def tool_history():
    return [dict(role='assistant',content='',tool_calls=[dict(id='native-one',type='function',function=dict(name='read_feed',arguments='{}'))]),
        dict(role='tool',tool_call_id='native-one',content=json.dumps(dict(post='post-one',date='2026-10-29',comment='留言甲',replied=False),ensure_ascii=False))]


class RecentDialogueRepair(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=f.NativeHooks.asyncSetUp
    asyncTearDown=f.NativeHooks.asyncTearDown
    run_hooks=f.NativeHooks.run_hooks
    ready=base.SingleGeneration.ready

    async def test_semantic_path_retains_directory_but_not_legacy_whole_reply_override(self):
        e,req=await self.ready('在QQ空间里的')
        routes=importlib.import_module('data.plugins.dafeiyu_message_routes.main').MessageRoutes(self.context)
        routes.directory=AsyncMock(return_value={});routes.private_directory=AsyncMock(return_value={f.OWNER:dict(name='爸爸',aliases=[])})
        routes.private_history=NS(prepare=AsyncMock(),correct=AsyncMock(return_value='旧私聊状态'))
        self.context.get_platform=lambda *a:NS(meta=lambda:NS(id='dafeiyu-qq'),bot=object())
        await routes.inject_routes(e,req);routes.private_history.prepare.assert_not_awaited()
        self.assertIn('当前真实好友',req.system_prompt)
        result=NS(is_llm_result=lambda:True,chain=[f.Plain('那条空间评论的回复状态还需要核实。')])
        e.get_result=lambda:result
        await routes.ground_private_status(e)
        args=dict(session=e.unified_msg_origin,messages=[dict(type='plain',text='保留语义回复')])
        await routes.ground_owner_tool_message(e,NS(name='send_message_to_user'),args)
        routes.private_history.correct.assert_not_awaited();self.assertEqual(args['messages'][0]['text'],'保留语义回复')
        e.set_extra('dafeiyu_generation_contract',False);await routes.ground_private_status(e)
        routes.private_history.correct.assert_awaited_once();self.assertEqual(result.chain[0].text,'旧私聊状态')

    async def test_only_complete_paired_native_tool_history_becomes_attributed_past_evidence(self):
        native=tool_history();rows=historical_tools(native)
        self.assertEqual(len(rows),1);self.assertFalse(rows[0]['current_turn']);self.assertIn('2026-10-29',rows[0]['result'])
        sources=claim_sources(dict(historical_tool_results=rows))
        self.assertEqual(sources[rows[0]['id']]['kind'],'memory');self.assertNotIn('tool:read_feed',sources)
        self.assertEqual(historical_tools(native[1:]),[])
        self.assertEqual(historical_tools(native+[native[0]]),[])
        native[-1]['content']='x'*5001;self.assertEqual(historical_tools(native),[])

    async def test_invalid_main_structure_recovers_once_and_passes_regular_gates(self):
        e,_=await self.ready('你好')
        model=AsyncMock(return_value=NS(completion_text=base.envelope('在呢。')))
        self.context.get_using_provider_async.return_value=NS(text_chat=model)
        response=NS(completion_text='{broken',tools_call_name=[])
        await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,'在呢。');self.assertTrue(e.get_extra('dafeiyu_generation_valid'))
        self.assertEqual(e.get_extra('dafeiyu_recovery_attempts'),1);model.assert_awaited_once();e.send.assert_not_awaited()
        self.assertIsNone(model.call_args.kwargs['func_tool'])

    async def test_repeated_invalid_structure_stops_after_one_attempt(self):
        e,_=await self.ready('你好');model=AsyncMock(return_value=NS(completion_text='{broken again'))
        self.context.get_using_provider_async.return_value=NS(text_chat=model)
        response=NS(completion_text='{broken',tools_call_name=[]);await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,UNAVAILABLE);model.assert_awaited_once()
        self.assertEqual(e.get_extra('dafeiyu_generation_failure_kind'),'format');self.assertFalse(e.get_extra('dafeiyu_generation_valid'))

    async def test_privacy_withdrawal_during_repair_cannot_release_the_repaired_draft(self):
        human='忘记全部资料';e,_=await self.ready(human)
        async def model(**kwargs):
            store=self.plugin.learning_store();store.apply_privacy(e.get_extra('dafeiyu_learning_source'),base.privacy('forget_all',human),[])
            return NS(completion_text=base.envelope('在呢。'))
        call=AsyncMock(side_effect=model);self.context.get_using_provider_async.return_value=NS(text_chat=call)
        response=NS(completion_text='{broken',tools_call_name=[]);await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,UNAVAILABLE);self.assertEqual(e.get_extra('dafeiyu_generation_failure_kind'),'source')
        self.assertFalse(e.get_extra('dafeiyu_generation_valid'));call.assert_awaited_once()

    async def test_memory_date_is_reviewed_even_with_social_focus_and_schema_retry_is_bounded(self):
        e,req=await self.ready('她那条评论是哪天的？');req.contexts=tool_history();await self.plugin.prepare(e,req)
        rows=e.get_extra('dafeiyu_dialogue_evidence')['historical_tool_results'];source=rows[0]['id']
        answers=iter(['not-json',json.dumps(dict(decision='revise',issues=[dict(segment_id=0,kind='unsupported_fact',span='28号')],
            revisions=[dict(segment_id=0,text='那条评论在29号的动态下。')])),json.dumps(dict(supported=True,violations=[]))])
        async def model(**kwargs):return NS(completion_text=next(answers))
        call=AsyncMock(side_effect=model);self.context.get_using_provider_async.return_value=NS(text_chat=call)
        obj=json.loads(base.envelope('那条评论在28号的动态下。',claims=[dict(kind='memory',span='28号',evidence_ids=[source])]))
        obj['focus']=['social'];obj['claims']=[];response=NS(completion_text=json.dumps(obj),tools_call_name=[])
        await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,'那条评论在29号的动态下。');self.assertTrue(e.get_extra('dafeiyu_generation_valid'))
        self.assertEqual(call.await_count,3);self.assertEqual(e.get_extra('dafeiyu_recovery_attempts'),1)

    async def test_valid_rejection_is_not_retried_into_an_approval(self):
        call=AsyncMock(return_value=json.dumps(dict(supported=False,violations=[dict(segment_id=0,span='已经发出')])) )
        self.assertFalse(await verify_revision(['已经发出'],{},call));call.assert_awaited_once()

    async def test_confirmed_image_delivery_is_available_to_fact_review_without_image_urls(self):
        from astrbot.core.message.components import Image
        from data.plugins.dafeiyu_runtime.reliability import ReplyReliability
        e,_=await self.ready('刚才那张图呢？');ReplyReliability.confirm_delivery(e,[Image(file='https://example.invalid/private.png')])
        answers=iter([dict(decision='revise',issues=[dict(segment_id=0,kind='unsupported_action',span='没有发出图片')],
            revisions=[dict(segment_id=0,text='本轮有图片已经发出了。')]),dict(supported=True,violations=[])])
        async def model(**kwargs):
            receipts=json.loads(kwargs['prompt'])['materials']['delivery_receipts']
            self.assertEqual(receipts[0]['media_types'],['image']);self.assertNotIn('private.png',json.dumps(receipts))
            return NS(completion_text=json.dumps(next(answers)))
        self.context.get_using_provider_async.return_value=NS(text_chat=AsyncMock(side_effect=model))
        obj=json.loads(base.envelope('没有发出图片'));obj['focus']=['action']
        response=NS(completion_text=json.dumps(obj),tools_call_name=[]);await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,'本轮有图片已经发出了。');e.send.assert_not_awaited()

    async def test_numeric_diagnostics_preserve_delivery_and_content_failure_separately(self):
        e=f.event('你好');e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text=UNAVAILABLE)])
        e.set_extra('dafeiyu_generation_failure_kind','format');turn=diagnostics.Turn(e);turn.calls['agent_runs']=1
        row=diagnostics.clean_row(turn.finish(e));self.assertEqual(row['outcome'],'reply');self.assertEqual(row['content_status'],'unavailable')
        self.assertEqual(row['generation_failure'],'format');self.assertNotIn(UNAVAILABLE,json.dumps(row,ensure_ascii=False))
        e.set_extra('dafeiyu_generation_failure_kind',None);e.set_extra('dafeiyu_generation_valid',True);e.set_extra('dafeiyu_recovery_attempts',1)
        self.assertEqual(diagnostics.Turn(e).finish(e)['content_status'],'recovered')

    async def test_slow_initiative_due_lookup_does_not_block_incoming_event_loop(self):
        cls=importlib.import_module('data.plugins.dafeiyu_companion.spontaneous').InitiativeEngine
        engine=cls.__new__(cls);engine.lock=asyncio.Lock()
        def slow_due():time.sleep(.15);return None
        engine.store=NS(diagnose=lambda *a:None,due=slow_due)
        task=asyncio.create_task(engine.tick_semantic());await asyncio.sleep(.03)
        self.assertFalse(task.done(),'The due lookup blocked the main event loop')
        await task


if __name__=='__main__':unittest.main()
