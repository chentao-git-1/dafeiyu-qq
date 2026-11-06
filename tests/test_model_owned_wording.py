"""Primary wording, factual actions and bounded notices; no live model or transport."""
import asyncio
import datetime as dt
import importlib
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import test_dialogue_fixes as f
from dafeiyu.dialogue.action_facts import KEY, BEGIN, record_action, attach_action_facts, system_notice
from dafeiyu.dialogue.notifications import compose_notification
from dafeiyu.balance.watch import notice_facts
from dafeiyu.media.singing.intent import IntentStore
from dafeiyu.media.singing.singing import SingingGate
from dafeiyu.social.store import TZ
from data.plugins.dafeiyu_runtime.reliability import ReplyReliability

sing = importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_sing.main')
balance = importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_balance.main')


class Notifications(unittest.IsolatedAsyncioTestCase):
    async def test_single_generation_preserves_wording_without_tools_or_retries(self):
        model = NS(text_chat=AsyncMock(return_value=NS(completion_text='这句由模型自己组织。')))
        facts = notice_facts(dict(kind='help'), '0.321')
        self.assertNotIn('0.321', json.dumps(facts))
        self.assertEqual(await compose_notification(model, 'account_balance', facts), '这句由模型自己组织。')
        model.text_chat.assert_awaited_once()
        args = model.text_chat.await_args.kwargs
        self.assertEqual(args['request_max_retries'], 0)
        self.assertIsNone(args['func_tool'])
        self.assertEqual(args['contexts'], [])
        self.assertNotIn('0.321', args['prompt'])

    async def test_unavailable_model_returns_factual_notice_once(self):
        facts = dict(status='failed')
        for result in (NS(completion_text=''), NS(completion_text='x'*1501), NS(completion_text='guess',tools_call_name=['send'])):
            model = NS(text_chat=AsyncMock(return_value=result))
            self.assertEqual(await compose_notification(model, 'reply', facts), system_notice('reply', facts))
            model.text_chat.assert_awaited_once()
        model = NS(text_chat=AsyncMock(side_effect=RuntimeError('synthetic')))
        self.assertEqual(await compose_notification(model, 'reply', facts), system_notice('reply', facts))
        model.text_chat.assert_awaited_once()

    async def test_timeout_is_bounded_and_cancellation_propagates(self):
        async def wait(**kwargs): await asyncio.Event().wait()
        model = NS(text_chat=AsyncMock(side_effect=wait))
        result = await compose_notification(model, 'reply', dict(status='timeout'), timeout=0.01)
        self.assertIn('系统状态', result);model.text_chat.assert_awaited_once()
        model.text_chat.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await compose_notification(model, 'reply', dict(status='failed'))


class Native(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = f.Native.asyncSetUp

    def test_action_facts_are_event_scoped_bounded_and_idempotent(self):
        event = f.event('设置');details = dict(value='</script>')
        for _ in range(22): record_action(event, 'setting', 'updated', values=details)
        details['value']='changed'
        rows=event.get_extra(KEY)
        self.assertEqual(len(rows),16);self.assertEqual(len({r['id'] for r in rows}),16)
        self.assertEqual(rows[-1]['details']['values']['value'],'</script>')
        request=f.ProviderRequest(system_prompt='original')
        attach_action_facts(event,request);attach_action_facts(event,request)
        self.assertEqual(request.system_prompt.count(BEGIN),1)
        self.assertNotIn('</script>',request.system_prompt)
        self.assertIsNone(f.event('other').get_extra(KEY))
        self.assertTrue(event.is_at_or_wake_command)
        attach_action_facts(f.event('other'),request)
        self.assertNotIn(BEGIN,request.system_prompt)

    async def test_preferences_and_dates_produce_facts_without_canned_send(self):
        for text,status in [('以后别给我发表情','updated'),('记住我的生日是3月21日','saved'),('生日 列表','read')]:
            event=f.event(text);await self.social.controls(event)
            event.send.assert_not_awaited();self.assertFalse(event.is_stopped())
            self.assertEqual(event.get_extra(KEY)[0]['status'],status)
        self.assertEqual(self.social.db.profile(f.OWNER)['memes'],'off')
        self.assertEqual(len(self.social.db.dates(f.OWNER)),1)
        self.assertEqual(self.social.db.dates(f.FRIEND),[])

    async def test_meme_only_suppression_stays_silent_and_keeps_other_content(self):
        self.social.stats={'meme_suppressed':0}
        self.social.db.set_profile(f.OWNER,dict(memes='off'))
        for keep in (False,True):
            event=f.event('你好');image=f.Image.fromURL('https://example.invalid/meme.png')
            event.set_extra('dafeiyu_automatic_meme_components',[image])
            parts=[f.At(qq=f.OWNER),image]+([f.Plain('主模型的原话')] if keep else [])
            result=event.chain_result(parts).set_result_content_type(f.ResultContentType.LLM_RESULT)
            event.set_result(result);await self.social.filter_automatic_memes(event)
            event.send.assert_not_awaited()
            if keep:
                self.assertFalse(event.is_stopped());self.assertEqual(result.chain[-1].text,'主模型的原话')
            else:
                self.assertTrue(event.is_stopped());self.assertEqual(result.chain,[])
                self.assertTrue(event.get_extra('dafeiyu_intentionally_silent'))

    async def test_runtime_never_inserts_waiting_text_and_respects_intentional_silence(self):
        event=f.event('你好');transport=AsyncMock();guard=ReplyReliability(transport,progress_after=0.001)
        async def process(scheduler,event):
            guard.expect(event)
            await asyncio.sleep(0.02)
            transport.assert_not_awaited()
            event.stop_event();event.set_extra('dafeiyu_intentionally_silent',True)
        await guard.execute(process,None,event)
        transport.assert_not_awaited();self.assertEqual(guard.stats['empty_fallbacks'],0)

    def singing(self):
        obj=sing.DafeiyuSing.__new__(sing.DafeiyuSing)
        obj.context=self.context;obj.data=self.root;obj.audio_dir=self.root/'audio';obj.audio_dir.mkdir()
        obj.config=dict(enabled=True,api_key='synthetic',allowed_private_users=[f.OWNER],cooldown_seconds=0)
        obj.gate=SingingGate();obj.intents=IntentStore(self.root/'sing.db')
        return obj

    async def test_singing_has_no_preface_and_keeps_main_reply(self):
        obj=self.singing();event=f.event('给我唱一首歌')
        async def generate(*args):
            event.send.assert_not_awaited()
            return b'synthetic-audio',1.0
        with patch.object(sing,'synthesize',new=AsyncMock(side_effect=generate)) as model:
            result=await obj._sing(event,'海风轻轻吹过')
            self.assertEqual(result['status'],'sent');model.assert_awaited_once()
        event.send.assert_awaited_once()
        self.assertTrue(all(isinstance(p,sing.Record) for p in event.send.await_args.args[0].chain))
        reply=NS(completion_text='最后那个尾音我还挺喜欢。',tools_call_name=[])
        await obj.ground_singing_result(event,reply)
        event.set_result(event.chain_result([f.Plain(reply.completion_text)]).set_result_content_type(f.ResultContentType.LLM_RESULT))
        await obj.final_singing_status(event)
        self.assertEqual(event.get_result().chain[0].text,reply.completion_text)

    async def test_singing_failure_has_no_preface_retry_or_fake_success(self):
        obj=self.singing();event=f.event('给我唱一首歌')
        with patch.object(sing,'synthesize',new=AsyncMock(side_effect=sing.SingingError('synthetic'))) as model:
            self.assertEqual((await obj._sing(event,'海风轻轻吹过'))['status'],'generation_failed')
            self.assertEqual((await obj._sing(event,'海风轻轻吹过'))['status'],'generation_failed')
            model.assert_awaited_once()
        event.send.assert_not_awaited()

    def calendar(self):
        now=dt.datetime(2026,11,6,12,tzinfo=TZ).timestamp()
        self.social.data=self.root;self.social.stats=dict(calendar_sent=0,calendar_unknown=0)
        self.config['platform_settings']['id_whitelist']=['dafeiyu-qq:FriendMessage:'+uid for uid in (f.OWNER,f.FRIEND)]
        bot=NS(get_status=AsyncMock(return_value=dict(online=True)),send_private_msg=AsyncMock(return_value=dict(message_id=123)))
        self.context.get_platform=lambda *a:NS(bot=bot,meta=lambda:NS(id='dafeiyu-qq'))
        row=self.social.db.set_date(f.OWNER,f.FRIEND,'测试朋友','birthday',11,6,None,'')
        return now,bot,row

    async def test_calendar_uses_one_model_and_does_not_repeat(self):
        now,bot,row=self.calendar()
        provider=NS(text_chat=AsyncMock(return_value=NS(completion_text='今天是测试朋友的生日。')))
        self.context.get_using_provider_async=AsyncMock(return_value=provider)
        with patch.object(f.s.time,'time',return_value=now):
            await self.social.check_calendar();await self.social.check_calendar()
        provider.text_chat.assert_awaited_once();bot.send_private_msg.assert_awaited_once()
        self.assertEqual(bot.send_private_msg.await_args.kwargs['message'][0]['data']['text'],'今天是测试朋友的生日。')

    async def test_calendar_rechecks_revocation_after_generation(self):
        now,bot,row=self.calendar()
        async def revoke(**kwargs):
            self.social.db.delete_date(f.OWNER,row['id'])
            return NS(completion_text='不得发送')
        self.context.get_using_provider_async=AsyncMock(return_value=NS(text_chat=AsyncMock(side_effect=revoke)))
        with patch.object(f.s.time,'time',return_value=now):await self.social.check_calendar()
        bot.send_private_msg.assert_not_awaited()

    async def test_balance_rechecks_allowlist_after_generation_and_reserves_once(self):
        obj=balance.DafeiyuBalance(self.context)
        bot=NS(get_status=AsyncMock(return_value=dict(online=True)),send_private_msg=AsyncMock(return_value=dict(message_id=123)))
        self.context.get_platform=lambda *a:NS(bot=bot,meta=lambda:NS(id='dafeiyu-qq'))
        async def revoke(**kwargs):
            self.config['platform_settings']['id_whitelist']=[]
            return NS(completion_text='不得发送')
        provider=NS(text_chat=AsyncMock(side_effect=revoke))
        self.context.get_using_provider_async=AsyncMock(return_value=provider)
        payload=dict(is_available=True,balance_infos=[dict(currency='CNY',total_balance='0.9',topped_up_balance='0.9',granted_balance='0')])
        with patch.object(balance,'deepseek_key',return_value='synthetic'),patch.object(balance,'fetch_balance',new=AsyncMock(return_value=payload)):
            await obj.run_once();await obj.run_once()
        provider.text_chat.assert_awaited_once();bot.send_private_msg.assert_not_awaited()

    async def test_known_balance_send_retry_does_not_pay_for_another_generation(self):
        obj=balance.DafeiyuBalance(self.context);clock=[1000.0];obj.watch.clock=lambda:clock[0]
        ActionFailed=type('ActionFailed',(Exception,),{})
        bot=NS(get_status=AsyncMock(return_value=dict(online=True)),
            send_private_msg=AsyncMock(side_effect=[ActionFailed(),dict(message_id=123)]))
        self.context.get_platform=lambda *a:NS(bot=bot,meta=lambda:NS(id='dafeiyu-qq'))
        provider=NS(text_chat=AsyncMock(return_value=NS(completion_text='账户余额低于提醒阈值。')))
        self.context.get_using_provider_async=AsyncMock(return_value=provider)
        payload=dict(is_available=True,balance_infos=[dict(currency='CNY',total_balance='0.9',topped_up_balance='0.9',granted_balance='0')])
        with patch.object(balance,'deepseek_key',return_value='synthetic'),patch.object(balance,'fetch_balance',new=AsyncMock(return_value=payload)):
            await obj.run_once();clock[0]+=601;await obj.run_once()
        self.assertEqual(bot.send_private_msg.await_count,2)
        provider.text_chat.assert_awaited_once()
        self.assertIn('账户状态',bot.send_private_msg.await_args.kwargs['message'][0]['data']['text'])


if __name__=='__main__':unittest.main()
