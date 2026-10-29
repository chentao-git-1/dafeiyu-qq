"""Cross-turn regressions with real request hooks and a fake QQ API; no live sends."""
import asyncio
import copy
import datetime as dt
import gc
import functools
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

STAGE = Path(sys.argv.pop(1));sys.path.insert(0, str(STAGE))
import data.plugins
data.plugins.__path__ = [str(STAGE), *list(data.plugins.__path__)]
o = importlib.import_module('data.plugins.dafeiyu_runtime.outbound')
p = importlib.import_module('data.plugins.dafeiyu_runtime.private_outbound')
r = importlib.import_module('data.plugins.dafeiyu_runtime.main')
h = importlib.import_module('data.plugins.dafeiyu_message_routes.private_history')
g = importlib.import_module('data.plugins.dafeiyu_natural_chat.grounding')
s = importlib.import_module('data.plugins.dafeiyu_natural_chat.spoken_style')
d = importlib.import_module('data.plugins.dafeiyu_runtime.diagnostics')
voice = importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_voice_request.main')
n = importlib.import_module('data.plugins.dafeiyu_natural_chat.main')
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import ResultContentType

OWNER, FRIEND, OTHER, BOT = '7000000001', '7000000011', '7000000012', '7000000002'
NOW = int(time.time())


def event(text='好的鲸鲸', uid=FRIEND, group='', platform='dafeiyu-qq'):
    msg = AstrBotMessage();msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    msg.self_id = BOT;msg.session_id = group or uid;msg.group_id = group;msg.message_id = 'continuity-test'
    msg.sender = MessageMember(uid, 'synthetic');msg.message = [Plain(text)];msg.message_str = text
    msg.raw_message = {'post_type': 'message', 'self_id': BOT, 'user_id': uid, 'time': time.time()}
    e = AiocqhttpMessageEvent(text, msg, PlatformMetadata('aiocqhttp', 'test', platform), group or uid, object())
    e.send = AsyncMock();return e


def user(text, stamp):
    return {'role': 'user', 'content': text + '<system_reminder>Current datetime: ' + dt.datetime.fromtimestamp(stamp, o.TZ).strftime('%Y-%m-%d %H:%M') + ' (CST)</system_reminder>'}


def report(received=NOW-86400, sent=NOW-60):
    return {'read_status': 'ok', 'contact': '虚构叔叔',
            'last_received': {'timestamp': received, 'at': '旧日期', 'text': '旧的纸船话题。'} if received else None,
            'last_sent': {'timestamp': sent, 'text': '新的提醒。'} if sent else None}


class FakeQQ:
    def __init__(self):self.call_action = AsyncMock(return_value={'message_id': 123})
    def __getattr__(self, name):return functools.partial(self.call_action, name)


class ReplyOrder(unittest.IsolatedAsyncioTestCase):
    def test_old_reply_is_not_a_reply_after_new_send(self):
        value = h.latest_reply_text(report(), '叔叔刚才那条回了吗？')
        self.assertIn('发出后', value);self.assertIn('还没查到', value);self.assertNotIn('纸船', value)

    def test_equal_second_remains_uncertain_and_later_incoming_is_an_arrival(self):
        self.assertIn('不能确认', h.latest_reply_text(report(NOW, NOW), '叔叔回了吗'))
        self.assertIn('发出以后', h.latest_reply_text(report(NOW, NOW-60), '叔叔回了吗'))

    def test_explicit_past_question_and_missing_sent_anchor_are_not_recast(self):
        self.assertIsNone(h.latest_reply_text(report(), '以前叔叔回过什么'))
        self.assertIsNone(h.latest_reply_text(report(sent=None), '叔叔说什么'))

    async def test_negative_and_positive_generated_claims_both_use_new_send_boundary(self):
        contacts = {FRIEND: {'name': '虚构叔叔', 'aliases': ['叔叔']}}
        for candidate in ('叔叔还没回。', '叔叔回了，说的是旧纸船。'):
            e = event('叔叔刚才回了吗', uid=OWNER)
            e.set_extra('dafeiyu_private_history', {FRIEND: report()})
            bot = NS(call_action=AsyncMock())
            actual = await h.PrivateHistory().correct(e, candidate, contacts, bot)
            self.assertIn('还没查到', actual);self.assertNotIn('旧纸船', actual);bot.call_action.assert_not_awaited()


class Wording(unittest.TestCase):
    def test_implicit_problem_preserves_helpful_clarification(self):
        for human in ('下班了，钥匙找不到了', '睡醒了，电脑打不开了', '考完了，题目还是看不懂'):
            candidate = '最后一次正常用是在什么时候？先核对一下。'
            self.assertEqual(s.conversational_mode(human), 'request')
            self.assertEqual(s.close_idle_questions(candidate, human), candidate)

    def test_casual_meal_is_sharing_and_rhetorical_banter_is_retained(self):
        self.assertEqual(s.conversational_mode('食堂买饭吃'), 'sharing')
        candidate = '这谁顶得住啊？我都替你无语。'
        self.assertEqual(s.close_idle_questions(candidate, '又输了三把'), candidate)
        self.assertIn('不顺手指导', s.style_context('食堂买饭吃'))

    def test_unscheduled_artwork_and_later_delivery_are_not_promised(self):
        for text in ('雪压着的小叶子挺好。画完先给你看。', '嗯，那你先忙，我慢慢画。', '稍后我会通知你。', '好。到时候提醒你。画完先给你看。'):
            actual = g.ground_response(text, '好的')
            self.assertNotIn('画完先给你看', actual);self.assertNotIn('我慢慢画', actual);self.assertNotIn('稍后我会通知', actual)

    def test_ideas_quotes_fiction_user_work_and_verified_notification_survive(self):
        for text, human in [('我想画一片叶子。', '你想画什么'), ('她说“画完先给你看”。', '接着聊'),
                            ('你画完给我看看。', '我准备画画'), ('在我的小屋故事里，我慢慢画。', '你的小屋呢')]:
            self.assertEqual(g.ground_response(text, human), text)
        text = '稍后我会通知你。'
        self.assertEqual(g.ground_response(text, '这个提醒设好了吗', reminder_ok=True), text)
        self.assertEqual(g.ground_response('画完先给你看。', '逐字复述这句话'), '画完先给你看。')

    def test_structured_custom_task_receipt_requires_created_and_valid_id(self):
        self.assertEqual(g.scheduled_job_id('dafeiyu_tasks', {'action': 'create'}, json.dumps({'created': True, 'id': 'job-test-123'})), 'job-test-123')
        for body in ('{}', '{"created":false,"id":"job-test-123"}', '失败'):
            self.assertIsNone(g.scheduled_job_id('dafeiyu_tasks', {'action': 'create'}, body))


class Outbound(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.addCleanup(gc.collect)
        self.root = Path(self.tmp.name)
        self.bot = FakeQQ()
        self.platform = NS(bot=self.bot, meta=lambda: NS(id='dafeiyu-qq'))
        self.config = {'platform_settings': {'enable_id_white_list': True, 'id_whitelist': [o.PREFIX+OWNER, o.PREFIX+FRIEND]},
                       'provider_tts_settings': {'enable': True, 'trigger_probability': 1, 'dual_output': False}}
        self.context = NS(get_config=lambda *a: self.config, get_platform=lambda *a: self.platform,
                          register_web_api=lambda *a: None,
                          conversation_manager=NS(get_curr_conversation_id=AsyncMock(return_value='native-one')))
        self.obj = p.PrivateOutbound(self.context, self.root);self.addCleanup(self.obj.close)

    async def send(self, text='测试用主动消息。', uid=FRIEND):
        await self.obj.observe_bot()
        return await self.bot.call_action('send_private_msg', user_id=int(uid), message=[{'type': 'text', 'data': {'text': text}}])

    def req(self, contexts=None, cid='native-one'):
        return ProviderRequest(contexts=contexts or [], conversation=NS(cid=cid), system_prompt='base')

    async def test_confirmed_send_is_durable_idempotent_and_target_scoped(self):
        receipt = await self.send();await self.send()
        self.assertEqual(receipt, {'message_id': 123})
        reopened = o.OutboundStore(self.obj.store.path)
        self.assertEqual(len(reopened.recent(FRIEND, 'native-one')), 1)
        self.assertFalse(reopened.recent(OWNER, 'native-one'));self.assertFalse(reopened.recent(FRIEND, 'new-chat'))

    async def test_dynamic_private_sender_uses_the_same_confirmed_ingestion(self):
        await self.obj.observe_bot()
        receipt = await self.bot.send_private_msg(user_id=int(FRIEND), message='测试用生日提醒。')
        self.assertEqual(receipt, {'message_id': 123})
        self.assertEqual(self.obj.store.recent(FRIEND, 'native-one')[0]['text'], '测试用生日提醒。')

    async def test_unknown_failed_invalid_and_exceptional_sends_are_not_recorded(self):
        await self.obj.observe_bot();original = self.obj.wrappers[0][1]
        for receipt in (None, True, {'message_id': 0}, {'message_id': False}, {'status': 'failed', 'message_id': 123}, {'retcode': 1, 'message_id': 123}):
            original.return_value = receipt;self.assertEqual(await self.send(), receipt)
        original.side_effect = TimeoutError('unconfirmed')
        with self.assertRaises(TimeoutError):await self.send()
        self.assertFalse(self.obj.store.recent(FRIEND, 'native-one'))

    async def test_metadata_failure_never_replays_or_changes_confirmed_send(self):
        await self.obj.observe_bot();original = self.obj.wrappers[0][1]
        with patch.object(self.obj.store, 'record', side_effect=OSError('synthetic')):
            self.assertEqual(await self.send(), {'message_id': 123})
        self.assertEqual(original.await_count, 1)

    async def test_group_wrong_account_and_unopened_peer_are_not_private_context(self):
        await self.obj.observe_bot()
        for action, params in [('send_group_msg', {'group_id': 7000000101}), ('send_private_msg', {'user_id': int(OTHER)}),
                               ('send_private_msg', {'user_id': int(FRIEND), 'self_id': 999999})]:
            await self.bot.call_action(action, message='test', **params)
        self.assertEqual(self.obj.store.status()['retained_records'], 0)

    async def test_normal_agent_reply_is_not_duplicated_but_cross_chat_send_is_recorded(self):
        e = event(uid=OWNER);turn = d.Turn(e);turn.calls['agent_runs'] = 1
        token = d.CURRENT.set(turn)
        try:
            await self.send(uid=OWNER);await self.send(uid=FRIEND)
        finally:d.CURRENT.reset(token)
        self.assertFalse(self.obj.store.recent(OWNER, 'native-one'))
        self.assertEqual(len(self.obj.store.recent(FRIEND, 'native-one')), 1)

    async def test_next_day_ack_gets_actual_assistant_turn_without_new_send(self):
        stamp = NOW - 2*86400
        self.obj.store.record(FRIEND, '请帮我转告爸爸饭钱的事。', {'message_id': 123}, 'native-one', at=stamp)
        e = event();e.set_extra('jingjing_actual_chat', [{'speaker': '鲸鲸', 'id': '123', 'timestamp': stamp, 'text': '请帮我转告爸爸饭钱的事。'}])
        req = self.req([user('以前的纸船话题', stamp-86400), {'role': 'assistant', 'content': '旧回复。'}])
        await self.obj.prepare(e, req);await self.obj.prepare(e, req)
        self.assertEqual(len(req.contexts), 3);self.assertEqual(req.contexts[-1]['role'], 'assistant')
        self.assertIn('转告爸爸', req.contexts[-1]['content']);self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertIn('本人这句是在应下', req.extra_user_content_parts[0].text)
        self.assertIn('不代表已充值', req.extra_user_content_parts[0].text);e.send.assert_not_awaited()

    async def test_historical_insertion_preserves_existing_turns_and_ambiguous_order(self):
        stamp = NOW-2*86400
        self.obj.store.record(FRIEND, '生日提醒。', {'message_id': 123}, 'native-one', at=stamp)
        old = [user('旧话题', stamp-86400), {'role': 'assistant', 'content': '旧回复'}, user('收到', stamp+180), {'role': 'assistant', 'content': '已接住'}]
        req = self.req(copy.deepcopy(old));e = event('继续新话题')
        await self.obj.prepare(e, req)
        self.assertEqual(req.contexts[:2]+req.contexts[3:], old)
        self.assertIn('生日提醒', req.contexts[2]['content'])
        row = self.obj.store.recent(FRIEND, 'native-one')[0]
        contexts = [{'role': 'user', 'content': '没有可信日期'}]
        self.assertEqual(o.bridge(contexts, row), (contexts, False))

    async def test_new_conversation_wrong_scope_and_cron_cannot_receive_old_context(self):
        await self.send()
        cases = [(event(uid=OTHER), self.req()), (event(group='7000000101'), self.req()), (event(platform='other'), self.req()), (event(), self.req(cid='new-chat'))]
        cron = event();cron.set_extra('cron_job', {'id': 'test'});cases.append((cron, self.req()))
        for e, req in cases:
            await self.obj.prepare(e, req);self.assertFalse(req.contexts);self.assertFalse(req.extra_user_content_parts)
            if getattr(req.conversation, 'cid', '') == 'new-chat':self.assertTrue(e.get_extra('jingjing_outbound_managed'))

    async def test_forgetting_removes_body_and_prevents_legacy_reimport(self):
        await self.send('测试用生日提醒。')
        await self.obj.prepare(event('忘记所有聊天记录'), self.req())
        self.assertFalse(self.obj.store.recent(FRIEND, 'native-one'))
        self.assertFalse(self.obj.store.record(FRIEND, '测试用生日提醒。', {'message_id': 123}, 'native-one'))
        e = event();await self.obj.prepare(e, self.req());e.send.assert_not_awaited()

    async def test_multiturn_send_ack_topic_switch_and_second_speaker(self):
        await self.send('爸爸的提醒，请明天带上资料。')
        first = event();first.set_extra('jingjing_actual_chat', [{'speaker': '鲸鲸', 'id': '123', 'timestamp': NOW, 'text': '爸爸的提醒，请明天带上资料。'}])
        req = self.req();await self.obj.prepare(first, req)
        context = req.contexts+[user('好的鲸鲸', NOW), {'role': 'assistant', 'content': '好，记得带上就行。'}]
        second = event('下班了，钥匙找不到了');second.set_extra('jingjing_actual_chat', [{'speaker': '鲸鲸', 'id': '124', 'timestamp': NOW, 'text': '好，记得带上就行。'}])
        second_req = self.req(context);await self.obj.prepare(second, second_req)
        candidate = '你最后一次用是在公司还是家门口？先看看包的夹层。'
        self.assertEqual(s.close_idle_questions(candidate, second.message_str), candidate)
        self.assertNotIn('本人这句是在应下', second_req.extra_user_content_parts[0].text)
        other_req = self.req();await self.obj.prepare(event(uid=OWNER), other_req)
        self.assertFalse(other_req.contexts)

    async def test_automatic_voice_budget_is_short_and_explicit_voice_keeps_provider_budget(self):
        provider = NS(get_audio=AsyncMock(return_value='/tmp/fake.wav'), synthesis_timeout=85)
        self.context.get_using_tts_provider_async = AsyncMock(return_value=provider)
        obj = voice.DafeiyuVoiceRequest(self.context)
        original = voice.asyncio.wait_for;budgets = []
        async def wait(awaitable, timeout):
            budgets.append(timeout);return await original(awaitable, timeout)
        with patch.object(voice, 'social_profile', return_value={'voice': 'auto'}), patch.object(voice.SessionServiceManager, 'should_process_tts_request', new=AsyncMock(return_value=True)), patch.object(voice.asyncio, 'wait_for', side_effect=wait):
            for text in ('今天的新发现', '请用语音说今天的新发现'):
                e = event(text);e.set_result(e.chain_result([Plain('测试用短回复。')]).set_result_content_type(ResultContentType.LLM_RESULT))
                await obj.honor_delivery(e)
                self.assertEqual(sum(isinstance(part, Record) for part in e.get_result().chain), 1)
        self.assertEqual(budgets, [8, 90])

    async def test_native_custom_task_verification_preserves_only_owned_notification(self):
        with patch.object(n, 'get_astrbot_data_path', return_value=str(self.root)):
            natural = n.NaturalChat(self.context)
        e = event('稍后告诉我结果');e.set_extra('jingjing_style_prepared', True)
        payload = {'session': e.unified_msg_origin, 'sender_id': 'scheduler', 'dafeiyu_creator': FRIEND, 'dafeiyu_target': FRIEND}
        job = NS(enabled=True, payload=payload, next_run_time=dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1))
        self.context.cron_manager = NS(db=NS(get_cron_job=AsyncMock(return_value=job)))
        result = json.dumps({'created': True, 'id': 'job-test-123'})
        await natural.verify_execution(e, NS(name='dafeiyu_tasks'), {'action': 'create'}, result)
        reply = NS(completion_text='稍后我会通知你。', tools_call_name=[])
        await natural.ground_current_facts(e, reply)
        self.assertEqual(reply.completion_text, '稍后我会通知你。')
        payload['dafeiyu_target'] = OTHER;e.set_extra('jingjing_reminder_verified', False)
        await natural.verify_execution(e, NS(name='dafeiyu_tasks'), {'action': 'create'}, result)
        reply.completion_text = '稍后我会通知你。';await natural.ground_current_facts(e, reply)
        self.assertNotIn('稍后我会通知你', reply.completion_text)

    async def test_runtime_lock_wait_and_numeric_prompt_measurements_survive_reload(self):
        with patch.object(r, 'get_astrbot_data_path', return_value=str(self.root)):
            obj = r.DafeiyuRuntime(self.context)
        original = r.session_lock_manager.acquire_lock
        await obj.initialize()
        try:
            e = event();turn = d.Turn(e);e.set_extra(d.KEY, turn)
            token = d.CURRENT.set(turn)
            try:
                held = asyncio.Event();release = asyncio.Event()
                async def hold():
                    async with original(e.unified_msg_origin):held.set();await release.wait()
                task = asyncio.create_task(hold());await held.wait()
                async def waiter():
                    async with r.session_lock_manager.acquire_lock(e.unified_msg_origin):pass
                pending = asyncio.create_task(waiter());await asyncio.sleep(.02);release.set();await task;await pending
                self.assertGreaterEqual(turn.phases['session_wait'], 10)
                d.request_metrics(ProviderRequest(system_prompt='private secret', contexts=[{'role': 'user', 'content': 'private text'}], extra_user_content_parts=[TextPart(text='private fact')]), e)
                self.assertEqual(turn.metrics['context_messages'], 1)
                self.assertNotIn('private', json.dumps(turn.metrics))
            finally:d.CURRENT.reset(token)
        finally:await obj.terminate()
        self.assertIs(r.session_lock_manager.acquire_lock, obj.original_session_lock)


if __name__ == '__main__':unittest.main(verbosity=2)
