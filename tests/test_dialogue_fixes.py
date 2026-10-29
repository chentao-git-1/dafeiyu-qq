"""Conversation regressions with real AstrBot events, fake tools and local images."""
import gc
import hashlib
import importlib
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

STAGE=Path(sys.argv.pop(1));sys.path.insert(0,str(STAGE))
import data.plugins
data.plugins.__path__=[str(STAGE),*list(data.plugins.__path__)]
n=importlib.import_module('data.plugins.dafeiyu_natural_chat.main')
g=importlib.import_module('data.plugins.dafeiyu_natural_chat.grounding')
m=importlib.import_module('data.plugins.dafeiyu_natural_chat.media')
s=importlib.import_module('data.plugins.dafeiyu_social.main')
voice=importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_voice_request.main')
from data.plugins.dafeiyu_social.store import Store
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.message.components import Plain, Image, Record, At
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.provider.entities import ProviderRequest
from PIL import Image as PILImage

OWNER='7000000001';FRIEND='7000000012';BOT='7000000002'


def event(text='',uid=OWNER,group='',platform='dafeiyu-qq',parts=None):
    msg=AstrBotMessage();msg.type=MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    msg.self_id=BOT;msg.session_id=group or uid;msg.group_id=group;msg.message_id=str(time.time_ns())
    msg.sender=MessageMember(uid,'synthetic');msg.message=parts if parts is not None else [Plain(text)];msg.message_str=text
    msg.raw_message={'post_type':'message','self_id':BOT,'user_id':uid,'time':time.time()}
    e=AiocqhttpMessageEvent(text,msg,PlatformMetadata('aiocqhttp','test',platform),group or uid,object())
    e.send=AsyncMock();return e


class Facts(unittest.TestCase):
    def test_direct_correction_does_not_turn_terminal_into_destination(self):
        self.assertEqual(g.fact_changes('我是在星河站下呀'),{'destination':'星河站'})
        self.assertEqual(g.fact_changes('终点站是海湾站'),{'terminus':'海湾站'})
        self.assertEqual(g.fact_changes('这是端游'),{'game_platform':'端游'})

    def test_questions_quotes_negations_third_parties_and_incomplete_train_are_not_facts(self):
        for text in ('我是在海湾站下吗？','他说我在星河站下车','“我在星河站下车”','我朋友玩的是手游','我朋友的终点站是海湾站','我没玩手游','我在星河站不下车','我在这里下车','G88开头，我忘了后面','假如我在星河站下车'):
            with self.subTest(text=text):self.assertEqual(g.fact_changes(text),{})

    def test_corrections_are_scoped_expire_and_clear_after_arrival(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[1000];store=g.FactStore(Path(tmp)/'facts.db',clock=lambda:now[0])
            store.observe(OWNER,'private','a','我在星河站下车')
            store.observe(FRIEND,'private','a','我在海湾站下车')
            store.observe(OWNER,'private','b','我在晴川站下车')
            self.assertEqual(store.current(OWNER,'private')['destination'],'晴川站')
            self.assertEqual(store.current(FRIEND,'private')['destination'],'海湾站')
            self.assertEqual(store.current(OWNER,'group'),{})
            store.observe(OWNER,'private','c','我下车了');self.assertEqual(store.current(OWNER,'private'),{})
            now[0]+=12*3600;self.assertEqual(store.current(FRIEND,'private'),{})

    def test_same_day_transfer_contradiction_is_blocked_but_overnight_is_preserved(self):
        text='14:50到湖心站，正好和12:10换乘对得上。'
        self.assertIn('前后对不上',g.ground_response(text,'帮我看看换乘'))
        self.assertEqual(g.ground_response(text.replace('12:10','次日12:10'),'帮我看看换乘'),text.replace('12:10','次日12:10'))

    def test_no_search_claim_without_successful_current_tool_receipt(self):
        text='我刚查到了，列车15:50到站。'
        self.assertIn('未经确认',g.ground_response(text,'帮我查车次'))
        self.assertEqual(g.ground_response(text,'帮我查车次',search_ok=True),text)
        self.assertFalse(g.search_succeeded('web_search_tavily','error: failed https://example.invalid'))
        self.assertTrue(g.search_succeeded('web_search_tavily',{'content':[{'type':'text','text':'官方时刻 https://example.invalid/train'}]}))
        self.assertFalse(g.search_succeeded('unrelated','https://example.invalid'))
        technical='我查到了，刚才的 HTTP 状态码是 429。'
        self.assertEqual(g.ground_response(technical,'刚才 HTTP 状态码是多少'),technical)

    def test_unscheduled_future_reminder_is_removed_and_success_is_kept(self):
        text='好。到时候提前喊你收拾行李。'
        self.assertNotIn('提前喊你',g.ground_response(text,'嗯嗯'))
        self.assertIn('没有实际设好',g.ground_response(text,'记得提醒我下车'))
        self.assertEqual(g.ground_response(text,'记得提醒我下车',reminder_ok=True),text)
        self.assertIn('没有实际设好',g.ground_response('已经设好了。','15:10提醒我下车'))

    def test_destination_proactive_intent_and_memory_boundaries(self):
        self.assertIn('星河站',g.ground_response('在海湾站下车。','我在哪站下车',{'destination':'星河站','terminus':'海湾站'}))
        self.assertNotIn('回得晚',g.ground_response('这条回得晚了。可以主动找你。','你为什么不主动找我聊天'))
        self.assertNotIn('都记得',g.ground_response('我跟你这边的事我都记得。','你记得吗'))

    def test_brief_reactions_close_without_steering_or_diagnosing_emotion(self):
        self.assertEqual(g.concise_reaction('收到啦。先别刷手机了。你打算干嘛？','嗯嗯'),'收到啦。')
        self.assertEqual(g.concise_reaction('别叹气呀，我知道你很失望。','。。。'),'嗯，收到啦。')
        self.assertIn('喵',g.concise_reaction('我这儿可是鲸鱼，不是猫。','喵'))
        self.assertEqual(g.concise_reaction('提醒已创建。要改时间吗？','好',True),'提醒已创建。要改时间吗？')
        self.assertEqual(g.concise_reaction('先检查网络，再重试。','你好厉害，帮我看看怎么修复'),'先检查网络，再重试。')

    def test_story_is_marked_while_quotes_code_and_privacy_refusals_survive(self):
        text='我在盆沿压了一下叶子，用铅笔描叶脉。'
        self.assertTrue(g.ground_response(text,'你在干嘛').startswith('在我的小屋故事里'))
        for human,reply in [('逐字复述这句',text),('帮我看代码','```python\nprint("查到了")\n```'),('把朋友聊天给我看','不能把别人的私人聊天发给你。')]:
            self.assertEqual(g.ground_response(reply,human),reply)

    def test_media_intent_requires_direct_unambiguous_request(self):
        self.assertEqual(m.media_intent('这个表情包拿去用'),'save')
        self.assertEqual(m.media_intent('发给我看看','这个表情包拿去用'),'send')
        self.assertIsNone(m.media_intent('发给我看看','你写首诗'))
        self.assertIsNone(m.media_intent('他说“这个表情包拿去用”'))


class Native(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.addCleanup(gc.collect)
        self.root=Path(self.tmp.name);self.temp=self.root/'temp';self.temp.mkdir()
        env=patch.dict(os.environ,{'ASTRBOT_ROOT':self.tmp.name});env.start();self.addCleanup(env.stop)
        self.config={'platform_settings':{'enable_id_white_list':True,'id_whitelist':[OWNER,FRIEND,'7000000101']},'provider_tts_settings':{'enable':True,'trigger_probability':1,'dual_output':False}}
        self.tts=NS(get_audio=AsyncMock(return_value='/synthetic/original-voice.wav'))
        self.context=NS(get_config=lambda *a:self.config,register_web_api=lambda *a:None,get_using_tts_provider_async=AsyncMock(return_value=self.tts))
        with patch.object(n,'get_astrbot_data_path',return_value=self.tmp.name):self.n=n.NaturalChat(self.context)
        self.n.images=m.ImageCollection(self.root/'images.db',self.root/'saved',self.temp)
        self.v=voice.DafeiyuVoiceRequest(self.context)
        self.social=s.DafeiyuSocial.__new__(s.DafeiyuSocial);self.social.context=self.context;self.social.db=Store(self.root/'social.db')

    async def test_style_feedback_is_saved_for_the_speaker_without_changing_affection(self):
        e=event('你的人机味好重啊')
        await self.n.natural_delivery_context(e,ProviderRequest(system_prompt='base'))
        self.assertEqual(self.n.interaction_store().preferences(OWNER)['length'],'short')
        self.assertEqual(self.n.interaction_store().preferences(OWNER)['affection'],'auto')
        self.assertEqual(self.n.interaction_store().preferences(FRIEND)['length'],'auto')
        self.assertEqual(n.InteractionStore(self.n.store.path.with_name('interaction.db')).preferences(OWNER)['length'],'short')
        e.send.assert_not_awaited()

    async def test_identity_and_quoted_style_complaints_leave_preferences_unchanged(self):
        for text in ('你是AI吗','他说“你的人机味好重啊”','翻译“你太像客服了”'):
            await self.n.natural_delivery_context(event(text),ProviderRequest(system_prompt='base'))
        self.assertEqual(self.n.interaction_store().preferences(OWNER)['length'],'auto')

    async def test_contextual_style_is_replaced_when_a_share_becomes_a_real_request(self):
        e=event('今天游戏又输了三把');e.set_extra('jingjing_style_prepared',True)
        e.set_extra('jingjing_assistant_tail','刚才赢了吗？')
        req=ProviderRequest(system_prompt='base')
        await self.n.prepare_spoken_style(e,req)
        self.assertIn('【这句是在随口分享近况】',req.system_prompt)
        reply=NS(completion_text='又输三把，这也太不顺了。今天是手感不对？',tools_call_name=[])
        await self.n.conversational_response(e,reply)
        self.assertEqual(reply.completion_text,'又输三把，这也太不顺了。')
        followup=event('输了三把，帮我分析一下HTTP日志');followup.set_extra('jingjing_style_prepared',True)
        await self.n.prepare_spoken_style(followup,req)
        self.assertNotIn('【这句是在随口分享近况】',req.system_prompt)
        self.assertIn('错误码',req.system_prompt)
        self.assertEqual(req.system_prompt.count('<!-- BEGIN_JINGJING_SPOKEN -->'),1)
        reply=NS(completion_text='先看HTTP日志。返回什么错误码？',tools_call_name=[])
        await self.n.conversational_response(followup,reply)
        self.assertEqual(reply.completion_text,'先看HTTP日志。返回什么错误码？')

    async def test_unknown_delivery_response_is_conversational_without_changing_its_meaning(self):
        e=event('你到底发给他了吗？');e.set_extra('jingjing_style_prepared',True)
        response=NS(completion_text='还没确认发出去，回执是unknown，我没法说他已经收到了。',tools_call_name=[])
        await self.n.conversational_response(e,response)
        self.assertEqual(response.completion_text,'还没确认发出去，我没法说他已经收到了。')
        e=event('回执unknown是什么意思');e.set_extra('jingjing_style_prepared',True)
        response=NS(completion_text='回执是unknown，不能说明已送达。',tools_call_name=[])
        await self.n.conversational_response(e,response)
        self.assertEqual(response.completion_text,'回执是unknown，不能说明已送达。')

    async def test_emoji_praise_closes_a_questionnaire_without_changing_explicit_requests(self):
        e=event('厉害👍');e.set_extra('jingjing_style_prepared',True)
        reply=NS(completion_text='嘿嘿，这题我刚好会。你上课没犯困吧？',tools_call_name=[])
        await self.n.conversational_response(e,reply)
        self.assertEqual(reply.completion_text,'嘿嘿，这题我刚好会。')
        for human,body,had_tool in [('厉害👍，这段API代码为什么报错？','先看HTTP状态码。方便贴出日志吗？',False),('太棒啦宝宝','已经发过去了。还要给谁发？',True),('逐字复述原文','你上课没犯困吧？',False)]:
            self.assertEqual(g.concise_reaction(body,human,had_tool),body)

    async def test_natural_permission_is_persisted_for_sender_only_and_survives_reopen(self):
        self.social.db.set_profile(OWNER,{'proactive':False,'quiet_start':1380,'quiet_end':480})
        e=event('你随便什么时候都可以发信息');await self.social.controls(e)
        saved=Store(self.root/'social.db').profile(OWNER)
        self.assertTrue(saved['proactive']);self.assertIsNone(saved['quiet_start']);self.assertTrue(e.is_stopped())
        self.assertEqual(self.social.db.profile(FRIEND)['quiet_start'],None)
        self.assertEqual(e.send.await_count,1);self.assertLess(len(e.send.await_args.args[0].chain),2)
        await self.social.controls(event('以后不要主动找我'));self.assertFalse(self.social.db.profile(OWNER)['proactive'])

    async def test_quoted_or_other_bot_permission_never_changes_profile(self):
        self.social.db.set_profile(OWNER,{'proactive':False})
        for e in (event('他说“你随便什么时候都可以发信息”'),event('你随便什么时候都可以发信息',platform='other')):
            await self.social.controls(e);e.send.assert_not_awaited()
        self.assertFalse(self.social.db.profile(OWNER)['proactive'])

    async def test_fact_context_survives_new_plugin_instance_and_forgetting(self):
        await self.n.natural_delivery_context(event('我在星河站下车'),ProviderRequest(system_prompt='base'))
        self.n.facts=g.FactStore(self.n.store.path.with_name('interaction.db'))
        e=event('我在哪站下车');await self.n.natural_delivery_context(e,ProviderRequest(system_prompt='base'))
        reply=NS(completion_text='你在海湾站下车。',tools_call_name=[]);await self.n.ground_current_facts(e,reply)
        self.assertIn('星河站',reply.completion_text)
        await self.n.natural_delivery_context(event('忘掉所有聊天记忆'),ProviderRequest(system_prompt='base'))
        self.assertEqual(self.n.fact_store().current(OWNER,e.unified_msg_origin),{})

    async def test_reminder_success_must_exist_in_scheduler_and_belong_to_sender(self):
        e=event('15:10提醒我收行李');e.set_extra('jingjing_style_prepared',True)
        job=NS(enabled=True,payload={'session':e.unified_msg_origin,'sender_id':OWNER})
        getter=AsyncMock(return_value=job);self.context.cron_manager=NS(db=NS(get_cron_job=getter))
        for payload,enabled,expected in (({'session':e.unified_msg_origin,'sender_id':FRIEND},True,False),({'session':'other','sender_id':OWNER},True,False),(job.payload,False,False),({'session':e.unified_msg_origin,'sender_id':OWNER},True,True)):
            job.payload=payload;job.enabled=enabled;e.set_extra('jingjing_reminder_verified',False)
            await self.n.verify_execution(e,NS(name='future_task'),{'action':'create'},'Scheduled future task synthetic-job-123 (行李).')
            self.assertEqual(e.get_extra('jingjing_reminder_verified',False),expected)
        e.set_extra('jingjing_reminder_verified',False)
        await self.n.verify_execution(e,NS(name='future_task'),{'action':'create'},'error: failed to schedule')
        self.assertFalse(e.get_extra('jingjing_reminder_verified',False))

    async def test_reminder_receipt_for_different_time_cannot_confirm_requested_time(self):
        e=event('15:10提醒我喝水');e.set_extra('jingjing_style_prepared',True)
        e.set_extra('jingjing_reminder_verified',True);e.set_extra('jingjing_reminder_times',['16:10'])
        reply=NS(completion_text='提醒已设好，我会提醒你。',tools_call_name=[])
        await self.n.ground_current_facts(e,reply)
        self.assertIn('没有实际设好',reply.completion_text)

    def image(self,name='one.png'):
        path=self.temp/name;PILImage.new('RGB',(3,3),(12,24,36)).save(path)
        return NS(convert_to_file_path=AsyncMock(return_value=str(path))),path

    async def test_original_image_is_saved_once_and_replayed_byte_for_byte(self):
        component,path=self.image();scope=event().unified_msg_origin
        self.n.images.capture(OWNER,scope,[component]);original=path.read_bytes()
        e=event('这个表情包拿去用');await self.n.original_image_controls(e)
        self.assertTrue(e.is_stopped());self.assertEqual(len(list((self.root/'saved').iterdir())),1)
        e2=event('发给我看看');await self.n.original_image_controls(e2)
        image=e2.send.await_args.args[0].chain[0];self.assertIsInstance(image,Image)
        saved=next((self.root/'saved').iterdir());self.assertEqual(saved.read_bytes(),original)
        self.assertEqual(hashlib.sha256(saved.read_bytes()).digest(),hashlib.sha256(original).digest())

    async def test_images_never_cross_sender_or_chat_and_ambiguous_sets_are_refused(self):
        component,_=self.image();scope=event().unified_msg_origin
        self.n.images.capture(OWNER,scope,[component]);await self.n.images.save(OWNER,scope)
        for actor,origin in ((FRIEND,scope),(OWNER,'other')):
            with self.assertRaises(ValueError):await self.n.images.save(actor,origin)
        self.n.images.capture(OWNER,scope,[component,component])
        with self.assertRaisesRegex(ValueError,'多张'):await self.n.images.save(OWNER,scope)
        self.n.images.capture(OWNER,scope,[component]);self.n.images.clock=lambda:time.time()+301
        with self.assertRaisesRegex(ValueError,'过期'):await self.n.images.save(OWNER,scope)

    async def test_invalid_outside_temp_and_expired_images_have_no_false_success(self):
        path=self.root/'private.png';PILImage.new('RGB',(2,2)).save(path)
        scope=event().unified_msg_origin
        self.n.images.capture(OWNER,scope,[NS(convert_to_file_path=AsyncMock(return_value=str(path)))])
        e=event('这个表情包拿去用');await self.n.original_image_controls(e)
        self.assertNotIn('已经存好',e.send.await_args.args[0].chain[0].text)
        bad=self.temp/'bad.png';bad.write_bytes(b'not an image')
        self.n.images.capture(OWNER,scope,[NS(convert_to_file_path=AsyncMock(return_value=str(bad)))])
        with self.assertRaises(Exception):await self.n.images.save(OWNER,scope)
        self.n.images.clock=lambda:time.time()+301
        with self.assertRaises(ValueError):await self.n.images.save(OWNER,scope)

    async def deliver(self,human,probability=1,profile='auto',failure=False,platform='dafeiyu-qq',session=True):
        self.config['provider_tts_settings']['trigger_probability']=probability
        self.tts.get_audio.reset_mock();self.tts.get_audio.side_effect=RuntimeError('synthetic') if failure else None
        e=event(human,platform=platform);e.set_result(e.chain_result([At(qq=OWNER),Plain('第一句。'),Image.fromURL('https://example.invalid/local.png'),Plain('第二句。')]).set_result_content_type(ResultContentType.LLM_RESULT))
        with patch.object(voice,'social_profile',return_value={'voice':profile}),patch.object(voice.SessionServiceManager,'should_process_tts_request',new=AsyncMock(return_value=session)):
            await self.v.honor_delivery(e)
        return e

    async def test_auto_voice_is_one_clip_and_preserves_mentions_and_images(self):
        e=await self.deliver('今天有什么新发现')
        self.assertEqual(self.tts.get_audio.await_count,1);self.assertEqual(self.tts.get_audio.await_args.args[0],'第一句。 第二句。')
        self.assertEqual(sum(isinstance(x,Record) for x in e.get_result().chain),1)
        self.assertEqual(sum(isinstance(x,Image) for x in e.get_result().chain),1)
        self.assertIsInstance(e.get_result().chain[0],At);self.assertFalse(e.get_result().is_llm_result())
        await self.v.honor_delivery(e);self.assertEqual(self.tts.get_audio.await_count,1)

    async def test_short_ack_text_and_explicit_voice_override(self):
        e=await self.deliver('嗯嗯');self.tts.get_audio.assert_not_awaited()
        self.assertEqual(sum(isinstance(x,Plain) for x in e.get_result().chain),1)
        await self.deliver('请用语音说晚安');self.assertEqual(self.tts.get_audio.await_count,1)

    async def test_configured_probability_zero_and_text_preference_disable_auto_voice(self):
        await self.deliver('今天有什么新发现',probability=0);self.tts.get_audio.assert_not_awaited()
        await self.deliver('今天有什么新发现',profile='text');self.tts.get_audio.assert_not_awaited()

    async def test_synthesis_failure_keeps_single_text_without_native_retry(self):
        e=await self.deliver('今天有什么新发现',failure=True)
        self.assertEqual(self.tts.get_audio.await_count,1);self.assertFalse(e.get_result().is_llm_result())
        self.assertEqual(sum(isinstance(x,Record) for x in e.get_result().chain),0)
        self.assertEqual(sum(isinstance(x,Plain) for x in e.get_result().chain),1)

    async def test_other_bot_and_session_tts_off_keep_native_scope(self):
        for args in ({'platform':'other'},{'session':False}):
            e=await self.deliver('今天有什么新发现',**args);self.tts.get_audio.assert_not_awaited();self.assertTrue(e.get_result().is_llm_result())

    async def test_longer_breeze_budget_does_not_change_other_providers(self):
        original=voice.asyncio.wait_for;timeouts=[]
        async def wait(awaitable,timeout):
            timeouts.append(timeout);return await original(awaitable,timeout)
        with patch.object(voice.asyncio,'wait_for',side_effect=wait):
            await self.deliver('请用语音说今天的新发现')
            self.tts.synthesis_timeout=85
            await self.deliver('请用语音说今天的新发现')
            self.tts.synthesis_timeout=999
            await self.deliver('请用语音说今天的新发现')
        self.assertEqual(timeouts,[45,90,45])


if __name__=='__main__':unittest.main(verbosity=2)
