"""No paid reviewers or canned drawing progress; fake providers and sends only."""
import asyncio
import json
import sys
import time
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import test_cognition as base
sys.argv.insert(1, str(base.STAGE))
import test_persona_native as drawing_fixture
from data.plugins.dafeiyu_runtime.reliability import ReplyReliability
from dafeiyu.dialogue.review import UNAVAILABLE
from astrbot.core.message.message_event_result import ResultContentType
from dafeiyu.dialogue.action_facts import record_action

f = base.fixture
d = drawing_fixture.d


class DirectChat(unittest.IsolatedAsyncioTestCase):
    run_hooks = f.NativeHooks.run_hooks
    ready = base.SingleGeneration.ready

    async def asyncSetUp(self):
        await f.NativeHooks.asyncSetUp(self)
        self.plugin.config.pop('chat_model_verification', None)
        # Existing production configurations used this value. It must not enable a reviewer.
        self.plugin.config['response_review'] = 'enforce'

    async def asyncTearDown(self):
        await self.plugin.terminate()
        await f.NativeHooks.asyncTearDown(self)

    async def test_history_and_self_focus_do_not_call_a_reviewer_by_default(self):
        event, _ = await self.ready('你怎么看这件事？')
        event.set_extra('dafeiyu_dialogue_history', [dict(role='assistant', content='上次我也有不同的看法。')])
        body = '我有一点不同的看法，先听你把这个想法讲完。'
        raw = json.loads(base.envelope(body));raw['focus'] = ['social', 'self']
        response = NS(completion_text=json.dumps(raw), tools_call_name=[])
        await self.plugin.review_response(event, response)
        self.assertEqual(response.completion_text, body)
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()
        self.assertFalse(self.plugin.model_verification_enabled())

    async def test_primary_learning_is_saved_after_delivery_without_second_model(self):
        proposal = base.item()
        event, _ = await self.ready(proposal['evidence'])
        response = NS(completion_text=base.envelope('嗯，我知道你的意思。', [proposal]), tools_call_name=[])
        await self.plugin.review_response(event, response)
        store = self.plugin.learning_store()
        self.assertEqual(store.counts()['entities'], 0)
        event.set_extra('dafeiyu_confirmed_delivery', [dict(kind='reply', text=response.completion_text)])
        await self.plugin.enqueue_learning(event)
        await self.plugin.deferred_learning.wait_idle()
        self.assertEqual(store.counts()['entities'], 1)
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    async def test_explicit_forgetting_uses_local_source_and_ownership_without_a_model(self):
        human = '把我的资料忘掉'
        event, _ = await self.ready(human)
        response = NS(completion_text=base.envelope('好。', control=base.privacy('forget_all', human)), tools_call_name=[])
        await self.plugin.review_response(event, response)
        self.assertEqual(event.get_extra('dafeiyu_privacy_action'), 'forget_all')
        self.assertFalse(self.plugin.learning_store().source_current(event.get_extra('dafeiyu_learning_source')))
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    async def test_wrong_speaker_privacy_cannot_delete_anything(self):
        event, _ = await self.ready('她说把她的资料忘掉')
        control = dict(base.privacy('forget_all', event.message_str), subject='other', modality='reported')
        response = NS(completion_text=base.envelope('我听到了。', control=control), tools_call_name=[])
        await self.plugin.review_response(event, response)
        self.assertIsNone(event.get_extra('dafeiyu_privacy_action'))
        self.assertTrue(self.plugin.learning_store().source_current(event.get_extra('dafeiyu_learning_source')))
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    async def test_bad_format_does_not_start_a_paid_repair_or_claim_it_is_checking(self):
        event, _ = await self.ready('在吗')
        response = NS(completion_text='{broken', tools_call_name=[])
        await self.plugin.review_response(event, response)
        self.assertEqual(response.completion_text, UNAVAILABLE)
        self.assertNotIn('核对', response.completion_text)
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    async def test_periodic_event_recheck_stays_off_after_three_delivered_turns(self):
        event, _ = await self.ready()
        store = self.plugin.learning_store()
        proposal = dict(base.item('明天我准备去手作体验'), kind='event', summary='准备手作体验', condition='', status='planned')
        source = store.begin(replace(f.identity(), message_id='earlier-plan'), proposal['evidence'])
        store.commit(source, proposal['evidence'], [proposal], {}, confirmed=True)
        for number in range(3):
            event = f.event('先聊点别的');event.message_obj.message_id = 'plain-' + str(number)
            await self.run_hooks(event, f.ProviderRequest(prompt=event.message_str))
            response = NS(completion_text=base.envelope('好呀。'), tools_call_name=[])
            await self.plugin.review_response(event, response)
            event.set_extra('dafeiyu_confirmed_delivery', [dict(kind='reply', text=response.completion_text)])
            await self.plugin.enqueue_learning(event)
        await self.plugin.deferred_learning.wait_idle()
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    async def test_tool_message_keeps_primary_wording_without_reviewer(self):
        event, _ = await self.ready('帮我转告一句话')
        args = dict(session=event.unified_msg_origin, messages=[dict(type='plain', text='我今天有自己的安排。')])
        await self.plugin.review_tool_message(event, NS(name='send_message_to_user'), args)
        self.assertEqual(args['messages'][0]['text'], '我今天有自己的安排。')
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    async def test_local_action_fact_reaches_primary_prompt_and_claim_sources(self):
        self.plugin.config.update(generation_contract=True, learning_v2=True, retrieval_index_v2=True)
        self.context.get_using_provider_async=AsyncMock(return_value=NS(text_chat=AsyncMock()))
        event=f.event('以后别给我发表情')
        record_action(event,'chat_preferences','updated',profile=dict(memes='off'))
        request=f.ProviderRequest(prompt=event.message_str)
        await self.run_hooks(event,request)
        self.assertIn('BEGIN_DAFEIYU_ACTION_FACTS',request.system_prompt)
        self.assertIn('local_action:0',request.system_prompt)
        self.assertIn('local_action:0',event.get_extra('dafeiyu_claim_sources'))
        body='那就用文字聊。'
        response=NS(completion_text=base.envelope(body),tools_call_name=[])
        await self.plugin.review_response(event,response)
        self.assertEqual(response.completion_text,body)
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()


class DirectDrawing(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = drawing_fixture.Native.asyncSetUp
    drawing = drawing_fixture.Native.drawing

    async def test_generation_starts_with_no_chat_send_and_sends_only_actual_image(self):
        drawing = self.drawing();event = drawing_fixture.event('画一只蓝鲸')
        entered, release = asyncio.Event(), asyncio.Event()
        async def generate(*args):
            entered.set();await release.wait()
            return drawing_fixture.PNG, '.png', (1, 1)
        with patch.object(d, 'generate', new=AsyncMock(side_effect=generate)) as call:
            task = asyncio.create_task(drawing._draw(event, '海边的蓝鲸'))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                event.send.assert_not_awaited()
                release.set()
                self.assertEqual(await task, '图片已发送。')
            finally:
                release.set()
                if not task.done():task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            call.assert_awaited_once()
        event.send.assert_awaited_once()
        parts = event.send.await_args.args[0].chain
        self.assertTrue(any(isinstance(part, drawing_fixture.Image) for part in parts))
        self.assertFalse(any(isinstance(part, drawing_fixture.Plain) for part in parts))

    async def test_failed_generation_sends_no_preface_and_never_retries(self):
        drawing = self.drawing();event = drawing_fixture.event('帮我画张图')
        with patch.object(d, 'generate', new=AsyncMock(side_effect=d.DrawingError('synthetic failure', http_status=504))) as call:
            await drawing.draw_tool(event, '一只蓝鲸')
            await drawing.draw_tool(event, '一只蓝鲸')
        call.assert_awaited_once();event.send.assert_not_awaited()
        self.assertTrue(event.get_extra('dafeiyu_suppress_progress'))

    async def test_global_timer_does_not_add_waiting_text_during_drawing(self):
        event = drawing_fixture.event('画一张图')
        event.set_extra('dafeiyu_suppress_progress', True)
        started = asyncio.Event();started.set()
        original = AsyncMock()
        guard = ReplyReliability(original, progress_after=0)
        await guard._progress(event, dict(processing_started=started, send_lock=asyncio.Lock()))
        original.assert_not_awaited()
        self.assertEqual(guard.stats['progress_notices'], 0)

    async def test_busy_and_cooldown_do_not_pretend_this_request_started(self):
        drawing = self.drawing();event = drawing_fixture.event('画一张图')
        with patch.object(d, 'generate', new=AsyncMock()) as call:
            async with drawing.lock:
                self.assertIn('这次没有开始生成', await drawing._draw(event, '一只蓝鲸'))
            drawing.config['cooldown_seconds'] = 30;drawing.last[event.unified_msg_origin] = time.monotonic()
            self.assertIn('这次没有开始生成', await drawing._draw(event, '一只蓝鲸'))
            call.assert_not_awaited();event.send.assert_not_awaited()

    async def test_terminal_drawing_does_not_replace_natural_model_wording(self):
        drawing = self.drawing()
        for outcome, expected in ((None, '还没有图片，我们先聊聊你想到的场景。'),
                ('这次画图没完成，没有拿到图片。', '这次没画成，想法先留着。'),
                ('图片已发送。', '这次我给它配了暖黄色的光。')):
            with self.subTest(outcome=outcome):
                event = drawing_fixture.event('画一只蓝鲸')
                event.set_extra('dafeiyu_draw_outcome', outcome)
                result = event.chain_result([drawing_fixture.Plain(expected)]).set_result_content_type(ResultContentType.LLM_RESULT)
                event.set_result(result)
                await drawing.ground_drawing_result(event)
                self.assertEqual(result.chain[0].text, expected)
                event.send.assert_not_awaited()

    async def test_successful_image_allows_natural_caption_and_unrelated_quote_is_untouched(self):
        drawing = self.drawing()
        for human, outcome, text in (
                ('画一只蓝鲸', '图片已发送。', '这次我给它配了暖黄色的光。'),
                ('你刚才为什么说这句台词', None, '你提到的“画着呢”是之前那句台词。')):
            event = drawing_fixture.event(human)
            event.set_extra('dafeiyu_draw_outcome', outcome)
            result = event.chain_result([drawing_fixture.Plain(text)]).set_result_content_type(ResultContentType.LLM_RESULT)
            event.set_result(result)
            await drawing.ground_drawing_result(event)
            self.assertEqual(result.chain[0].text, text)


if __name__ == '__main__':
    unittest.main()
