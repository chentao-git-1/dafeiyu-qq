"""Real plugin boundaries with fake providers, filesystem sandboxes and captured sends."""
import asyncio
import base64
import importlib
import gc
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

STAGE=Path(sys.argv.pop(1));sys.path.insert(0,str(STAGE))
import data.plugins
data.plugins.__path__=[str(STAGE),*list(data.plugins.__path__)]
n=importlib.import_module('data.plugins.dafeiyu_natural_chat.main')
l=importlib.import_module('data.plugins.dafeiyu_life.main')
q=importlib.import_module('data.plugins.astrbot_plugin_qzone.main')
ql=importlib.import_module('data.plugins.astrbot_plugin_qzone.core.llm_action')
pi=importlib.import_module('data.plugins.astrbot_plugin_qzone.core.post_images')
d=importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_draw.main')
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage,MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.message.components import Plain,Image,At
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.provider.provider import Provider
from astrbot.core.agent.tool import ToolSet

OWNER='7000000001';FRIEND='7000000012'
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6KwAAAABJRU5ErkJggg==')

def event(text='',uid=OWNER,group='',platform='dafeiyu-qq',parts=None):
    msg=AstrBotMessage();msg.type=MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    msg.self_id='7000000002';msg.session_id=group or uid;msg.group_id=group;msg.message_id='native-1'
    msg.sender=MessageMember(uid,'爸爸' if uid==OWNER else '朋友');msg.message=parts if parts is not None else [Plain(text)];msg.message_str=text
    msg.raw_message={'post_type':'message','self_id':7000000002,'user_id':uid,'time':time.time()}
    e=AiocqhttpMessageEvent(text,msg,PlatformMetadata('aiocqhttp','test',platform),group or uid,object())
    e.send=AsyncMock(return_value=None)
    return e


class Native(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.addCleanup(gc.collect)  # Release SQLite cycles before Windows removes the sandbox.
        self.config={'platform_settings':{'enable_id_white_list':True,'id_whitelist':['7000000101','7000000102','dafeiyu-qq:FriendMessage:'+OWNER,'dafeiyu-qq:FriendMessage:'+FRIEND]}}
        self.context=NS(get_config=lambda *args:self.config,register_web_api=lambda *args:None)

    def publisher(self):
        obj=q.QzonePlugin.__new__(q.QzonePlugin);obj.context=self.context;obj.cfg=NS(client=None)
        obj.llm=NS(generate_post=AsyncMock(return_value='今天把一个小念头慢慢想明白。'),editorial=object())
        async def fake_publish(**kwargs):return NS(tid='synthetic-id',text=kwargs['text'])
        obj.service=NS(publish_post=AsyncMock(side_effect=fake_publish))
        obj.db=NS(save=AsyncMock())
        return obj

    async def test_autonomous_post_drops_model_supplied_private_content(self):
        obj=self.publisher();e=event('发条空间吧，内容随意')
        with patch.object(q,'prepare_post_images',new=AsyncMock(return_value=[])) as images:
            text=await obj.dafeiyu_qzone_publish(e,text='PRIVATE FAMILY HISTORY',topic='PRIVATE',image_prompt='PRIVATE IMAGE')
        self.assertIn('已发布',text);self.assertEqual(obj.service.publish_post.await_args.kwargs['text'],'今天把一个小念头慢慢想明白。')
        self.assertEqual(obj.llm.generate_post.await_args.kwargs['topic'],'')
        self.assertIsNone(images.await_args.args[2]);self.assertEqual(images.await_args.args[4],'')
        self.assertEqual(obj.service.publish_post.await_count,1)

    async def test_negated_quote_is_not_authorized_as_public_body(self):
        obj=self.publisher();e=event('不要用“爸爸今天工作累了”，自己挑一条发')
        with patch.object(q,'prepare_post_images',new=AsyncMock(return_value=[])):
            await obj.dafeiyu_qzone_publish(e,text='爸爸今天工作累了')
        self.assertEqual(obj.llm.generate_post.await_count,1)
        self.assertEqual(obj.service.publish_post.await_args.kwargs['text'],'今天把一个小念头慢慢想明白。')

    async def test_explicit_body_remains_verbatim_and_does_not_ask_again(self):
        obj=self.publisher();e=event('正文：今天的风很舒服，原文发空间。')
        with patch.object(q,'prepare_post_images',new=AsyncMock(return_value=[])):
            text=await obj.dafeiyu_qzone_publish(e,text='今天的风很舒服。',with_image=False)
        self.assertIn('已发布',text);obj.llm.generate_post.assert_not_awaited()
        self.assertEqual(obj.service.publish_post.await_args.kwargs['text'],'今天的风很舒服。')

    async def test_self_chosen_words_keep_explicitly_requested_photo(self):
        obj=self.publisher();e=event('自己挑内容，配上我刚发的照片')
        with patch.object(q,'prepare_post_images',new=AsyncMock(return_value=['synthetic-photo'])) as images:
            await obj.dafeiyu_qzone_publish(e,text='PRIVATE OLD CHAT')
        self.assertIs(images.await_args.args[2],e);self.assertTrue(images.await_args.kwargs['require_supplied'])
        self.assertEqual(obj.llm.generate_post.await_count,1)

    async def test_plain_text_request_overrides_default_image_flag(self):
        obj=self.publisher();e=event('自己挑一条，纯文字发')
        with patch.object(q,'prepare_post_images',new=AsyncMock(return_value=[])) as images:
            await obj.dafeiyu_qzone_publish(e,text='PRIVATE',with_image=True)
        self.assertFalse(images.await_args.args[3]);self.assertIsNone(images.await_args.args[2])
        e2=event('纯文字，还是配这张照片')
        with patch.object(q,'prepare_post_images',new=AsyncMock(return_value=['photo'])) as images2:
            await obj.dafeiyu_qzone_publish(e2,text='ignored',with_image=False)
        self.assertTrue(images2.await_args.args[3]);self.assertIs(images2.await_args.args[2],e2)

    async def test_explicit_body_keeps_same_message_attachments_unless_refused(self):
        obj=self.publisher();e=event('把这段正文发空间',parts=[Plain('把这段正文发空间'),Image.fromURL('https://example.invalid/picture.png')])
        with patch.object(q,'prepare_post_images',new=AsyncMock(return_value=['picture'])) as images:
            await obj.dafeiyu_qzone_publish(e,text='主人指定的正文')
        self.assertIs(images.await_args.args[2],e)
        e2=event('把这段正文发空间，不要用这张照片')
        with patch.object(q,'prepare_post_images',new=AsyncMock(return_value=[])) as images2:
            await obj.dafeiyu_qzone_publish(e2,text='主人指定的正文')
        self.assertIsNone(images2.await_args.args[2])

    async def test_scope_denials_still_have_zero_public_writes(self):
        obj=self.publisher()
        for e in (event('发空间',uid=FRIEND),event('发空间',group='999999999'),event('发空间',platform='other-bot')):
            self.assertIn('仅限',await obj.dafeiyu_qzone_publish(e,text='不应发出'))
        obj.service.publish_post.assert_not_awaited();obj.llm.generate_post.assert_not_awaited()

    async def test_owner_whitelist_groups_and_dedupe_still_work(self):
        obj=self.publisher()
        with patch.object(q,'prepare_post_images',new=AsyncMock(return_value=[])):
            for group in ('','7000000101','7000000102'):
                e=event('正文：今天的小心情。',group=group)
                self.assertIn('已发布',await obj.dafeiyu_qzone_publish(e,text='今天的小心情。',with_image=False))
                self.assertIn('已尝试',await obj.dafeiyu_qzone_publish(e,text='今天的小心情。',with_image=False))
        self.assertEqual(obj.service.publish_post.await_count,3)

    async def test_missing_selected_photo_does_not_generate_substitute_or_publish(self):
        obj=self.publisher();e=event('自己挑内容，配这张照片')
        with patch.object(pi,'get_image_urls',new=AsyncMock(return_value=[])),patch.object(q,'prepare_post_images',new=pi.prepare_post_images):
            result=await obj.dafeiyu_qzone_publish(e,text='ignored')
        self.assertIn('尚未发布',result);obj.service.publish_post.assert_not_awaited()

    async def test_actual_attachment_is_returned_without_generation(self):
        e=event('用这张照片',parts=[Plain('用这张照片'),Image.fromURL('https://example.invalid/picture.png')])
        context=NS(get_registered_star=Mock(side_effect=AssertionError('must not generate')))
        images=await pi.prepare_post_images(context,'公开说明',e,require_supplied=True)
        self.assertEqual(images,['https://example.invalid/picture.png'])

    async def test_public_writer_never_fetches_private_group_history(self):
        obj=ql.LLMAction.__new__(ql.LLMAction)
        provider=Mock(spec=Provider);provider.text_chat=AsyncMock(return_value=NS(completion_text='今天把一个小念头慢慢想明白。'))
        obj._get_provider=lambda *args:provider
        obj.cfg=NS(llm=NS(post_provider_id='fake',post_prompt='写一条公开短动态'),source=NS(post_max_msg=100),client=NS(get_group_list=AsyncMock(side_effect=AssertionError('private history'))))
        obj._get_persona_context=AsyncMock(return_value=('普通人格',[{'role':'user','content':'PRIVATE SEED DIALOGUE'}]))
        obj.editorial=NS(choose_plan=AsyncMock(return_value=({'kind':'小想法'},[])),writing_prompt=lambda *args:'公开素材：无具体活动。',remember_candidate=AsyncMock())
        result=await obj.generate_post(topic='PRIVATE MODEL TOPIC',event=event('自己挑吧'))
        self.assertTrue(result);obj.editorial.choose_plan.assert_awaited_once_with(None)
        sent=provider.text_chat.await_args.kwargs
        self.assertEqual(sent['contexts'],[]);self.assertNotIn('PRIVATE',sent['system_prompt'])
        obj.cfg.client.get_group_list.assert_not_awaited()

    def life(self,record=None):
        obj=l.JingjingLife.__new__(l.JingjingLife);obj.actor=lambda e:e.get_sender_id();obj.public_context=lambda:record
        return obj

    async def test_daily_chat_does_not_receive_story_and_can_remove_stale_block(self):
        life=self.life();req=ProviderRequest(system_prompt='base\n<!-- BEGIN_JINGJING_LIFE -->old story<!-- END_JINGJING_LIFE -->')
        e=event('我醒了');await life.life_context(e,req)
        self.assertEqual(req.system_prompt,'base');self.assertFalse(e.get_extra('jingjing_own_life_context'))

    async def test_repeated_questions_and_refusal_have_final_turn_constraint(self):
        with patch.object(n,'get_astrbot_data_path',return_value=self.tmp.name):
            natural=n.NaturalChat(self.context)
            for human,tail,expected in [('我醒了','睡一会儿吗？\n睡踏实了吗？',True),('我先忙，别问了','',True),('帮我算一下','',False)]:
                e=event(human);e.set_extra('jingjing_style_prepared',True);e.set_extra('jingjing_assistant_tail',tail)
                req=ProviderRequest(system_prompt='base');await natural.prepare_spoken_style(e,req)
                self.assertEqual('【这轮收尾】' in req.system_prompt,expected)
                if expected:self.assertIn('确实缺少必要信息',req.system_prompt)

    async def test_short_wake_up_delivery_closes_only_idle_question_tail(self):
        with patch.object(n,'get_astrbot_data_path',return_value=self.tmp.name):
            natural=n.NaturalChat(self.context)
            for human,tool_used,expected in [('我醒了',False,'醒啦。'),('我醒了',True,'醒啦，脑袋还迷迷糊糊的吗？'),('我醒了，能帮我查提醒吗',False,'醒啦，脑袋还迷迷糊糊的吗？')]:
                e=event(human);e.set_extra('jingjing_style_prepared',True);e.set_extra('jingjing_assistant_tail','要睡一会儿吗？\n睡踏实了吗？');e.set_extra('jingjing_any_tool',tool_used)
                response=NS(completion_text='醒啦，脑袋还迷迷糊糊的吗？',tools_call_name=[])
                await natural.conversational_response(e,response);self.assertEqual(response.completion_text,expected)

    async def test_own_world_survives_without_recent_activity_but_not_as_shared_memory(self):
        life=self.life();e=event('你的小屋窗台盆栽叫什么？');req=ProviderRequest(system_prompt='base')
        await life.life_context(e,req)
        self.assertIn('小芽',req.system_prompt);self.assertIn('没有可核验',req.system_prompt)
        with patch.object(n,'get_astrbot_data_path',return_value=self.tmp.name):
            natural=n.NaturalChat(self.context)
            e=event('还记得你的小屋窗台植物叫什么吗？');req=ProviderRequest(system_prompt='base')
            await life.life_context(e,req);await natural.natural_delivery_context(e,req)
            self.assertFalse(e.get_extra('jingjing_unsupported_recall'))
            e2=event('你还记得上次给我的手帐建议吗？');req2=ProviderRequest(system_prompt='base')
            await life.life_context(e2,req2);await natural.natural_delivery_context(e2,req2)
            self.assertTrue(e2.get_extra('jingjing_unsupported_recall'))

    async def test_real_reading_question_has_completed_record_and_origin(self):
        record={'id':'life:test','kind':'reading','day':'2026-10-27','note':'读了一篇关于潮汐的文章','thought':'引力的关系很有趣','story':'在我的小屋故事里翻了一页','project':'好奇手帐','stage':1,'source':{'url':'https://example.invalid/article'},'completed_at':time.time()}
        life=self.life(record);req=ProviderRequest(system_prompt='base');await life.life_context(event('你在读什么文章啊'),req)
        self.assertIn('潮汐',req.system_prompt);self.assertIn('example.invalid/article',req.system_prompt)

    def drawing(self):
        obj=d.DafeiyuDraw.__new__(d.DafeiyuDraw);obj.context=self.context;obj.config={'enabled':True,'api_key':'synthetic-no-key','cooldown_seconds':0};obj.lock=asyncio.Lock();obj.last={}
        obj.image_dir=Path(self.tmp.name);obj.intent_context=d.IntentContext();return obj

    async def test_504_is_natural_now_and_technical_in_next_same_session(self):
        obj=self.drawing();e=event('画一只蓝鲸')
        error=d.DrawingError('画图接口暂时失败（HTTP 504），没有自动重试。',http_status=504)
        with patch.object(d,'generate',new=AsyncMock(side_effect=error)) as generate:
            result=await obj._draw(e,'一只蓝鲸');self.assertNotIn('HTTP',result);self.assertIn('没拿到图片结果',result)
            later=event('刚才HTTP状态码是多少？');req=ProviderRequest(system_prompt='base',func_tool=ToolSet())
            await obj.prepare_drawing(later,req)
            self.assertIn('504',req.system_prompt);self.assertIn('此前错误',req.system_prompt)
            other=event('刚才HTTP是多少？',uid=FRIEND);other_req=ProviderRequest(system_prompt='base',func_tool=ToolSet());await obj.prepare_drawing(other,other_req)
            self.assertNotIn('504',other_req.system_prompt);self.assertEqual(generate.await_count,1)

    async def test_429_retains_structured_code_without_blaming_unknown_cause(self):
        obj=self.drawing();e=event('画一只蓝鲸')
        with patch.object(d,'generate',new=AsyncMock(side_effect=d.DrawingError('画图服务繁忙，稍后再试。',http_status=429))):
            result=await obj._draw(e,'一只蓝鲸')
        self.assertIn('忙',result);self.assertEqual(obj._error_context().get(e)['http_status'],429)

    async def test_generated_but_unconfirmed_send_has_one_generation(self):
        obj=self.drawing();e=event('画一只蓝鲸');attempts=[]
        async def send(message):
            if any(isinstance(part,Image) for part in message.chain):attempts.append(message);raise TimeoutError()
        e.send=send
        with patch.object(d,'generate',new=AsyncMock(return_value=(PNG,'.png',(1,1)))) as generate:
            result=await obj._draw(e,'一只蓝鲸')
        self.assertIn('图片已生成',result);self.assertIn('发送没确认',result);self.assertEqual(len(attempts),1);self.assertEqual(generate.await_count,1)

    async def test_failed_progress_has_not_started_generation(self):
        obj=self.drawing();e=event('画一只蓝鲸');e.send=AsyncMock(side_effect=RuntimeError('synthetic-send'))
        with patch.object(d,'generate',new=AsyncMock()) as generate:result=await obj._draw(e,'一只蓝鲸')
        self.assertIn('画图还没开始',result);generate.assert_not_awaited()

    async def test_real_http_client_keeps_429_metadata_without_network(self):
        drawing=importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_draw.drawing')
        class Response:
            status=429
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
        class Session:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            def post(self,*args,**kwargs):return Response()
        with patch.object(drawing.aiohttp,'ClientSession',return_value=Session()),patch.object(drawing.aiohttp,'TCPConnector',return_value=object()),patch.object(drawing,'PublicResolver',return_value=object()):
            with self.assertRaises(drawing.DrawingError) as caught:
                await drawing.generate('synthetic-no-key','一只小鲸鱼')
        self.assertEqual(caught.exception.http_status,429)

    async def test_download_timeout_is_not_relabelled_as_generation_failure(self):
        drawing=importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_draw.drawing')
        class PostResponse:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
        class DownloadResponse:
            async def __aenter__(self):raise asyncio.TimeoutError()
            async def __aexit__(self,*args):return False
        class Session:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            def post(self,*args,**kwargs):return PostResponse()
            def get(self,*args,**kwargs):return DownloadResponse()
        with patch.object(drawing.aiohttp,'ClientSession',return_value=Session()),patch.object(drawing.aiohttp,'TCPConnector',return_value=object()),patch.object(drawing,'PublicResolver',return_value=object()),patch.object(drawing,'bounded_body',new=AsyncMock(return_value=b'{"data":[{"url":"https://example.invalid/complete.png"}]}')):
            with self.assertRaises(drawing.DrawingError) as caught:await drawing.generate('synthetic-no-key','a whale')
        self.assertEqual(caught.exception.stage,'download')
        self.assertIn('图片已经生成',d.drawing_error_text(caught.exception,'画一张'))

if __name__=='__main__':unittest.main(verbosity=2)
