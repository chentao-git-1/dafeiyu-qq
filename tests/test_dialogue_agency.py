"""Conversation freedom and post-delivery learning with fake providers only."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

import test_cognition as base
from dafeiyu.dialogue.deferred import DeferredLearning

f = base.fixture


class NaturalDelivery(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = f.NativeHooks.asyncSetUp
    run_hooks = f.NativeHooks.run_hooks
    ready = base.SingleGeneration.ready

    async def asyncTearDown(self):
        await self.plugin.terminate()
        await f.NativeHooks.asyncTearDown(self)

    async def draft(self):
        proposal = base.item()
        event, _ = await self.ready(proposal['evidence'])
        response = NS(completion_text=base.envelope('那我先听你讲。', [proposal]), tools_call_name=[])
        await self.plugin.review_response(event, response)
        return event, response

    @staticmethod
    def confirm(event, response):
        event.set_extra('dafeiyu_confirmed_delivery', [dict(kind='reply', text=response.completion_text)])

    async def test_long_opinion_and_question_are_preserved_without_auxiliary_call(self):
        event, _ = await self.ready('这个结尾你怎么看？')
        body = ('我有点不同意见。留白能让人继续想，但如果前面埋下的线索全没回应，我也会觉得遗憾。'
                '\n\n我更喜欢解决一个问题，再留下一个新的问题，让结尾有落点，也还有一点余味。'
                '你更在意故事说清楚，还是结束以后还想继续琢磨的感觉？')
        self.assertGreater(len(body), 80)
        response = NS(completion_text=base.envelope(body), tools_call_name=[])
        await self.plugin.review_response(event, response)
        self.assertEqual(response.completion_text, body)
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()

    async def test_learning_does_not_delay_reply_or_start_without_confirmed_delivery(self):
        event, response = await self.draft()
        call = self.context.get_using_provider_async.return_value.text_chat
        call.assert_not_awaited()
        self.assertEqual(response.completion_text, '那我先听你讲。')
        await self.plugin.enqueue_learning(event)
        await self.plugin.deferred_learning.wait_idle()
        call.assert_not_awaited()
        self.assertEqual(self.plugin.learning_store().counts()['entities'], 0)
        self.confirm(event, response)
        await self.plugin.enqueue_learning(event)
        await self.plugin.enqueue_learning(event)
        await self.plugin.deferred_learning.wait_idle()
        call.assert_awaited_once()
        self.assertEqual(self.plugin.learning_store().counts()['entities'], 1)
        self.assertEqual(self.plugin.review_stats['learning_check_background_calls'], 1)
        event.send.assert_not_awaited()

    async def test_background_timeout_preserves_reply_and_does_not_retry(self):
        event, response = await self.draft()
        call = self.context.get_using_provider_async.return_value.text_chat
        call.side_effect = TimeoutError('synthetic timeout')
        self.confirm(event, response)
        await self.plugin.enqueue_learning(event)
        await self.plugin.deferred_learning.wait_idle()
        await self.plugin.enqueue_learning(event)
        await self.plugin.deferred_learning.wait_idle()
        self.assertEqual(response.completion_text, '那我先听你讲。')
        self.assertEqual(self.plugin.learning_store().counts()['entities'], 0)
        call.assert_awaited_once()
        event.send.assert_not_awaited()

    async def test_hypothetical_proposal_is_rejected_after_delivery_without_rewriting_reply(self):
        human = '假如将来开书店，我可能会喜欢清晨整理书架，现在还没有这个打算。'
        event, _ = await self.ready(human)
        proposal = base.item(human)
        response = NS(completion_text=base.envelope('这个画面让我想到刚开门时的阳光。', [proposal]), tools_call_name=[])
        await self.plugin.review_response(event, response)
        call = self.context.get_using_provider_async.return_value.text_chat
        call.side_effect = None
        call.return_value = NS(completion_text=json.dumps(dict(privacy_authorized=True,
            assessments=[dict(base.assessment(human), stance='hypothetical', retention='unspecified')])))
        self.confirm(event, response)
        await self.plugin.enqueue_learning(event)
        await self.plugin.deferred_learning.wait_idle()
        self.assertEqual(self.plugin.learning_store().counts()['entities'], 0)
        self.assertEqual(response.completion_text, '这个画面让我想到刚开门时的阳光。')
        call.assert_awaited_once()

    def revoke(self, event):
        store = self.plugin.learning_store()
        source = store.begin(replace(f.identity(), message_id='forget-new'), '忘掉我的资料')
        store.apply_privacy(source, base.privacy('forget_all', '忘掉我的资料'), [])

    async def test_revoked_source_is_skipped_before_any_background_model_call(self):
        event, response = await self.draft()
        self.revoke(event)
        self.confirm(event, response)
        await self.plugin.enqueue_learning(event)
        await self.plugin.deferred_learning.wait_idle()
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()
        self.assertEqual(self.plugin.learning_store().counts()['entities'], 0)

    async def test_forgetting_during_background_check_blocks_late_write(self):
        event, response = await self.draft()
        entered, release = asyncio.Event(), asyncio.Event()
        call = self.context.get_using_provider_async.return_value.text_chat
        original = call.side_effect
        async def slow(**kwargs):
            entered.set()
            await release.wait()
            return await original(**kwargs)
        call.side_effect = slow
        self.confirm(event, response)
        await self.plugin.enqueue_learning(event)
        await asyncio.wait_for(entered.wait(), 2)
        self.revoke(event)
        release.set()
        await self.plugin.deferred_learning.wait_idle()
        self.assertEqual(self.plugin.learning_store().counts()['entities'], 0)
        call.assert_awaited_once()

    async def test_privacy_request_is_processed_before_reply_and_never_deferred(self):
        human = '这一轮别保存我说的话'
        event, _ = await self.ready(human)
        response = NS(completion_text=base.envelope('好。', control=base.privacy('deny_turn', human)), tools_call_name=[])
        await self.plugin.review_response(event, response)
        self.context.get_using_provider_async.return_value.text_chat.assert_awaited_once()
        self.assertEqual(event.get_extra('dafeiyu_privacy_action'), 'deny_turn')
        self.assertFalse(self.plugin.learning_store().source_current(event.get_extra('dafeiyu_learning_source')))
        self.confirm(event, response)
        await self.plugin.enqueue_learning(event)
        self.assertEqual(self.plugin.deferred_learning.status()['pending'], 0)

    async def test_stopping_plugin_cancels_check_and_never_saves_candidate(self):
        event, response = await self.draft()
        entered = asyncio.Event()
        async def blocked(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        call = self.context.get_using_provider_async.return_value.text_chat
        call.side_effect = blocked
        self.confirm(event, response)
        await self.plugin.enqueue_learning(event)
        await asyncio.wait_for(entered.wait(), 2)
        await self.plugin.terminate()
        self.assertEqual(self.plugin.learning_store().counts()['entities'], 0)
        self.assertTrue(self.plugin.deferred_learning.closed)
        self.assertEqual(self.plugin.deferred_learning.status()['pending'], 0)


class QueueBudget(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_queue_deduplicates_consumed_jobs_even_after_failure(self):
        queue = DeferredLearning(capacity=2)
        self.addAsyncCleanup(queue.close)
        release = asyncio.Event()
        async def first():
            await release.wait()
            raise ValueError('synthetic failure')
        second = AsyncMock()
        self.assertTrue(queue.submit('one', first))
        self.assertTrue(queue.submit('two', second))
        self.assertFalse(queue.submit('one', first))
        self.assertFalse(queue.submit('three', second))
        release.set()
        await queue.wait_idle()
        second.assert_awaited_once()
        self.assertFalse(queue.submit('one', first))
        self.assertEqual(queue.status()['pending'], 0)

    async def test_close_discards_waiting_jobs_and_rejects_new_jobs(self):
        queue = DeferredLearning()
        entered = asyncio.Event()
        async def waiting():
            entered.set()
            await asyncio.Event().wait()
        later = AsyncMock()
        queue.submit('waiting', waiting)
        queue.submit('later', later)
        await asyncio.wait_for(entered.wait(), 2)
        await queue.close()
        later.assert_not_awaited()
        self.assertFalse(queue.submit('new', later))


if __name__ == '__main__':
    unittest.main()
