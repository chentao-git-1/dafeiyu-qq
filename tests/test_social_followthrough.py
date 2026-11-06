"""Verified-comment follow-through: model-selected timing, per-peer state and fake sends."""
import asyncio
import datetime as dt
import gc
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,Mock,patch

import test_cognition as base
from dafeiyu.dialogue.catalog import MemoryCatalog
from dafeiyu.initiative.social_intents import SocialIntentStore,SocialReply,source_key
from dafeiyu.initiative.social_policy import parse_decision,TZ
from dafeiyu.social.store import Store
from dafeiyu.runtime.outbound import OutboundStore
from data.plugins.dafeiyu_companion.social import SocialEngine
from data.plugins.dafeiyu_companion.main import Companion
from data.plugins.astrbot_plugin_qzone.core.auto_replies import AutoReplies
from data.plugins.astrbot_plugin_qzone.core.llm_action import LLMAction
from data.plugins.astrbot_plugin_qzone.core.db import PostDB
from data.plugins.astrbot_plugin_qzone.core.model import Post,Comment
from astrbot.core.provider.provider import Provider

PEER='7000000012';OTHER='7000000011';OWNER='7000000001';BOT='7000000002'
NOW=dt.datetime(2026,11,6,12,tzinfo=TZ).timestamp()
COMMENT='没找我 不和你玩了'


def source(now,peer=PEER,cid=1):
    return dict(peer=peer,post_tid='own-post',comment_id=cid,parent_id=0,post_at=now-3600,
        comment_at=now,post_text='自己的公开动态',comment=COMMENT)


def decision(now,action='contact',public='',body='我想来找你聊聊刚才那个念头。'):
    return dict(public_reply=public,action=action,intention='接续这个人的邀请',evidence=COMMENT,reason='结合实际邀请与当前交流',
        when=dt.datetime.fromtimestamp(now+3600,TZ).isoformat() if action=='later' else '',
        private_message=body if action=='contact' else '')


class DurableIntents(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.addCleanup(gc.collect)
        self.now=NOW;self.store=SocialIntentStore(Path(self.tmp.name)/'social.db',lambda:self.now);self.now+=2

    def ready(self,peer=PEER,cid=1):
        key=self.store.observe(source(self.now,peer,cid));epoch=self.store.peer(peer)['epoch']
        self.assertTrue(self.store.claim(key,epoch));self.assertTrue(self.store.decide(key,dict(decision(self.now),due=None),epoch))
        return key,epoch

    def test_old_comment_requires_explicit_selection_and_source_is_idempotent(self):
        old=source(self.now-7*86400)
        self.assertIsNone(self.store.observe(old));key=self.store.observe(old,requested=True)
        self.assertEqual(self.store.observe(old,requested=True),key);self.assertEqual(len(self.store.due()),1)
        self.assertEqual(self.store.get(key)['source']['comment'],COMMENT)

    def test_per_peer_waiting_does_not_block_another_contact(self):
        one,epoch=self.ready();self.assertTrue(self.store.begin_send(one,epoch));self.store.finish_send(one,'10001')
        two,epoch2=self.ready(OTHER,2);self.assertTrue(self.store.begin_send(two,epoch2))
        self.assertTrue(self.store.peer(PEER)['pending_since']);self.assertTrue(self.store.peer(OTHER)['pending_since'])
        self.now+=2;self.store.incoming(PEER,'answer',self.now)
        self.assertFalse(self.store.peer(PEER)['pending_since']);self.assertTrue(self.store.peer(OTHER)['pending_since'])

    def test_send_claim_is_unique_and_interruption_is_not_replayed(self):
        key,epoch=self.ready();self.assertTrue(self.store.begin_send(key,epoch));self.assertFalse(self.store.begin_send(key,epoch))
        SocialIntentStore(self.store.path,lambda:self.now)
        self.assertEqual(self.store.get(key)['status'],'sending')
        self.store.recover();self.assertEqual(self.store.get(key)['status'],'unknown');self.assertEqual(self.store.due(),[])

    def test_new_private_message_invalidates_generated_draft(self):
        key=self.store.observe(source(self.now));epoch=self.store.peer(PEER)['epoch'];self.store.claim(key,epoch)
        self.store.incoming(PEER,'new-input',self.now)
        self.assertFalse(self.store.decide(key,dict(decision(self.now),due=None),epoch))
        row=self.store.get(key);self.assertEqual(row['status'],'deferred');self.assertEqual(row['decision'],{})

    def test_generation_failure_keeps_source_and_has_bounded_retry(self):
        key=self.store.observe(source(self.now));epoch=self.store.peer(PEER)['epoch']
        self.store.claim(key,epoch);self.store.failed(key,'provider_error')
        self.assertEqual(self.store.due(),[]);self.assertEqual(self.store.get(key)['source']['comment'],COMMENT)
        self.now+=3601;self.assertTrue(self.store.claim(key,epoch));self.store.failed(key,'provider_error')
        self.now+=3601;self.assertEqual(self.store.get(key)['status'],'failed');self.assertEqual(self.store.due(),[])

    def test_forgetting_cancels_unsent_content_and_prevents_late_commit(self):
        key=self.store.observe(source(self.now));epoch=self.store.peer(PEER)['epoch'];self.store.claim(key,epoch)
        self.store.forget(PEER);self.assertFalse(self.store.decide(key,dict(decision(self.now),due=None),epoch))
        self.assertEqual(self.store.get(key)['status'],'cancelled');self.assertEqual(self.store.get(key)['decision'],{})

    def test_model_selected_time_and_exact_evidence_are_required(self):
        item=source(self.now);obj=decision(self.now,'later')
        parsed=parse_decision(json.dumps(obj),item,self.now,False);self.assertEqual(parsed['due'],self.now+3600)
        for changed in (dict(obj,when='2026-11-07T10:00:00'),dict(obj,evidence='别人的话'),
                dict(obj,target=OTHER),dict(obj,public_reply='旧留言不应再补发公开回评'),dict(obj,private_message='提前说好了')):
            with self.assertRaises(ValueError):parse_decision(json.dumps(changed),item,self.now,False)

    def test_private_body_is_removed_after_confirmed_delivery(self):
        key,epoch=self.ready();self.store.begin_send(key,epoch);self.store.finish_send(key,'10001')
        row=self.store.get(key);self.assertEqual(row['status'],'sent');self.assertEqual(row['message_id'],'10001')
        self.assertEqual(row['decision']['private_message'],'')


class SocialIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=NOW
        self.path=self.root/'plugin_data/astrbot_plugin_qzone/posts_5.db';self.path.parent.mkdir(parents=True)
        self.settings=Store(self.root/'plugin_data/dafeiyu_social/preferences.db',lambda:self.now)
        self.outgoing=OutboundStore(self.root/'plugin_data/dafeiyu_runtime/outgoing.db',lambda:self.now)
        self.provider=Mock(spec=Provider);self.provider.text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(decision(self.now))))
        self.context=NS(register_web_api=lambda *a:None,get_config=lambda *a:{'platform_settings':{
            'enable_id_white_list':True,'id_whitelist':[f'dafeiyu-qq:FriendMessage:{p}' for p in (PEER,OTHER,OWNER)]}},
            get_using_provider_async=AsyncMock(return_value=self.provider),get_provider_by_id=lambda *a:self.provider)
        self.plugin=NS(path=self.root/'plugin_data/dafeiyu_companion/state.json',context=self.context,
            family={'爸爸':OWNER,'田阿姨':PEER,'猛叔叔':OTHER})
        self.engine=SocialEngine(self.plugin,lambda:self.now);self.plugin.social_engine=self.engine
        self.controller=NS(catalog=lambda:MemoryCatalog(self.root),metered_provider=lambda provider,*a,**kw:provider)
        self.cfg=NS(db_path=self.path,context=self.context,llm=NS(reply_provider_id='fake'),client=object())
        self.db=PostDB(self.cfg);await self.db.initialize()
        self.plugins={'dafeiyu_dialogue':self.controller,'dafeiyu_natural_chat':NS(dialogue_character=lambda:dict(stable_profile='鲸鲸')),
            'astrbot_plugin_qzone':NS(cfg=self.cfg),'dafeiyu_companion':self.plugin,
            'astrbot_plugin_dafeiyu_balance':NS(watch=NS(status=lambda:dict(last_balance_cny='2.00',last_available=True,last_checked_at=self.now)))}
        self.context.get_registered_star=lambda name:NS(activated=True,star_cls=self.plugins[name]) if name in self.plugins else None
        self.now+=2
        self.comment=Comment(uin=int(PEER),nickname='爸爸（不可信昵称）',content=COMMENT,create_time=int(self.now),tid=1)
        self.post=Post(tid='own-post',uin=int(BOT),text='自己的公开动态',create_time=int(self.now)-3600,comments=[self.comment])
        await self.db.save(self.post)
        async def send(origin,chain):
            body=''.join(part.text for part in chain.chain)
            self.outgoing.record(origin.rsplit(':',1)[1],body,{'message_id':'10001'},'',at=self.now)
            return True
        self.context.send_message=AsyncMock(side_effect=send)

    async def asyncTearDown(self):
        gc.collect();self.tmp.cleanup()

    def row(self):return self.engine.store.get(source_key(self.post.tid,self.comment.tid,0,PEER))

    async def test_one_comment_generation_can_contact_its_author_without_review_or_second_generation(self):
        self.provider.text_chat.return_value=NS(completion_text=json.dumps(decision(self.now,public='接住你的邀请。')))
        body=await self.engine.handle_comment(self.post,self.comment,self.provider)
        self.assertIsInstance(body,SocialReply);self.assertEqual(body,'接住你的邀请。')
        self.context.send_message.assert_not_awaited()
        await self.engine.tick();await self.engine.tick()
        self.provider.text_chat.assert_awaited_once();self.context.send_message.assert_awaited_once()
        self.assertEqual(self.context.send_message.await_args.args[0],'dafeiyu-qq:FriendMessage:'+PEER)
        self.assertEqual(self.row()['status'],'sent');self.assertEqual(self.row()['message_id'],'10001')
        packet=json.loads(self.provider.text_chat.await_args.kwargs['prompt'])
        self.assertEqual(packet['target']['relationship'],'田阿姨')

    async def test_model_chooses_later_and_due_generation_uses_current_context(self):
        self.provider.text_chat.return_value=NS(completion_text=json.dumps(decision(self.now,'later')))
        await self.engine.handle_comment(self.post,self.comment,self.provider);await self.engine.tick()
        self.context.send_message.assert_not_awaited();self.assertEqual(self.row()['due'],self.now+3600)
        self.now+=3601;self.provider.text_chat.return_value=NS(completion_text=json.dumps(decision(self.now,body='现在想接着和你聊那件事。')))
        await self.engine.tick();self.assertEqual(self.provider.text_chat.await_count,2)
        self.assertEqual(self.row()['status'],'sent')
        self.assertFalse(json.loads(self.provider.text_chat.await_args.kwargs['prompt'])['allow_public_reply'])

    async def test_only_current_contact_private_history_is_offered(self):
        path=self.root/'plugin_data/dafeiyu_natural_chat/interaction.db';path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE exchanges(id,qq,origin,human,reply,at)')
            for i,peer in enumerate((PEER,OTHER)):
                db.execute('INSERT INTO exchanges VALUES (?,?,?,?,?,?)',(i,peer,'dafeiyu-qq:FriendMessage:'+peer,
                    '这个人的兴趣' if peer==PEER else '另一个人的秘密','实际回复',self.now-60))
        await self.engine.handle_comment(self.post,self.comment,self.provider)
        prompt=self.provider.text_chat.await_args.kwargs['prompt']
        self.assertIn('这个人的兴趣',prompt);self.assertNotIn('另一个人的秘密',prompt)

    async def test_unknown_send_is_not_a_success_and_never_repeats(self):
        self.context.send_message.side_effect=None;self.context.send_message.return_value=True
        await self.engine.handle_comment(self.post,self.comment,self.provider);await self.engine.tick();await self.engine.tick()
        self.assertEqual(self.row()['status'],'unknown');self.context.send_message.assert_awaited_once()
        self.engine.store.recover();await self.engine.tick();self.context.send_message.assert_awaited_once()

    async def test_permissions_and_quiet_time_are_checked_again_before_send(self):
        await self.engine.handle_comment(self.post,self.comment,self.provider)
        self.settings.set_profile(PEER,dict(quiet_start=11*60,quiet_end=13*60))
        await self.engine.tick();self.context.send_message.assert_not_awaited();self.provider.text_chat.assert_awaited_once()
        self.settings.set_profile(PEER,dict(proactive=False));self.now+=301;await self.engine.tick()
        self.assertEqual(self.row()['status'],'cancelled');self.context.send_message.assert_not_awaited()

    async def test_private_message_during_generation_invalidates_both_channel_drafts(self):
        async def generate(**kwargs):
            self.engine.store.incoming(PEER,'new-message',self.now)
            return NS(completion_text=json.dumps(decision(self.now,public='过期草稿')))
        self.provider.text_chat.side_effect=generate
        with self.assertRaises(RuntimeError):await self.engine.handle_comment(self.post,self.comment,self.provider)
        self.assertEqual(self.row()['status'],'deferred');self.context.send_message.assert_not_awaited()

    async def test_model_failure_keeps_a_retryable_intention_without_sending(self):
        self.provider.text_chat.side_effect=RuntimeError('fake failure')
        with self.assertRaises(RuntimeError):await self.engine.handle_comment(self.post,self.comment,self.provider)
        self.assertEqual(self.row()['status'],'pending');self.assertEqual(self.row()['source']['comment'],COMMENT)
        await self.engine.tick();self.provider.text_chat.assert_awaited_once();self.context.send_message.assert_not_awaited()

    async def test_no_new_comments_cause_no_model_calls(self):
        await self.engine.tick();await self.engine.tick();self.provider.text_chat.assert_not_awaited()

    async def test_another_confirmed_private_send_invalidates_unshipped_draft(self):
        await self.engine.handle_comment(self.post,self.comment,self.provider)
        self.outgoing.record(PEER,'另一条实际发出的私聊',{'message_id':'9000'},'',at=self.now)
        self.provider.text_chat.return_value=NS(completion_text=json.dumps(decision(self.now,'none')))
        await self.engine.tick()
        self.assertEqual(self.provider.text_chat.await_count,2);self.context.send_message.assert_not_awaited()
        self.assertEqual(self.row()['status'],'declined')

    async def test_forgetting_while_model_is_running_does_not_send_or_restore_private_draft(self):
        async def generate(**kwargs):
            self.engine.store.forget(PEER)
            return NS(completion_text=json.dumps(decision(self.now)))
        self.provider.text_chat.side_effect=generate
        with self.assertRaises(RuntimeError):await self.engine.handle_comment(self.post,self.comment,self.provider)
        await self.engine.tick();self.assertEqual(self.row()['decision'],{})
        self.assertEqual(self.row()['status'],'cancelled');self.context.send_message.assert_not_awaited()

    async def test_old_comment_is_only_processed_when_explicitly_selected(self):
        old=self.comment.model_copy(update={'create_time':int(self.now)-7*86400})
        self.assertIsNone(await self.engine.handle_comment(self.post,old,self.provider))
        selected=source(old.create_time);selected['post_tid']=self.post.tid
        key=self.engine.store.observe(selected,requested=True);self.assertIsNotNone(key)
        self.provider.text_chat.return_value=NS(completion_text=json.dumps(decision(self.now,'later')))
        await self.engine.tick()
        packet=json.loads(self.provider.text_chat.await_args.kwargs['prompt'])
        self.assertTrue(packet['owner_requested']);self.assertFalse(packet['allow_public_reply'])
        self.assertEqual(self.row()['status'],'deferred')

    async def test_deleted_source_cancels_without_calling_model(self):
        key=self.engine.store.observe(source(self.now))
        await self.db.delete(self.post.id);await self.engine.tick()
        self.assertEqual(self.engine.store.get(key)['status'],'cancelled');self.provider.text_chat.assert_not_awaited()

    async def test_native_comment_path_can_skip_public_reply_and_still_follow_through(self):
        llm=LLMAction(self.cfg)
        service=NS(session=NS(get_uin=AsyncMock(return_value=int(BOT))),query_feeds=AsyncMock(return_value=[self.post]),llm=llm,
            qzone=NS(get_detail=AsyncMock(return_value=NS(ok=True,data={})),reply=AsyncMock()))
        auto=AutoReplies(self.cfg,service)
        with patch('data.plugins.astrbot_plugin_qzone.core.auto_replies.time.time',return_value=self.now-60):
            await auto.start();await auto.terminate()
        with patch('data.plugins.astrbot_plugin_qzone.core.auto_replies.QzoneParser.parse_feeds',return_value=[self.post]):
            await auto.run_once()
        with sqlite3.connect(self.path) as db:
            state=db.execute('SELECT status FROM qzone_auto_reply_comments').fetchone()[0]
        self.assertEqual(state,'no_public_reply');service.qzone.reply.assert_not_awaited()
        await self.engine.tick();self.assertEqual(self.row()['status'],'sent');self.provider.text_chat.assert_awaited_once()

    async def test_native_incoming_contact_state_is_not_shared(self):
        event=base.fixture.event('我来啦',PEER)
        # Fixture timestamp is wall time; make it consistent with this test clock.
        event.message_obj.raw_message['time']=self.now
        plugin=NS(social_engine=self.engine,owner_message=lambda e:False)
        await Companion.initiative_incoming(plugin,event)
        self.assertGreater(self.engine.store.peer(PEER)['epoch'],0)
        self.assertEqual(self.engine.store.peer(OTHER)['epoch'],0)


if __name__=='__main__':unittest.main()
