"""Conversation/publication bridge with SQLite and mocked generations; no network."""
import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import test_cognition as base
from dafeiyu.dialogue.catalog import MemoryCatalog
from dafeiyu.dialogue.contracts import MemoryItem,TurnContext
from dafeiyu.dialogue.context_builder import compose,automatic_items
from dafeiyu.dialogue.generation import parse_generation
from dafeiyu.dialogue.indexes import SourceIndex
from dafeiyu.social.activity import ActivityStore
from dafeiyu.social.inspiration import sharing_proposal
from data.plugins.astrbot_plugin_qzone.core.editorial import EditorialPolicy

OWNER,ORIGIN=base.OWNER,base.ORIGIN
BOT='7000000002'
HUMAN='城市的老招牌为什么让人觉得亲切？'
IDEA='旧招牌的手写字与城市记忆之间的关系'


def sharing(text=IDEA,evidence=HUMAN):
    return dict(privacy=dict(action='none'),ideas=[dict(summary=text,evidence=evidence,scope='general_topic')])


class Inspiration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.addCleanup(gc.collect)
        self.root=Path(self.tmp.name);self.now=time.time();self.clock=lambda:self.now
        self.store=ActivityStore(self.root/'preferences.db',self.clock)

    def confirmed(self,human=HUMAN,mid='1',**kwargs):
        source=self.store.prepare(OWNER,ORIGIN,mid,self.now,human,**kwargs)
        self.store.record_confirmed(source)
        return source

    def ideas(self,text=IDEA,evidence=HUMAN):return sharing_proposal(sharing(text,evidence),evidence)[1]

    def test_arbitrary_topic_is_specific_without_old_keyword_categories(self):
        source=self.confirmed();self.assertIsNone(source['topic'])
        self.assertEqual(self.store.record_public_ideas(source,self.ideas()),1)
        self.assertEqual(self.store.record_public_ideas(source,self.ideas()),0)
        topics=self.store.public_topics();self.assertEqual([r['summary'] for r in topics],[IDEA])
        self.assertEqual(set(topics[0]),{'key','date','summary'});self.assertTrue(self.store.topics_valid(topics))
        with self.store.connect() as db:
            record=dict(db.execute('SELECT * FROM public_ideas').fetchone())
        self.assertNotIn(HUMAN,json.dumps(record,ensure_ascii=False));self.assertEqual(len(record['evidence_hash']),64)

    def test_sensitive_input_does_not_block_independent_public_abstraction(self):
        human='我今天去医院，候诊时想起城市路牌的字体为什么这么难看'
        source=self.confirmed(human);self.store=ActivityStore(self.store.path,self.clock)
        self.assertEqual(self.store.record_public_ideas(source,self.ideas(evidence=human)),1)
        self.assertNotIn('医院',self.store.public_topics()[0]['summary'])

    def test_no_delivery_media_or_registered_bot_cannot_seed_public_ideas(self):
        source=self.store.prepare(OWNER,ORIGIN,'1',self.now,HUMAN)
        self.assertEqual(self.store.record_public_ideas(source,self.ideas()),0)
        media=self.confirmed(mid='2',media=True)
        self.assertEqual(self.store.record_public_ideas(media,self.ideas()),0)
        source=self.confirmed(mid='3')
        with self.store.connect() as db:
            db.execute('CREATE TABLE bots(qq TEXT PRIMARY KEY)');db.execute('INSERT INTO bots VALUES (?)',(OWNER,))
        self.assertEqual(self.store.record_public_ideas(source,self.ideas()),0)

    def test_refusal_revokes_drafts_and_late_receipts_without_deleting_posts(self):
        source=self.confirmed();self.store.record_public_ideas(source,self.ideas());topics=self.store.public_topics()
        path=self.root/'posts.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE qzone_editorial_candidates(text_key,text,plan)')
            db.execute('INSERT INTO qzone_editorial_candidates VALUES (?,?,?)',('draft','generated',json.dumps(dict(chat_topics=topics))))
            db.execute('CREATE TABLE posts(tid,text)');db.execute("INSERT INTO posts VALUES ('real','published')")
        self.store.prepare(OWNER,ORIGIN,'2',self.now,'这段别写进总结')
        self.assertFalse(self.store.topics_valid(topics));self.store.purge_drafts(path)
        self.assertEqual(self.store.record_public_ideas(source,self.ideas()),0)
        later=self.confirmed(mid='3');self.assertEqual(self.store.record_public_ideas(later,self.ideas()),0)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM qzone_editorial_candidates').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM posts').fetchone()[0],1)

    def test_global_optout_and_forget_survive_reopened_store(self):
        source=self.confirmed();self.store.record_public_ideas(source,self.ideas())
        self.store.revoke_public_ideas(OWNER)
        self.assertEqual(self.store.record_public_ideas(source,self.ideas()),0)
        self.store.prepare(OWNER,ORIGIN,'2',self.now,'以后别公开我的聊天')
        self.now+=3600;self.store=ActivityStore(self.store.path,self.clock)
        source=self.confirmed(mid='3');self.assertEqual(self.store.record_public_ideas(source,self.ideas()),0)
        self.assertEqual(self.store.public_topics(),[])

    def test_window_and_context_budget_do_not_force_a_topic(self):
        for i in range(12):
            source=self.confirmed(mid=str(i));self.store.record_public_ideas(source,self.ideas(IDEA+str(i)))
        topics=self.store.public_topics();self.assertEqual(len(topics),8)
        changed=[dict(topics[0],summary='篡改过的主题内容')];self.assertFalse(self.store.topics_valid(changed))
        self.now+=86400;self.assertTrue(self.store.public_topics())
        self.now+=7*86400;self.assertEqual(self.store.public_topics(),[]);self.assertFalse(self.store.topics_valid(topics))

    def test_optional_schema_rejects_private_scope_contacts_and_false_evidence(self):
        for payload in (sharing('联系邮箱 a@b.com'),sharing(evidence='不是当前人说的'),
                dict(ideas=[dict(scope='private_event',summary=IDEA,evidence=HUMAN)]),dict(ideas='broken')):
            self.assertEqual(sharing_proposal(payload,HUMAN),('none',[]))
        payload=dict(privacy=dict(action='disable',subject='quoted',modality='direct',evidence=HUMAN),ideas=[])
        self.assertEqual(sharing_proposal(payload,HUMAN),('none',[]))
        payload['privacy']['subject']='speaker';self.assertEqual(sharing_proposal(payload,HUMAN),('disable',[]))
        obj=json.loads(base.envelope());obj['sharing']='bad optional metadata'
        self.assertEqual(parse_generation(json.dumps(obj)).reply,obj['reply'])
        self.assertIsNone(parse_generation(base.envelope()).sharing)


def publication_db(path,now):
    path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE posts(tid TEXT,uin INTEGER,status TEXT,text TEXT,images TEXT,create_time REAL,comments TEXT)')
        comments=[dict(uin=7000000011,tid=7,parent_tid=None,content='路牌好特别',create_time=now-80)]
        db.executemany('INSERT INTO posts VALUES (?,?,?,?,?,?,?)',[
            ('post-1',BOT,'approved','临湾的旧招牌，今天的照片记下了弯弯的手写字。','["https://private-image-url"]',now-100,json.dumps(comments)),
            ('draft',BOT,'pending','不应召回的草稿','[]',now-90,'[]'),
            ('',BOT,'approved','没有发布回执','[]',now-90,'[]'),
            ('other','7000000011','approved','别人的动态','[]',now-90,'[]'),
            ('long-ago',BOT,'approved','很久以前分享的海边小提琴','[]',now-400*86400,'[]')])
        db.execute('CREATE TABLE qzone_auto_reply_comments(key,post_tid,cid,parent_id,author,status,reply,observed_at,updated_at)')
        db.executemany('INSERT INTO qzone_auto_reply_comments VALUES (?,?,?,?,?,?,?,?,?)',[
            ('sent','post-1',7,0,7000000011,'sent','对呀，招牌的字很有个性',now-70,now-60),
            ('unknown','post-1',8,0,7000000012,'unknown','不确定是否发送的回答',now-70,now-60)])


class PublicMemory(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.addCleanup(gc.collect)
        self.root=Path(self.tmp.name);self.now=time.time();self.path=self.root/'space.db'
        publication_db(self.path,self.now)
        self.config=dict(path=self.path,uin=BOT,platform='dafeiyu-qq',comments_owner=OWNER)
        self.catalog=MemoryCatalog(self.root,public_space=self.config)
        self.turn=TurnContext(OWNER,ORIGIN,'dafeiyu-qq','1',self.now)

    def test_only_confirmed_own_posts_and_replies_are_read_without_ttl_or_mutation(self):
        before=self.path.read_bytes();snap=self.catalog.read(self.turn,self.now);self.assertEqual(before,self.path.read_bytes())
        self.assertEqual(len(snap.items),4);body=json.dumps([r.data() for r in snap.items],ensure_ascii=False)
        for excluded in ('不应召回','没有发布回执','别人的动态','不确定是否发送','private-image-url'):self.assertNotIn(excluded,body)
        post=next(r for r in snap.items if r.id=='qzone:post:post-1:0');data=json.loads(post.text)
        self.assertEqual(data['image_count'],1);self.assertEqual(data['visual_content'],'not_inspected')
        self.assertEqual(data['live_visibility'],'not_checked');self.assertIn('海边小提琴',body)
        self.assertIn('"speaker": "commenter"',body.replace('\\"','"'))

    def test_group_and_other_friend_see_own_posts_but_no_comments_or_cross_platform(self):
        for turn in (TurnContext(OWNER,'dafeiyu-qq:GroupMessage:7000000101','dafeiyu-qq','2',self.now),
                TurnContext('7000000011','dafeiyu-qq:FriendMessage:7000000011','dafeiyu-qq','3',self.now)):
            self.assertEqual({r.kind for r in self.catalog.read(turn,self.now).items},{'publication'})
        turn=TurnContext(OWNER,'other:FriendMessage:'+OWNER,'other','4',self.now)
        self.assertEqual(self.catalog.read(turn,self.now).items,())
        forged=MemoryItem('fake',OWNER,ORIGIN,'public','fact','private',self.now,'human_evidence','dafeiyu-qq:public')
        self.assertFalse(forged.visible_to(self.turn))

    def test_relevance_and_deletion_refresh_do_not_reintroduce_answer_priming(self):
        snapshot=self.catalog.read(self.turn,self.now);index=SourceIndex(self.root/'index.db')
        items=automatic_items(snapshot.items);index.sync(self.turn,items)
        built=compose('',self.turn,snapshot,'这些旧招牌的字真好看',ranked=index.rank(self.turn,'旧招牌',items),contract=True)
        self.assertTrue(any(r['kind']=='publication' for r in built.evidence['memory']))
        self.assertEqual(compose('',self.turn,snapshot,'你好呀',ranked=index.rank(self.turn,'你好呀',items),contract=True).evidence['memory'],[])
        with sqlite3.connect(self.path) as db:db.execute("DELETE FROM posts WHERE tid='post-1'")
        current=self.catalog.read(self.turn,self.now);index.sync(self.turn,current.items)
        self.assertEqual(index.rank(self.turn,'旧招牌',current.items),[])
        self.assertFalse(any(r.kind=='public_interaction' for r in current.items))

    def test_long_posts_are_split_into_retrievable_evidence(self):
        text='海风吹过老街。'*600
        with sqlite3.connect(self.path) as db:db.execute("UPDATE posts SET text=? WHERE tid='post-1'",(text,))
        rows=[r for r in self.catalog.read(self.turn,self.now).items if r.id.startswith('qzone:post:post-1:')]
        self.assertGreater(len(rows),1);self.assertEqual(''.join(json.loads(r.text)['text'] for r in rows),text)
        self.assertTrue(all(len(r.text)<3000 for r in rows))


class NativeBridge(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.SingleGeneration.asyncSetUp
    asyncTearDown=base.SingleGeneration.asyncTearDown
    run_hooks=base.SingleGeneration.run_hooks
    ready=base.SingleGeneration.ready

    async def prepare(self,human=HUMAN):
        self.plugin.config['chat_model_verification']=False
        e,req=await self.ready(human)
        store=ActivityStore(self.plugin.root/'plugin_data/dafeiyu_social/preferences.db')
        e.set_extra('dafeiyu_activity_input',store.prepare(OWNER,ORIGIN,e.message_obj.message_id,time.time(),human))
        obj=json.loads(base.envelope('旧招牌像一座城留下的笔迹。'));obj['sharing']=sharing(evidence=human)
        return e,req,store,obj

    async def test_one_generation_delivery_then_automatic_editorial_inspiration(self):
        e,req,store,obj=await self.prepare();response=NS(completion_text=json.dumps(obj),tools_call_name=[])
        await self.plugin.review_response(e,response);await self.plugin.commit_learning(e)
        self.assertEqual(store.public_topics(),[])
        e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text=response.completion_text,at=time.time())])
        await self.plugin.commit_learning(e);await self.plugin.commit_learning(e)
        self.assertEqual([r['summary'] for r in store.public_topics()],[IDEA])
        policy=EditorialPolicy(self.plugin.root/'plugin_data/astrbot_plugin_qzone/posts_5.db')
        policy.recent=AsyncMock(return_value=[]);policy.daily_facts.collect=AsyncMock(return_value=[])
        plan,history=await policy.choose_plan(diary=True);prompt=policy.writing_prompt(plan,history)
        self.assertIn(IDEA,prompt);self.assertNotIn(HUMAN,prompt);self.assertNotIn(OWNER,prompt)
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited();e.send.assert_not_awaited()

    async def test_semantic_public_refusal_takes_effect_before_delivery(self):
        human='这段聊天不要拿去发空间';e,req,store,obj=await self.prepare(human)
        obj['sharing']=dict(privacy=dict(action='exclude_segment',evidence=human,subject='speaker',modality='direct'),ideas=[])
        response=NS(completion_text=json.dumps(obj),tools_call_name=[]);await self.plugin.review_response(e,response)
        source=e.get_extra('dafeiyu_activity_input');store.record_confirmed(source)
        self.assertEqual(store.record_public_ideas(source,sharing_proposal(sharing(evidence=human),human)[1]),0)
        self.assertEqual(e.get_extra('dafeiyu_public_privacy'),'exclude_segment');e.send.assert_not_awaited()

    async def test_forget_revokes_pending_public_material(self):
        e,req,store,obj=await self.prepare();source=e.get_extra('dafeiyu_activity_input');store.record_confirmed(source)
        store.record_public_ideas(source,sharing_proposal(obj['sharing'],HUMAN)[1]);self.assertTrue(store.public_topics())
        await self.plugin.forget_legacy(e,self.plugin.identity(e),base.privacy('forget_all',HUMAN))
        self.assertEqual(store.public_topics(),[])
        self.assertEqual(store.record_public_ideas(source,sharing_proposal(obj['sharing'],HUMAN)[1]),0)

    async def test_catalog_integration_and_self_lookup_use_records_without_model(self):
        path=self.plugin.root/'qzone.db';publication_db(path,time.time())
        old=self.context.get_registered_star
        qzone=NS(cfg=NS(db_path=path),own_space=NS(uin=BOT,platform_id='dafeiyu-qq'))
        self.context.get_registered_star=lambda name:NS(activated=True,star_cls=qzone) if name=='astrbot_plugin_qzone' else old(name)
        e,req,store,obj=await self.prepare('旧招牌真好看')
        self.assertTrue(any(r['kind']=='publication' for r in e.get_extra('dafeiyu_dialogue_evidence')['memory']))
        result=json.loads(await self.plugin.self_details(e,'publications'))
        self.assertTrue(result['read_only']);self.assertTrue(result['data']['records'])
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()


if __name__=='__main__':unittest.main()
