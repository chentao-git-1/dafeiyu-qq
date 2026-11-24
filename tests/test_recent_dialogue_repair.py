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

    def bind_fish(self, event):
        registry = self.context.get_registered_star
        binding = dict(umo=event.unified_msg_origin, sender_id=str(event.get_sender_id()), group_id='')
        state = dict(binding=binding, key_hash='fixture-fish')
        fish = NS(config={'enabled': True}, store=NS(data=state))
        self.context.get_registered_star = lambda name: (
            NS(star_cls=fish, activated=True) if name == 'astrbot_plugin_fishworld' else registry(name))
        return state

    async def observe_fish_tool(self, event, name, args, text, *, failed=False):
        tool = NS(name=name, mcp_server_name='fish-world')
        result = NS(content=[NS(type='text', text=text)], isError=failed)
        await self.plugin.collect_tool_evidence(event, tool, args, result)

    async def test_only_matching_fish_receipts_can_confirm_completed_actions(self):
        event, _ = await self.ready('放好了吗？')
        self.bind_fish(event)
        stored = '我把番茄炒蛋放进冰箱了。'
        noodles = '我点好面了。'
        await self.observe_fish_tool(event, 'world_now', {}, '现在 12:03 · 在家')
        self.assertNotEqual(await self.plugin.grounded_wording(event, noodles, final=True), noodles)
        await self.observe_fish_tool(event, 'world_now', {}, '可用操作：下单番茄鸡蛋面 order-noodles')
        self.assertNotEqual(await self.plugin.grounded_wording(event, noodles, final=True), noodles)
        await self.observe_fish_tool(event, 'world_do', {'id': 'throw-dish-22'}, '好的，你把「清炒青菜」扔掉了。')
        self.assertNotEqual(await self.plugin.grounded_wording(event, stored, final=True), stored)
        discarded = '我把清炒青菜扔掉了。'
        self.assertEqual(await self.plugin.grounded_wording(event, discarded, final=True), discarded)
        await self.observe_fish_tool(event, 'world_do', {'id': 'fridge-dish-21'},
                                     'error: MCP session is not available', failed=True)
        self.assertNotEqual(await self.plugin.grounded_wording(event, stored, final=True), stored)

        await self.observe_fish_tool(event, 'world_do', {'id': 'fridge-dish-21'},
                                     '你醒了，起来了。\n好的，你把番茄炒蛋放进了冰箱。冰箱放了 9 格，最多 25 格。')
        self.assertEqual(await self.plugin.grounded_wording(event, stored, final=True), stored)
        other = '我把清炒青菜放进冰箱了。'
        self.assertNotEqual(await self.plugin.grounded_wording(event, other, final=True), other)
        for original in ('我想点一碗番茄鸡蛋面。', '如果我点好了面，就告诉你。', '你已经点好了面，可以歇歇了。'):
            with self.subTest(original=original):
                self.assertEqual(await self.plugin.grounded_wording(event, original, final=True), original)
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    async def test_receipt_follows_the_bound_chat_and_dated_history_can_confirm_an_order(self):
        event, _ = await self.ready('点面了吗？')
        state = self.bind_fish(event)
        await self.observe_fish_tool(event, 'world_history', {}, '12:05  已下单：番茄鸡蛋面')
        later = f.event('刚才那碗面呢？')
        later.message_obj.message_id = 'next-turn'
        await self.run_hooks(later, f.ProviderRequest(prompt=later.message_str))
        ordered = '我点好番茄鸡蛋面了。'
        self.assertEqual(await self.plugin.grounded_wording(later, ordered, final=True), ordered)
        state['key_hash'] = 'another-fish'
        self.assertNotEqual(await self.plugin.grounded_wording(later, ordered, final=True), ordered)

    async def test_reading_inventory_cannot_confirm_a_new_storage_action(self):
        event, _ = await self.ready('这次把番茄炒蛋放进去了吗？')
        self.bind_fish(event)
        await self.observe_fish_tool(event, 'world_now', {},
            '冰箱（放了 3 格，最多 25 格）：番茄炒蛋 dish-21。world_do 可以将食物放进冰箱。')
        observed = '番茄炒蛋在冰箱里。'
        self.assertEqual(await self.plugin.grounded_wording(event, observed, final=True), observed)
        for claimed in ('我刚把番茄炒蛋放进冰箱了。', '我刚把番茄炒蛋又放进冰箱了。'):
            with self.subTest(claimed=claimed):
                self.assertNotEqual(await self.plugin.grounded_wording(event, claimed, final=True), claimed)
        event.send.assert_not_awaited()

    async def test_unrelated_world_read_cannot_support_a_declared_external_action(self):
        self.plugin.config['chat_model_verification'] = False
        claimed = '我修好自行车了。'
        event, _ = await self.ready('自行车修好了吗？')
        self.bind_fish(event)
        async def finish(turn, tool):
            claim = dict(kind='external_action', span=claimed, evidence_ids=['tool:' + tool])
            envelope = json.loads(base.envelope(claimed, claims=[claim]))
            envelope['focus'] = ['action']
            response = NS(completion_text=json.dumps(envelope, ensure_ascii=False), tools_call_name=[])
            await self.plugin.review_response(turn, response)
            return response.completion_text
        await self.observe_fish_tool(event, 'world_now', {}, '位置在家')
        self.assertNotIn('修好自行车了', await finish(event, 'world_now'))
        next_turn = f.event('现在呢？')
        next_turn.message_obj.message_id = 'bicycle-completed'
        await self.run_hooks(next_turn, f.ProviderRequest(prompt=next_turn.message_str))
        await self.observe_fish_tool(next_turn, 'world_do', {'id': 'repair-bicycle'}, '好的，你修好了自行车。')
        self.assertEqual(await finish(next_turn, 'world_do'), claimed)
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    async def test_location_is_readable_and_textual_item_failure_is_not_a_success(self):
        from data.plugins.dafeiyu_dialogue.world_receipts import unsupported_world_spans
        sources = {'tool:world_now': {'data': {'tool':'world_now','result':'现在 00:03 · 在家'}}}
        claim = dict(kind='external_action',span='我已经在家了。',evidence_ids=['tool:world_now'])
        self.assertEqual(unsupported_world_spans(NS(claims=[claim]),sources), [])
        event, _ = await self.ready('收好了吗？')
        self.bind_fish(event)
        await self.observe_fish_tool(event,'world_do',{'id':'fridge-dish-21'},'把番茄炒蛋放进冰箱失败，容量不足。')
        reply = '我把番茄炒蛋放进冰箱了。'
        self.assertNotEqual(await self.plugin.grounded_wording(event,reply,final=True), reply)

    async def test_rereading_old_order_history_does_not_revive_a_cancelled_order(self):
        event, _ = await self.ready('我记得你点了面，还有在等的订单吗？')
        self.bind_fish(event)
        old = '00:00  已下单：番茄鸡蛋面'
        await self.observe_fish_tool(event, 'world_history', {}, old)
        waiting = '那碗番茄鸡蛋面还没送到。'
        self.assertEqual(await self.plugin.grounded_wording(event, waiting, final=True), waiting)
        await self.observe_fish_tool(event, 'world_history', {}, '00:01  订单已取消：番茄鸡蛋面')
        await self.observe_fish_tool(event, 'world_history', {}, old)
        self.assertNotEqual(await self.plugin.grounded_wording(event, waiting, final=True), waiting)

    async def test_continuing_private_chat_uses_recent_confirmed_bedtime_closing(self):
        self.plugin.config['chat_model_verification'] = False
        first, _ = await self.ready('能给我看看那本书吗？')
        followup = '这本书你最喜欢哪一章？'
        key = 'recent_bedtime_closing'

        async def hint(text):
            event = f.event(text)
            event.message_obj.message_id = str(time.time_ns())
            await self.run_hooks(event, f.ProviderRequest(prompt=text))
            self.assertTrue(event.get_extra('dafeiyu_generation_contract'))
            return event.get_extra('dafeiyu_dialogue_evidence')['current_confirmed_facts'].get(key)

        closing = '那本蓝皮的旅行手记。你也早点睡吧。'
        response = NS(completion_text=base.envelope(closing), tools_call_name=[])
        await self.plugin.review_response(first, response)
        self.assertTrue(first.get_extra('dafeiyu_generation_valid'))
        await self.natural.remember_delivered_reply(first)
        self.assertFalse(await hint(followup))
        first.set_extra('dafeiyu_confirmed_delivery', [dict(kind='reply', text=closing, at=time.time())])
        await self.natural.remember_delivered_reply(first)
        self.assertTrue(await hint(followup))
        self.assertFalse(await hint('晚安，明天见。'))
        self.assertFalse(await hint('你还没睡着吗？'))
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    def test_bedtime_signal_has_private_scope_and_recent_time_window(self):
        from data.plugins.dafeiyu_dialogue.recent_closing import recent_bedtime_closing
        now = time.time()
        turn = f.TurnContext(f.OWNER, f.ORIGIN, 'dafeiyu-qq', 'fixture', now)
        def exchange(origin=f.ORIGIN, owner=f.OWNER, age=60, reply='我这就回去睡了。'):
            return f.MemoryItem('exchange:1', owner, origin, 'session', 'exchange',
                                json.dumps({'human': '说说你的画吧', 'reply': reply}, ensure_ascii=False),
                                now-age, 'confirmed_exchange')
        text = '你准备给那只海鸟涂什么颜色？'
        self.assertTrue(recent_bedtime_closing(turn, text, [exchange()]))
        self.assertFalse(recent_bedtime_closing(turn, text, [exchange(age=3600)]))
        self.assertFalse(recent_bedtime_closing(turn, text, [
            exchange(origin='dafeiyu-qq:FriendMessage:' + f.FRIEND, owner=f.FRIEND)]))
        group = f.TurnContext(f.OWNER, 'dafeiyu-qq:GroupMessage:812', turn.platform, 'group', now)
        self.assertFalse(recent_bedtime_closing(group, text, [exchange(origin=group.origin)]))

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
        routes.private_history.correct.assert_not_awaited();self.assertEqual(result.chain[0].text,'那条空间评论的回复状态还需要核实。')

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
