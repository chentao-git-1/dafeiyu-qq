"""Run native decoration and QQ sending with captured OneBot calls; no external services."""
import copy
import importlib
import inspect
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch

import test_cognition as base
f=base.fixture
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.message.components import Plain,Record
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from data.plugins.dafeiyu_runtime.reliability import ReplyReliability
voice=importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_voice_request.main')
decorate=importlib.import_module('astrbot.core.pipeline.result_decorate.stage')
respond=importlib.import_module('astrbot.core.pipeline.respond.stage')


async def run_stage(stage,event):
    result=stage.process(event)
    if inspect.isasyncgen(result):
        async for _ in result:pass
    else:await result


class NativeTextDelivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config=copy.deepcopy(DEFAULT_CONFIG)
        self.config['platform_settings']['id_whitelist']=[f.OWNER,'dafeiyu-qq:GroupMessage:7000000101']
        self.config['platform_settings']['enable_id_white_list']=True
        self.config['platform_settings']['reply_with_quote']=False
        self.config['platform_settings']['reply_with_mention']=False
        self.config['platform_settings']['segmented_reply'].update(enable=True,only_llm_result=True,
            split_mode='regex',regex=r'.*?[。？！~…\r\n]+|.+$',words_count_threshold=150,interval='0,0')
        self.config['provider_tts_settings'].update(enable=True,trigger_probability=1,dual_output=False)
        self.config['t2i']=False
        self.tts=NS(get_audio=AsyncMock(return_value='/tmp/synthetic.wav'))
        self.service=NS(get_config=lambda *args:self.config,get_using_tts_provider_async=AsyncMock(return_value=self.tts))
        self.ctx=NS(astrbot_config=self.config,plugin_manager=NS(context=self.service))
        self.original_check=AsyncMock(return_value=True)
        self.check_patch=patch.object(voice.SessionServiceManager,'should_process_tts_request',new=self.original_check)
        self.check_patch.start();self.addCleanup(self.check_patch.stop)
        self.v=voice.DafeiyuVoiceRequest(self.service);await self.v.initialize();self.addAsyncCleanup(self.v.terminate)
        self.handlers=patch.object(decorate.star_handlers_registry,'get_handlers_by_event_type',return_value=[])
        self.handlers.start();self.addCleanup(self.handlers.stop)
        self.after=AsyncMock(return_value=False)
        self.after_patch=patch.object(respond,'call_event_hook',new=self.after)
        self.after_patch.start();self.addCleanup(self.after_patch.stop)
        self.metric_patch=patch('astrbot.core.platform.astr_message_event.Metric.upload',new=AsyncMock())
        self.metric_patch.start();self.addCleanup(self.metric_patch.stop)

    async def deliver(self,mode='text',failure=False,group=False,body='我刚写了两句小诗。\n\n想留一点没说满的余味。'):
        self.v._intent=lambda event:mode
        self.tts.get_audio.side_effect=RuntimeError('synthetic synthesis failure') if failure else None
        message=f.event('你最近在做什么').message_obj
        if group:
            message.type=MessageType.GROUP_MESSAGE;message.group_id='7000000101';message.session_id=message.group_id
            self.config['platform_settings']['reply_with_quote']=True
            self.config['platform_settings']['reply_with_mention']=True
        bot=NS(send_private_msg=AsyncMock(),send_group_msg=AsyncMock())
        event=AiocqhttpMessageEvent('你最近在做什么',message,PlatformMetadata('aiocqhttp','test','dafeiyu-qq'),
            message.group_id if group else f.OWNER,bot)
        event.set_result(event.chain_result([Plain(body)]).set_result_content_type(ResultContentType.LLM_RESULT))
        reliability=ReplyReliability(AiocqhttpMessageEvent.send)
        async def send(chain):return await reliability.send(event,chain)
        event.send=send
        await self.v.honor_delivery(event)
        self.before_native_type=event.get_result().result_content_type
        self.decorator=decorate.ResultDecorateStage();await self.decorator.initialize(self.ctx)
        self.responder=respond.RespondStage();await self.responder.initialize(self.ctx)
        with patch.object(Record,'convert_to_base64',new=AsyncMock(return_value='YXVkaW8=')):
            await run_stage(self.decorator,event);await run_stage(self.responder,event)
        return event,bot

    async def test_text_reaches_two_distinct_qq_calls_with_receipts_and_one_after_send_hook(self):
        event,bot=await self.deliver()
        self.tts.get_audio.assert_not_awaited()
        self.assertEqual(self.before_native_type,ResultContentType.LLM_RESULT)
        self.assertEqual(bot.send_private_msg.await_count,2)
        texts=[''.join(p['data']['text'] for p in c.kwargs['message'] if p['type']=='text') for c in bot.send_private_msg.await_args_list]
        self.assertEqual(texts,['我刚写了两句小诗。','想留一点没说满的余味。'])
        receipts=event.get_extra('dafeiyu_confirmed_delivery');self.assertEqual([r['text'] for r in receipts],texts)
        self.after.assert_awaited_once()

    async def test_speech_failure_falls_back_to_two_text_bubbles_without_a_second_synthesis(self):
        event,bot=await self.deliver(mode='voice',failure=True)
        self.tts.get_audio.assert_awaited_once()
        self.assertEqual(self.before_native_type,ResultContentType.LLM_RESULT)
        self.assertTrue(event.get_extra('_dafeiyu_skip_native_tts'))
        self.assertEqual(bot.send_private_msg.await_count,2)

    async def test_successful_voice_stays_one_audio_message(self):
        event,bot=await self.deliver(mode='voice')
        self.tts.get_audio.assert_awaited_once();self.assertEqual(bot.send_private_msg.await_count,1)
        parts=bot.send_private_msg.await_args.kwargs['message']
        self.assertEqual(sum(p['type']=='record' for p in parts),1)
        self.assertEqual(self.before_native_type,ResultContentType.GENERAL_RESULT)

    async def test_short_code_block_preserves_format_as_one_message(self):
        body='```python\nprint("第一句。")\nprint("第二句。")\n```'
        event,bot=await self.deliver(body=body)
        self.tts.get_audio.assert_not_awaited();self.assertEqual(bot.send_private_msg.await_count,1)
        self.assertEqual(bot.send_private_msg.await_args.kwargs['message'][0]['data']['text'],body)

    async def test_group_quote_and_mention_appear_only_on_first_bubble(self):
        event,bot=await self.deliver(group=True)
        self.assertEqual(bot.send_group_msg.await_count,2)
        first,second=[c.kwargs['message'] for c in bot.send_group_msg.await_args_list]
        self.assertEqual(sum(p['type']=='reply' for p in first),1)
        self.assertEqual(sum(p['type']=='at' for p in first),1)
        self.assertFalse(any(p['type'] in ('reply','at') for p in second))

    async def test_native_tts_override_is_per_event_and_unload_restores_original(self):
        one,two=f.event('one'),f.event('two');one.set_extra('_dafeiyu_skip_native_tts',True)
        self.assertFalse(await voice.SessionServiceManager.should_process_tts_request(one))
        self.assertTrue(await voice.SessionServiceManager.should_process_tts_request(two))
        await self.v.terminate()
        self.assertIs(voice.SessionServiceManager.should_process_tts_request,self.original_check)


class BriefReview(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=f.NativeHooks.asyncSetUp
    asyncTearDown=f.NativeHooks.asyncTearDown
    run_hooks=f.NativeHooks.run_hooks
    ready=base.SingleGeneration.ready

    async def test_multi_paragraph_casual_reply_below_old_threshold_is_still_reviewed(self):
        event,_=await self.ready('在吗？')
        self.plugin.reviewed=AsyncMock(return_value=NS(segments=('在呀。',)))
        response=NS(completion_text=base.envelope('在呀。\n\n旧手帐那页还空着，我又想起了那张画。'),tools_call_name=[])
        await self.plugin.review_response(event,response)
        self.plugin.reviewed.assert_awaited_once();self.assertEqual(response.completion_text,'在呀。')


if __name__=='__main__':unittest.main()
