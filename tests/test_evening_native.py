"""Real plugin entry points with fake transports, clocks and text providers."""
import datetime as dt
import hashlib
import importlib
import gc
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

STAGE = Path(sys.argv.pop(1)); sys.path.insert(0, str(STAGE))
import data.plugins
data.plugins.__path__ = [str(STAGE), *list(data.plugins.__path__)]
social = importlib.import_module('data.plugins.dafeiyu_social.main')
access = importlib.import_module('data.plugins.dafeiyu_social.access')
balance = importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_balance.main')
daily = importlib.import_module('data.plugins.astrbot_plugin_qzone.core.daily_publish')
editor = importlib.import_module('data.plugins.astrbot_plugin_qzone.core.editorial')
llm = importlib.import_module('data.plugins.astrbot_plugin_qzone.core.llm_action')
api = importlib.import_module('data.plugins.astrbot_plugin_qzone.core.qzone.api')
from data.plugins.astrbot_plugin_qzone.core.db import PostDB
from data.plugins.astrbot_plugin_qzone.core.model import Post
from data.plugins.dafeiyu_social.activity import ActivityStore, TZ, OWNER, SELF
from data.plugins.dafeiyu_runtime.delivery import append_receipt
from data.plugins.dafeiyu_life.life import LifeStore
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.message.components import Plain, At, Reply, Image
from astrbot.core.provider.provider import Provider


def event(text, uid='7000000011', group='', mid='1', parts=None):
    msg = AstrBotMessage(); msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    msg.self_id = SELF; msg.session_id = group or uid; msg.group_id = group; msg.message_id = mid
    msg.sender = MessageMember(uid, 'synthetic nickname'); msg.message_str = text
    msg.message = parts if parts is not None else [Plain(text)]
    import time
    msg.raw_message = {'post_type': 'message', 'self_id': SELF, 'user_id': uid, 'time': time.time()}
    result = AiocqhttpMessageEvent(text, msg, PlatformMetadata('aiocqhttp', 'test', 'dafeiyu-qq'), group or uid, object())
    result.send = AsyncMock(); return result


class Native(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.addCleanup(gc.collect)
        self.env = patch.dict(os.environ, {'ASTRBOT_ROOT': self.tmp.name}); self.env.start(); self.addCleanup(self.env.stop)
        access.database.cache_clear()
        self.data = Path(self.tmp.name)/'data'
        self.config = {'platform_settings': {'enable_id_white_list': True, 'id_whitelist': [
            '7000000101', *['dafeiyu-qq:FriendMessage:'+uid for uid in (OWNER, '7000000011', '7000000012', '7000000013')]]}}
        self.context = NS(get_config=lambda *a:self.config, register_web_api=lambda *a:None)
        self.social = social.DafeiyuSocial.__new__(social.DafeiyuSocial)
        self.social.context = self.context; self.social.db = access.store(); self.social.data = self.data

    async def test_receipts_count_only_real_human_replies_once(self):
        e = event('音乐很好听'); await self.social.activity_input(e)
        self.assertEqual(access.activity().ranked(), [])
        append_receipt(e, '等待', 'notice'); self.assertEqual(access.activity().ranked(), [])
        append_receipt(e, '回复一'); append_receipt(e, '回复二')
        self.assertEqual(access.activity().ranked()[0]['turns'], 1)
        replay = event('音乐很好听'); await self.social.activity_input(replay); append_receipt(replay, '重复')
        self.assertEqual(access.activity().ranked()[0]['turns'], 1)

    async def test_group_requires_actual_at_and_private_group_counts_merge(self):
        for i, parts in enumerate(([Plain('鲸鲸，音乐好听')], [Reply(id=3, sender_id=SELF), Plain('音乐好听')], [At(qq='7000000012'), Plain('音乐好听')])):
            e = event('音乐好听', group='7000000101', mid=str(i), parts=parts)
            await self.social.activity_input(e); append_receipt(e, '回复')
        self.assertEqual(access.activity().ranked(), [])
        e = event('音乐好听', group='7000000101', parts=[At(qq=SELF), Plain('音乐好听')])
        await self.social.activity_input(e); append_receipt(e, '回复')
        e = event('音乐好听'); await self.social.activity_input(e); append_receipt(e, '回复')
        self.assertEqual(access.activity().ranked()[0]['turns'], 2)

    async def test_cron_bot_notice_and_wrong_account_have_no_turns(self):
        for kind in ('cron', 'bot', 'notice', 'account'):
            e = event('音乐好听', mid=kind)
            if kind == 'cron': e.set_extra('cron_job', {'id':'synthetic'})
            if kind == 'bot': self.social.db.set_bot(OWNER, e.get_sender_id(), 'known bot')
            if kind == 'notice': e.message_obj.raw_message['post_type'] = 'notice'
            if kind == 'account': e.message_obj.raw_message['self_id'] = 12345
            await self.social.activity_input(e); append_receipt(e, '回复')
        self.assertEqual(access.activity().ranked(), [])

    async def test_setting_and_account_controls_are_not_normal_dialogue(self):
        for text in ('饭钱状态','聊天设置 文字','相处设置','/余额'):
            e=event(text,mid=text);await self.social.activity_input(e);append_receipt(e,'设置结果')
        self.assertEqual(access.activity().ranked(),[])

    async def test_privacy_control_changes_only_sender_and_keeps_proactive(self):
        for uid in ('7000000011', '7000000012'):
            e = event('音乐好听', uid=uid); await self.social.activity_input(e); append_receipt(e, '回复')
        e = event('以后别公开我的聊天', mid='deny'); await self.social.activity_input(e)
        self.assertTrue(e.is_stopped()); self.assertEqual(e.send.await_count, 1)
        self.assertTrue(self.social.db.profile('7000000011')['proactive'])
        self.assertEqual(len(access.activity().public_topics()), 1)

    async def test_ranked_balance_uses_friend_reachability_and_permissions(self):
        for uid, count in (('7000000011',3), ('7000000012',2), ('7000000013',1)):
            for i in range(count):
                e = event('音乐好听', uid=uid, mid=str(i)); await self.social.activity_input(e); append_receipt(e, '回复')
        self.social.db.set_profile('7000000011', {'proactive':False})
        bot = NS(get_friend_list=AsyncMock(return_value=[{'user_id':uid} for uid in ('7000000011','7000000012','7000000013')]),
                 get_status=AsyncMock(return_value={'online':True}), send_private_msg=AsyncMock(return_value={'message_id':123}))
        obj = balance.DafeiyuBalance(self.context)
        obj.context.get_platform = lambda *a:NS(bot=bot, meta=lambda:NS(id='dafeiyu-qq'))
        payload = {'is_available':True,'balance_infos':[{'currency':'CNY','total_balance':'0.60','topped_up_balance':'0.60','granted_balance':'0'}]}
        with patch.object(balance, 'deepseek_key', return_value='synthetic'), patch.object(balance, 'fetch_balance', new=AsyncMock(return_value=payload)), patch.object(obj.watch,'daytime',return_value=True):
            await obj.run_once(); await obj.run_once()
        sent = [str(c.kwargs['user_id']) for c in bot.send_private_msg.await_args_list]
        self.assertEqual(sent, [OWNER, '7000000012', '7000000013'])

    async def test_offline_does_not_reserve_or_record_success(self):
        bot = NS(get_friend_list=AsyncMock(return_value=[]),get_status=AsyncMock(return_value={'online':False}),send_private_msg=AsyncMock())
        obj = balance.DafeiyuBalance(self.context); obj.context.get_platform = lambda *a:NS(bot=bot,meta=lambda:NS(id='dafeiyu-qq'))
        payload = {'is_available':True,'balance_infos':[{'currency':'CNY','total_balance':'0.95','topped_up_balance':'0.95','granted_balance':'0'}]}
        with patch.object(balance,'deepseek_key',return_value='synthetic'),patch.object(balance,'fetch_balance',new=AsyncMock(return_value=payload)):
            await obj.run_once()
        self.assertEqual(obj.watch.state['cycle']['notices'], {}); bot.send_private_msg.assert_not_awaited()

    async def test_nighttime_quiet_period_does_not_freeze_empty_help_targets(self):
        e=event('音乐好听');await self.social.activity_input(e);append_receipt(e,'回复')
        self.social.db.set_profile('7000000011',{'quiet_start':23*60,'quiet_end':8*60})
        bot=NS(get_friend_list=AsyncMock(return_value=[{'user_id':'7000000011'}]),get_status=AsyncMock(return_value={'online':True}),send_private_msg=AsyncMock(return_value={'message_id':123}))
        obj=balance.DafeiyuBalance(self.context);obj.context.get_platform=lambda *a:NS(bot=bot,meta=lambda:NS(id='dafeiyu-qq'))
        import time
        now=dt.datetime.fromtimestamp(time.time(),TZ).replace(hour=23,minute=30,second=0,microsecond=0)
        clock=[now.timestamp()];obj.watch.clock=lambda:clock[0]
        payload={'is_available':True,'balance_infos':[{'currency':'CNY','total_balance':'0.60','topped_up_balance':'0.60','granted_balance':'0'}]}
        with patch.object(balance,'deepseek_key',return_value='synthetic'),patch.object(balance,'fetch_balance',new=AsyncMock(return_value=payload)):
            await obj.run_once()
            self.assertEqual([t['qq'] for t in obj.watch.state['cycle']['targets']],['7000000011'])
            self.assertEqual(bot.send_private_msg.await_count,1)
            clock[0]=(now+dt.timedelta(days=1)).replace(hour=9,minute=0).timestamp()
            await obj.run_once()
        self.assertEqual(sum(str(c.kwargs['user_id'])=='7000000011' for c in bot.send_private_msg.await_args_list),1)


class Diary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.addCleanup(gc.collect)
        self.data = Path(self.tmp.name)/'data'; self.path = self.data/'plugin_data/astrbot_plugin_qzone/posts_5.db'
        self.path.parent.mkdir(parents=True)
        self.now = dt.datetime(2026,10,28,21,30,tzinfo=TZ)
        self.cfg = NS(db_path=self.path,context=NS(get_registered_star=lambda *a:None),trigger=NS(send_admin=False),client=None)
        await PostDB(self.cfg).initialize()
        self.editorial = editor.EditorialPolicy(self.path, self.cfg.context)
        await self.editorial.initialize()
        self.text = '今天把一个小念头慢慢想明白，普通的旋律里也藏着许多有趣的变化。'
        self.outbound = []
        async def publish(**kwargs):
            if kwargs.get('before_publish'): await kwargs['before_publish']()
            self.outbound.append(kwargs)
            return NS(tid='synthetic-id',text=kwargs['text'])
        self.service = NS(llm=NS(editorial=self.editorial, generate_post=AsyncMock(return_value=self.text)),publish_post=AsyncMock(side_effect=publish))
        self.publisher = daily.DailyRandomPublisher(self.cfg,self.service,NS(send_admin_post=AsyncMock()))
        self.publisher.scheduler = Mock(running=False)
        self.publisher._now = lambda:self.now
        self.images = patch.object(daily, 'prepare_post_images', new=AsyncMock(return_value=[])); self.image_mock=self.images.start(); self.addCleanup(self.images.stop)
        await self.candidate()

    async def candidate(self, topics=None):
        await self.editorial.remember_candidate(self.text, {'diary':True,'day':'2026-10-28','kind':'晚间日记','frame':'桌面静物','chat_topics':topics or []})

    def row(self, day='2026-10-28'):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT run_at,status,text FROM daily_publish_runs WHERE day=?',(day,)).fetchone()

    async def due(self):
        await self.publisher.start()
        self.now = dt.datetime.fromtimestamp(self.row()[0], TZ)

    async def test_persistent_evening_schedule_and_one_publication(self):
        await self.due(); first=self.row()[0]
        self.assertTrue(dt.time(21,30) <= self.now.time() < dt.time(22,30))
        await self.publisher._schedule_next(); self.assertEqual(self.row()[0],first)
        await self.publisher._run_task('2026-10-28'); await self.publisher._run_task('2026-10-28')
        self.assertEqual(len(self.outbound),1); self.assertEqual(self.row()[1],'published')

    async def test_old_unsent_schedule_migrates_and_terminal_states_stay(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE daily_publish_runs(day TEXT PRIMARY KEY,run_at INTEGER,status TEXT,text TEXT DEFAULT "",tid TEXT,error_type TEXT,updated_at INTEGER)')
            db.execute('INSERT INTO daily_publish_runs(day,run_at,status,updated_at) VALUES (?,?,?,?)', ('2026-10-28',self.now.replace(hour=15).timestamp(),'scheduled',0))
            db.execute('INSERT INTO daily_publish_runs(day,run_at,status,text,updated_at) VALUES (?,?,?,?,?)', ('2026-10-29',123,'unknown','keep',0))
        await self.due()
        self.assertTrue(21 <= self.now.hour <= 22)
        self.assertEqual(self.row('2026-10-29'),(123,'unknown','keep'))

    async def test_after_window_skips_day_and_does_not_publish_late(self):
        self.now = self.now.replace(hour=23)
        await self.publisher.start()
        self.assertIsNone(self.row())
        self.assertEqual(self.publisher.scheduler.add_job.call_args.kwargs['kwargs']['day'],'2026-10-29')

    async def test_revocation_during_image_preparation_cancels_publication(self):
        store = ActivityStore(self.data/'plugin_data/dafeiyu_social/preferences.db',lambda:self.now.timestamp())
        source = store.prepare('7000000011','private','1',self.now.timestamp(),'音乐很好听'); store.record_confirmed(source)
        await self.candidate(store.public_topics())
        async def revoke(*a,**kw):
            store.prepare('7000000011','private','2',self.now.timestamp(),'以后别公开我的聊天')
            store.purge_drafts(self.path)
            return []
        self.image_mock.side_effect = revoke
        await self.due(); await self.publisher._run_task('2026-10-28')
        self.assertEqual(self.outbound,[]); self.assertEqual(self.row()[1:3],('cancelled',''))

    async def test_unknown_post_result_is_not_retried(self):
        self.service.publish_post.side_effect = TimeoutError()
        await self.due(); await self.publisher._run_task('2026-10-28'); await self.publisher._run_task('2026-10-28')
        self.assertEqual(self.service.publish_post.await_count,1); self.assertEqual(self.row()[1],'unknown')

    async def test_image_generation_failure_falls_back_once(self):
        self.image_mock.side_effect = RuntimeError('synthetic generation failure')
        await self.due(); await self.publisher._run_task('2026-10-28')
        self.assertEqual(len(self.outbound),1); self.assertEqual(self.outbound[0]['images'],[])

    async def test_known_image_upload_failure_has_one_text_fallback(self):
        self.image_mock.return_value = ['synthetic-image']
        self.service.publish_post.side_effect = [daily.ImagePreparationFailed(),NS(tid='plain-id')]
        await self.due(); await self.publisher._run_task('2026-10-28')
        self.assertEqual(self.service.publish_post.await_count,2)
        self.assertEqual(self.service.publish_post.await_args_list[1].kwargs['images'],[])

    async def test_final_transport_guard_runs_after_upload_before_post_request(self):
        obj = api.QzoneAPI.__new__(api.QzoneAPI)
        obj.session = NS(get_ctx=AsyncMock(return_value=NS(uin=int(SELF),gtk2=123)))
        obj.request = AsyncMock()
        obj._upload_image = AsyncMock(return_value=NS(ok=True,data={}))
        guard = AsyncMock(side_effect=daily.DiaryCancelled('revoked during upload'))
        post = Post(uin=int(SELF),name='鲸鲸',text=self.text,images=['synthetic'])
        with patch.object(api,'normalize_images',new=AsyncMock(return_value=[b'image'])), patch.object(api.QzoneParser,'parse_upload_result',return_value=('bo','rich')):
            with self.assertRaises(daily.DiaryCancelled): await obj.publish(post,before_publish=guard)
        obj._upload_image.assert_awaited_once(); obj.request.assert_not_awaited()

    async def test_writer_rejects_unfounded_or_private_output_without_public_write(self):
        obj = llm.LLMAction.__new__(llm.LLMAction); obj.editorial=self.editorial
        obj.cfg=NS(llm=NS(post_provider_id='fake',post_prompt='写简短动态'),client=object())
        provider=Mock(spec=Provider);provider.text_chat=AsyncMock(return_value=NS(completion_text='爸爸今天带我出门去见朋友了，真开心。'))
        obj._get_provider=lambda *a:provider
        with self.assertRaises(ValueError): await obj.generate_post(diary=True,now=self.now.timestamp())
        self.assertEqual(provider.text_chat.await_count,2)

    async def test_review_uncertainty_rejects_otherwise_innocent_text(self):
        obj=llm.LLMAction.__new__(llm.LLMAction);obj.editorial=self.editorial
        obj.cfg=NS(llm=NS(post_provider_id='fake',post_prompt='写简短动态'),client=object())
        provider=Mock(spec=Provider);provider.text_chat=AsyncMock(side_effect=[NS(completion_text=self.text),NS(completion_text='{"ok":false}')]*2)
        obj._get_provider=lambda *a:provider
        with self.assertRaises(ValueError): await obj.generate_post(diary=True,now=self.now.timestamp())
        self.assertEqual(provider.text_chat.await_count,4)

    async def test_window_expiry_during_images_has_zero_post_writes(self):
        async def delayed(*a,**kw): self.now=self.now.replace(hour=22,minute=30); return []
        self.image_mock.side_effect=delayed
        await self.due(); await self.publisher._run_task('2026-10-28')
        self.assertEqual(self.row()[1],'expired'); self.service.publish_post.assert_not_awaited()

    async def test_existing_successful_summary_skips_diary(self):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO qzone_editorial_posts VALUES (?,?,?,?)',('already','k',json.dumps({'kind':'当天小记'}),self.now.timestamp()))
        await self.due(); await self.publisher._run_task('2026-10-28')
        self.assertEqual(self.row()[1],'skipped'); self.service.llm.generate_post.assert_not_awaited()

    async def test_writer_receives_safe_topics_without_persona_or_private_words(self):
        store = ActivityStore(self.data/'plugin_data/dafeiyu_social/preferences.db',lambda:self.now.timestamp())
        source = store.prepare('7000000011','private','1',self.now.timestamp(),'音乐里的旋律让我想到紫丁香');store.record_confirmed(source)
        obj = llm.LLMAction.__new__(llm.LLMAction);obj.editorial=self.editorial
        obj.cfg=NS(llm=NS(post_provider_id='fake',post_prompt='写简短动态'),client=object())
        provider = Mock(spec=Provider);provider.text_chat=AsyncMock(side_effect=[NS(completion_text=self.text),NS(completion_text='{"ok":true}')])
        obj._get_provider=lambda *a:provider;obj._build_request_context=AsyncMock(side_effect=AssertionError('full persona must not enter diary'))
        result = await obj.generate_post(diary=True,now=self.now.timestamp())
        self.assertEqual(result,self.text);self.assertEqual(provider.text_chat.await_count,2)
        payload=str(provider.text_chat.await_args_list)
        self.assertNotIn('紫丁香',payload);self.assertNotIn('7000000011',payload)
        self.assertEqual(provider.text_chat.await_args_list[0].kwargs['contexts'],[])

    async def test_primary_artifact_uses_actual_completion_date_and_integrity(self):
        store=LifeStore(self.data/'plugin_data/dafeiyu_life');works=store.root/'works';works.mkdir()
        path=works/'test.jpg';path.write_bytes(b'synthetic-artifact')
        record={'id':'own','day':'2026-10-27','kind':'drawing','note':'数字插画作品','thought':'一个小想法','story':'在故事里的小屋',
                'project':'好奇手帐','stage':1,'source':{},'completed_at':self.now.timestamp(),'artifact':'works/test.jpg','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        with store.db() as db: db.execute('INSERT INTO runs(day,run_at,status,kind,record,updated) VALUES (?,?,?,?,?,?)',('2026-10-27',0,'completed','drawing',json.dumps(record),0))
        self.assertEqual(store.public_context_for_day(self.now.timestamp())['day'],'2026-10-28')
        self.assertEqual(store.public_image_for_day(self.now.timestamp()),str(path))
        self.assertIsNone(store.public_context_for_day(self.now.timestamp()+86400))
        path.write_bytes(b'tampered');self.assertIsNone(store.public_context_for_day(self.now.timestamp()))


if __name__ == '__main__': unittest.main(verbosity=2)
