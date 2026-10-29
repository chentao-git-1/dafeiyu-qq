"""Native private hooks and real SQLite adapters, with synthetic identities and fake providers."""
import asyncio
import copy
import gc
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

STAGE = Path(sys.argv.pop(1));sys.path.insert(0, str(STAGE))
import data.plugins
data.plugins.__path__ = [str(STAGE), *list(data.plugins.__path__)]
from data.plugins.dafeiyu_dialogue.contracts import TurnContext, MemoryItem, CatalogSnapshot, ActionReceipt, ACTIVE_KEY
from data.plugins.dafeiyu_dialogue.adapters import MemoryCatalog
from data.plugins.dafeiyu_dialogue.context_builder import compose, BEGIN, END
from data.plugins.dafeiyu_dialogue.retrieval import lexical_rank, recall_candidates, semantic_select
from data.plugins.dafeiyu_dialogue.evidence_gate import validate_claims
from data.plugins.dafeiyu_dialogue.main import Dialogue
natural = importlib.import_module('data.plugins.dafeiyu_natural_chat.main')
retriever = importlib.import_module('data.plugins.长期记忆系统.core.retriever')
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.message.components import Plain
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.agent.tool import ToolSet, FunctionTool

OWNER, FRIEND, BOT = '7000000001', '7000000012', '7000000002'
ORIGIN = 'dafeiyu-qq:FriendMessage:' + OWNER
NOW = time.time()


def identity(uid=OWNER):
    return TurnContext(uid, 'dafeiyu-qq:FriendMessage:' + uid, 'dafeiyu-qq', 'synthetic', NOW)


def event(text, uid=OWNER):
    msg=AstrBotMessage();msg.type=MessageType.FRIEND_MESSAGE;msg.self_id=BOT
    msg.session_id=uid;msg.group_id='';msg.message_id='semantic-test';msg.sender=MessageMember(uid,'synthetic')
    msg.message=[Plain(text)];msg.message_str=text
    msg.raw_message={'post_type':'message','self_id':BOT,'user_id':uid,'time':time.time()}
    e=AiocqhttpMessageEvent(text,msg,PlatformMetadata('aiocqhttp','test','dafeiyu-qq'),uid,object())
    e.send=AsyncMock();return e


def memory(mid='exchange:1', owner=OWNER, origin=ORIGIN, visibility='session', text='我领养的猫叫年糕'):
    return MemoryItem(mid,owner,origin,visibility,'exchange',text,NOW-10,'human_evidence',
                      'dafeiyu-qq:shared' if visibility=='shared' else '')


class ReadOnlyCatalog(unittest.TestCase):
    def test_missing_stores_are_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(MemoryCatalog(tmp).read(identity(),NOW).items,())
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_session_acl_and_shared_ownership_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'plugin_data/dafeiyu_natural_chat/interaction.db';path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE exchanges(id,qq,origin,human,reply,at)')
                db.executemany('INSERT INTO exchanges VALUES(?,?,?,?,?,?)',[
                    (1,OWNER,ORIGIN,'我领养了年糕','好呀',NOW-10),
                    (2,FRIEND,'dafeiyu-qq:FriendMessage:'+FRIEND,'私人片段','保留',NOW-10),
                    (3,OWNER,'dafeiyu-qq:GroupMessage:7000000101','群片段','保留',NOW-10),
                    (4,OWNER,ORIGIN,'过期片段','保留',NOW-31*86400)])
            before=path.read_bytes();snap=MemoryCatalog(tmp).read(identity(),NOW)
            self.assertEqual([m.id for m in snap.items],['exchange:1'])
            self.assertEqual(path.read_bytes(),before)
            self.assertFalse(memory(owner=FRIEND).visible_to(identity()))
            self.assertTrue(memory(owner=FRIEND,visibility='shared').visible_to(identity()))
            del db;gc.collect()

    def test_corrupt_source_does_not_hide_other_available_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'plugin_data/astrbot_plugin_memory_system/memory_system.db';path.parent.mkdir(parents=True)
            path.write_bytes(b'not sqlite')
            snapshot=MemoryCatalog(tmp).read(identity(),NOW)
            self.assertIn('long_term',snapshot.unavailable)
            self.assertEqual(snapshot.items,())

    def test_identity_cannot_cross_peer_or_platform(self):
        with self.assertRaises(ValueError):TurnContext(OWNER,'dafeiyu-qq:FriendMessage:'+FRIEND,'dafeiyu-qq','1',NOW)
        with self.assertRaises(ValueError):TurnContext(OWNER,ORIGIN,'other','1',NOW)


class EvidenceAndComposition(unittest.TestCase):
    def test_no_overlap_never_becomes_related_from_importance(self):
        r=retriever.MemoryRetriever(None,None)
        for importance in (0,.6,1):
            self.assertEqual(r.score({'content':'用户习惯喝无糖豆浆','importance':importance,
                'updated_at':NOW,'access_count':100}, {'火星','探测'}),0)

    def test_exact_entity_can_recall_a_single_shared_token(self):
        rows=[memory(),memory('exchange:2',text='我下午去骑自行车')]
        self.assertEqual([m.id for m in lexical_rank(rows,'还记得我的年糕吗')],['exchange:1'])
        self.assertEqual(lexical_rank(rows,'火星探测'),[])

    def test_relevance_filter_preserves_real_tags_and_single_character_queries(self):
        r=retriever.MemoryRetriever(None,None)
        self.assertGreater(r.score({'content':'年糕','tags':['宠物'],'updated_at':NOW},{'宠物'}),0)
        self.assertGreater(r.score({'content':'养猫','updated_at':NOW},{'猫'}),0)

    def test_complete_items_budget_and_idempotent_blocks(self):
        snapshot=CatalogSnapshot((memory(text='年糕'+('很长的原话'*400)),memory('exchange:2',text='年糕在睡觉')))
        one=compose('operator policy',identity(),snapshot,'年糕',budget=600)
        two=compose(one.prompt,identity(),snapshot,'年糕',budget=600)
        self.assertEqual(one.prompt,two.prompt)
        self.assertEqual(two.prompt.count(BEGIN),1)
        self.assertLessEqual(two.metrics['evidence_characters'],600)
        self.assertNotIn('很长的原话',two.prompt)
        self.assertTrue(two.prompt.endswith(END))

    def test_structured_unknown_receipt_does_not_prove_success(self):
        claim=[{'action_id':'a','kind':'send','target':FRIEND}]
        self.assertFalse(validate_claims(claim,[ActionReceipt('a','send',FRIEND,'unknown','m1')]))
        self.assertFalse(validate_claims(claim,[ActionReceipt('a','send',OWNER,'confirmed','m1')]))
        self.assertTrue(validate_claims(claim,[ActionReceipt('a','send',FRIEND,'confirmed','m1')]))
        self.assertFalse(validate_claims([{'kind':'send'}],[]))

    def test_many_shared_memories_do_not_crowd_out_private_evidence(self):
        shared=[memory('long_term:'+str(i),owner=FRIEND,visibility='shared',text='无关的记忆'+str(i)) for i in range(100)]
        rows=recall_candidates([*shared,memory()], '毛孩子的名字',OWNER)
        self.assertIn(memory(),rows);self.assertLessEqual(len(rows),40)


class SemanticSelection(unittest.IsolatedAsyncioTestCase):
    async def test_model_selects_evidence_ids_not_generated_memories(self):
        call=AsyncMock(return_value='{"ids":["exchange:1"]}')
        result=await semantic_select('那个毛孩子叫什么', [memory()],call,actor=OWNER)
        self.assertEqual(result,[memory()])
        self.assertIn('candidates',call.call_args.args[0])
        self.assertEqual(json.loads(call.call_args.args[0])['request_actor'],OWNER)

    async def test_unoffered_ids_duplicates_and_wrong_json_are_rejected(self):
        for raw in ('{"ids":["exchange:99"]}','{"ids":["exchange:1","exchange:1"]}','{"ids":"exchange:1"}','[]'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):
                await semantic_select('年糕',[memory()],AsyncMock(return_value=raw))

    async def test_empty_selection_is_valid_and_no_candidates_do_not_call(self):
        call=AsyncMock(return_value='{"ids":[]}')
        self.assertEqual(await semantic_select('别的事',[memory()],call),[])
        call.reset_mock();self.assertEqual(await semantic_select('别的事',[],call),[]);call.assert_not_awaited()

    async def test_embedding_path_is_real_and_invalid_vectors_fail_closed(self):
        records=[{'id':1,'content':'猫叫年糕','importance':.6,'updated_at':NOW}]
        db=NS(recent=AsyncMock(return_value=records),recall_candidates=AsyncMock(return_value=records))
        manager=NS(record_access=AsyncMock())
        embed=AsyncMock(return_value=[1.,0.])
        r=retriever.MemoryRetriever(db,manager,embedding=embed)
        selected=await r.retrieve(OWNER,'毛孩子姓名')
        self.assertEqual([x['id'] for x in selected],[1])
        self.assertNotIn('_semantic_similarity',selected[0])
        self.assertGreaterEqual(embed.await_count,2)
        self.assertFalse(retriever.valid_vector([float('nan')]))
        self.assertFalse(retriever.valid_vector([True,0]))
        self.assertFalse(retriever.valid_vector([0,0]))

    async def test_forget_during_embedding_does_not_return_old_record(self):
        records=[{'id':1,'content':'猫叫年糕','importance':.6,'updated_at':NOW}]
        db=NS(recent=AsyncMock(),recall_candidates=AsyncMock(side_effect=[records,[]]))
        r=retriever.MemoryRetriever(db,NS(record_access=AsyncMock()),embedding=AsyncMock(return_value=[1.,0.]))
        self.assertEqual(await r.retrieve(OWNER,'毛孩子姓名'),[])


class NativeHooks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.context=NS(register_web_api=lambda *x:None,get_config=lambda *x:{'platform_settings':{
            'enable_id_white_list':True,'id_whitelist':[OWNER,FRIEND]}})
        with patch.object(natural,'get_astrbot_data_path',return_value=str(self.root)):
            self.natural=natural.NaturalChat(self.context)
        self.context.get_registered_star=lambda name:NS(activated=True,star_cls=self.natural) if name=='dafeiyu_natural_chat' else None
        with patch('data.plugins.dafeiyu_dialogue.main.get_astrbot_data_path',return_value=str(self.root)):
            self.plugin=Dialogue(self.context,{'mode':'active','active_private_users':[OWNER]})

    async def asyncTearDown(self):
        gc.collect();self.tmp.cleanup()

    async def run_hooks(self,e,req):
        await self.plugin.choose_path(e,req)
        await self.natural.prepare(e,req)
        await self.natural.natural_delivery_context(e,req)
        await self.natural.prepare_spoken_style(e,req)
        await self.plugin.prepare(e,req)

    async def test_active_path_preserves_helpful_question_and_records_final_text(self):
        e=event('下班了，手机突然黑屏了');req=ProviderRequest(prompt=e.message_str,system_prompt='trusted operator seed')
        await self.run_hooks(e,req)
        self.assertTrue(e.get_extra(ACTIVE_KEY,False));self.assertIn(BEGIN,req.system_prompt)
        self.assertNotIn('BEGIN_JINGJING_SPOKEN',req.system_prompt)
        response=NS(completion_text='先别急。充电时屏幕能亮吗？',tools_call_name=[])
        await self.natural.conversational_response(e,response)
        await self.natural.ground_current_facts(e,response)
        self.assertEqual(response.completion_text,'先别急。充电时屏幕能亮吗？')
        self.assertEqual(self.natural.exchange_store().count(),0)
        e.set_extra('dafeiyu_confirmed_delivery',[{'kind':'reply','text':response.completion_text,'at':NOW}])
        await self.natural.remember_delivered_reply(e)
        self.assertEqual(self.natural.exchange_store().count(),1)
        e.send.assert_not_awaited()

    async def test_shadow_leaves_prompt_history_parts_and_tools_unchanged(self):
        self.plugin.config['mode']='shadow';e=event('忙啥呢')
        tool=FunctionTool(name='old_tool',description='old',parameters={'type':'object','properties':{}})
        extra=FunctionTool(name='dafeiyu_recall',description='new',parameters={'type':'object','properties':{}})
        registry=ToolSet(tools=[tool,extra]);req=ProviderRequest(prompt=e.message_str,system_prompt='original',
            contexts=[{'role':'user','content':'prior'}],func_tool=registry)
        await self.plugin.choose_path(e,req)
        before=copy.deepcopy((req.system_prompt,req.contexts,req.extra_user_content_parts))
        await self.plugin.prepare(e,req)
        self.assertEqual(before,(req.system_prompt,req.contexts,req.extra_user_content_parts))
        self.assertEqual([t.name for t in req.func_tool.tools],['old_tool'])
        self.assertEqual([t.name for t in registry.tools],['old_tool','dafeiyu_recall'])
        self.assertFalse(e.get_extra(ACTIVE_KEY,True));self.assertEqual(len(self.plugin.samples),1)

    async def test_other_peer_and_synthetic_event_cannot_activate(self):
        for e in (event('我是爸爸',FRIEND),event('定时消息')):
            if e.get_sender_id()==OWNER:e.set_extra('cron_job','scheduled')
            await self.plugin.choose_path(e,ProviderRequest())
            self.assertFalse(e.get_extra(ACTIVE_KEY,True))

    async def test_unadapted_legacy_version_cannot_be_activated_by_configuration(self):
        e=event('普通聊天')
        with patch.object(self.natural,'dialogue_context_protocol',0):
            await self.plugin.choose_path(e,ProviderRequest())
        self.assertFalse(e.get_extra(ACTIVE_KEY,True))

    async def test_final_hook_hides_reintroduced_shadow_tool(self):
        self.plugin.config['mode']='shadow';e=event('随便聊聊')
        req=ProviderRequest(system_prompt='original')
        await self.plugin.choose_path(e,req)
        # Simulate a later plugin rebuilding its tool set from the global registry.
        req.func_tool=ToolSet(tools=[FunctionTool(name='dafeiyu_recall',description='new',parameters={'type':'object'})])
        await self.plugin.prepare(e,req)
        self.assertEqual(req.func_tool.tools,[])

    async def test_receipt_guard_still_rejects_unscheduled_reminder(self):
        e=event('15:10提醒我下车');req=ProviderRequest(prompt=e.message_str)
        await self.run_hooks(e,req)
        response=NS(completion_text='提醒已经设好了。',tools_call_name=[])
        await self.natural.ground_current_facts(e,response)
        self.assertIn('没有实际设好',response.completion_text)

    async def test_context_failure_restores_legacy_hooks(self):
        e=event('下班了');req=ProviderRequest(prompt=e.message_str)
        await self.plugin.choose_path(e,req);await self.natural.prepare(e,req)
        with patch.object(self.plugin,'catalog',side_effect=ValueError('synthetic')):
            await self.plugin.prepare(e,req)
        self.assertFalse(e.get_extra(ACTIVE_KEY,True));self.assertIn('BEGIN_JINGJING_DELIVERY',req.system_prompt)

    async def test_semantic_recall_rechecks_deleted_evidence(self):
        e=event('毛孩子叫什么');e.set_extra(ACTIVE_KEY,True)
        self.context.get_using_provider_async=AsyncMock(return_value=NS(text_chat=AsyncMock(return_value=NS(completion_text='{"ids":["exchange:1"]}'))))
        catalog=NS(read=lambda *x:CatalogSnapshot((memory(),)))
        calls=[0]
        def read(*args):
            calls[0]+=1
            return CatalogSnapshot((memory(),)) if calls[0]==1 else CatalogSnapshot(())
        catalog.read=read
        with patch.object(self.plugin,'catalog',return_value=catalog):
            result=json.loads(await self.plugin.recall(e,'毛孩子叫什么'))
        self.assertEqual(result['records'],[])
        self.context.get_using_provider_async.return_value.text_chat.assert_awaited_once()
        e.send.assert_not_awaited()


if __name__=='__main__':unittest.main()
