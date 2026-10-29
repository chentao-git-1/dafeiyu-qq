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
from data.plugins.dafeiyu_dialogue.persona import compact_persona, replace_persona
from data.plugins.dafeiyu_dialogue.review import review, parse_review, verify_revision, UNAVAILABLE
from data.plugins.dafeiyu_dialogue.self_state import self_state
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


class ReviewContracts(unittest.IsolatedAsyncioTestCase):
    async def test_accept_preserves_question_code_and_story_byte_for_byte(self):
        segments=['充电时屏幕能亮吗？', '```python\nprint("我刚画完")\n```', '故事里我昨天画了一条鱼。']
        call=AsyncMock(return_value='{"decision":"accept","issues":[],"revisions":[]}')
        result=await review(segments,{},call,human='帮我检查这段原文')
        self.assertEqual(result.segments,tuple(segments));call.assert_awaited_once()
        self.assertEqual(json.loads(call.call_args.args[0])['human'],'帮我检查这段原文')

    async def test_revise_only_identified_segment_and_preserve_order(self):
        segments=['我今天画完了鲸鱼。','充电有没有反应？']
        raw=json.dumps({'decision':'revise','issues':[{'segment_id':0,'kind':'unsupported_action','span':'画完了鲸鱼'}],
            'revisions':[{'segment_id':0,'text':'我倒是想画一条鲸鱼。'}]},ensure_ascii=False)
        result=parse_review(raw,segments)
        self.assertEqual(result.segments[1],segments[1]);self.assertEqual(result.decision,'revise')

    async def test_invalid_decisions_never_pass_through(self):
        valid={'decision':'revise','issues':[{'segment_id':0,'kind':'unsupported_fact','span':'做完了'}],
            'revisions':[{'segment_id':0,'text':'还没有完成记录。'}]}
        bad=[dict(valid,decision='accept'),dict(valid,revisions=[]),dict(valid,extra=True),
             dict(valid,issues=[{'segment_id':True,'kind':'unsupported_fact','span':'做完了'}]),
             dict(valid,issues=[{'segment_id':0,'kind':'new_category','span':'做完了'}]),
             dict(valid,issues=[{'segment_id':0,'kind':'unsupported_fact','span':'原文没有这句'}]),
             dict(valid,revisions=[{'segment_id':1,'text':'越界'}]),
             dict(valid,revisions=[{'segment_id':0,'text':'a'},{'segment_id':0,'text':'b'}])]
        for obj in bad:
            with self.subTest(obj=obj),self.assertRaises(ValueError):parse_review(json.dumps(obj),['我做完了。'])

    async def test_timeout_and_cancellation_propagate_without_retry(self):
        async def slow(*args):await asyncio.sleep(1)
        with self.assertRaises(TimeoutError):await review(['草稿'],{},slow,timeout=.005)
        with self.assertRaises(asyncio.CancelledError):
            await review(['草稿'],{},AsyncMock(side_effect=asyncio.CancelledError()))

    async def test_revised_claims_need_independent_evidence(self):
        call=AsyncMock(return_value='{"supported":false,"violations":[{"segment_id":0,"span":"还没弄完"}]}')
        self.assertFalse(await verify_revision(['那页手帐还没弄完。'],{},call))
        with self.assertRaises(ValueError):
            await verify_revision(['原文'],{},AsyncMock(return_value='{"supported":true,"violations":[{}]}'))

    async def test_composer_applies_acl_even_to_a_bad_adapter_snapshot(self):
        built=compose('policy',identity(),CatalogSnapshot((memory(owner=FRIEND,text='年糕的私人秘密'),)), '年糕')
        self.assertNotIn('私人秘密',built.prompt);self.assertEqual(built.evidence['memory'],[])

    async def test_missing_progress_is_explicit_and_not_inferred_from_completed(self):
        completed={'id':'work:1','day':'2026-10-30','completed_at':NOW,'note':'一张旧画'}
        state=self_state(completed)
        self.assertEqual(state['verified_completed']['id'],'work:1')
        self.assertIsNone(state['verified_in_progress'])
        self.assertIsNone(self_state({'completed_at':True})['verified_completed'])

    async def test_persona_compaction_keeps_identity_permissions_and_external_policies(self):
        seed=(STAGE.parent/'persona/live.md').read_text(encoding='utf-8')
        prompt='外部最高优先级规则\n'+seed+'\n另一插件附加规则'
        replaced,changed=replace_persona(prompt,seed)
        self.assertTrue(changed);self.assertLess(len(replaced),len(prompt))
        for value in ('外部最高优先级规则','另一插件附加规则','唯一的爸爸','权限','dafeiyu_tasks','web_search_deepseek'):
            self.assertIn(value,replaced)
        self.assertNotIn('BEGIN_JINGJING_OWN_LIFE',replaced)
        self.assertEqual(replace_persona(seed+seed,seed),(seed+seed,False))
        custom='用户完全自定义的人设\n禁止修改这项规则'
        self.assertEqual(compact_persona(custom),custom)


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

    async def reviewer(self, e, text='我刚把画到一半的画翻出来了。', raw=None):
        await self.run_hooks(e,ProviderRequest(prompt=e.message_str,system_prompt='operator seed'))
        raw=raw or '{"decision":"accept","issues":[],"revisions":[]}'
        call=AsyncMock(return_value=NS(completion_text=raw,usage=NS(input_other=10,input_cached=2,output=5)))
        self.context.get_using_provider_async=AsyncMock(return_value=NS(text_chat=call))
        response=NS(completion_text=text,tools_call_name=[])
        return response,call

    async def test_real_response_hook_checks_once_and_caches_without_sending(self):
        e=event('手机黑屏了');text='充电时屏幕能亮吗？'
        response,call=await self.reviewer(e,text)
        await self.plugin.review_response(e,response);await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,text);call.assert_awaited_once();e.send.assert_not_awaited()
        self.assertEqual(self.plugin.review_stats['review_input_other'],10)
        self.assertIsNone(call.call_args.kwargs['func_tool']);self.assertEqual(call.call_args.kwargs['contexts'],[])

    async def test_response_failure_closed_and_tool_failure_clears_media(self):
        e=event('忙啥呢');response,call=await self.reviewer(e)
        call.return_value.completion_text='{"decision":"accept","issues":[{}],"revisions":[]}'
        await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,UNAVAILABLE)
        arguments={'messages':[{'type':'plain','text':'草稿'},{'type':'image','path':'unverified.png'}]}
        await self.plugin.review_tool_message(e,NS(name='send_message_to_user'),arguments)
        self.assertEqual(arguments['messages'],[]);e.send.assert_not_awaited()

    async def test_revised_response_updates_native_history_and_confirmed_exchange(self):
        e=event('我今天什么都没干')
        raw=json.dumps({'decision':'revise','issues':[{'segment_id':0,'kind':'unsupported_action','span':'画到一半'}],
            'revisions':[{'segment_id':0,'text':'放空一天也挺好。'}]},ensure_ascii=False)
        response,call=await self.reviewer(e,raw=raw)
        call.side_effect=[NS(completion_text=raw),NS(completion_text='{"supported":true,"violations":[]}')]
        await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,'放空一天也挺好。')
        from astrbot.core.agent.message import Message,TextPart
        context=NS(messages=[Message(role='assistant',content=[TextPart(text='未经核验的旧草稿')])])
        await self.natural.save_grounded_reply(e,context,response)
        self.assertEqual(context.messages[-1].content[0].text,response.completion_text)
        e.set_extra('dafeiyu_confirmed_delivery',[{'kind':'reply','text':response.completion_text,'at':NOW}])
        await self.natural.remember_delivered_reply(e)
        with self.natural.exchange_store().connect() as db:
            row=db.execute('SELECT reply FROM exchanges ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(row['reply'],response.completion_text)

    async def test_tool_revisions_keep_media_target_and_segment_order(self):
        e=event('把这两句话发给我');_,call=await self.reviewer(e)
        call.return_value.completion_text=json.dumps({'decision':'revise',
            'issues':[{'segment_id':0,'kind':'unsupported_action','span':'做完了'}],
            'revisions':[{'segment_id':0,'text':'我想继续做。'}]},ensure_ascii=False)
        call.side_effect=[call.return_value,NS(completion_text='{"supported":true,"violations":[]}')]
        image={'type':'image','path':'existing.png'}
        args={'session':ORIGIN,'messages':[{'type':'plain','text':'我做完了'},image,{'type':'plain','text':'需要哪一张？'}]}
        await self.plugin.review_tool_message(e,NS(name='send_message_to_user'),args)
        self.assertEqual(args['session'],ORIGIN);self.assertIs(args['messages'][1],image)
        self.assertEqual(args['messages'][2]['text'],'需要哪一张？')
        self.assertEqual(args['messages'][0]['text'],'我想继续做。')

    async def test_rewrite_cannot_smuggle_another_unsupported_project(self):
        e=event('忙啥呢');response,call=await self.reviewer(e)
        raw=json.dumps({'decision':'revise','issues':[{'segment_id':0,'kind':'unsupported_action','span':'画到一半'}],
            'revisions':[{'segment_id':0,'text':'那页手帐还没弄完。'}]},ensure_ascii=False)
        call.side_effect=[NS(completion_text=raw),NS(completion_text='{"supported":false,"violations":[{"segment_id":0,"span":"还没弄完"}]}')]
        await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,UNAVAILABLE);self.assertEqual(call.await_count,2)

    async def test_shadow_and_off_never_review_or_collect_content(self):
        for mode in ('shadow','off'):
            self.plugin.config['mode']=mode;e=event('忙啥呢')
            response,call=await self.reviewer(e)
            await self.plugin.review_response(e,response)
            await self.plugin.collect_tool_evidence(e,NS(name='fake'),{},'body')
            call.assert_not_awaited();self.assertIsNone(e.get_extra('dafeiyu_dialogue_tool_evidence',None))

    async def test_activity_change_during_review_prevents_delivery(self):
        e=event('忙啥呢');response,call=await self.reviewer(e)
        with patch.object(self.plugin,'own_activity',AsyncMock(side_effect=[(None,None),
                ({'id':'new','day':'2026-10-30','completed_at':NOW},None)])):
            await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,UNAVAILABLE)

    async def test_activity_snapshot_is_read_only_and_stale_run_is_unknown(self):
        from data.plugins.dafeiyu_life.life import LifeStore
        store=LifeStore(self.root/'activity');run=store.schedule();store.claim(run['day'],'drawing')
        life=NS(store=store,public_context=store.public_context)
        self.context.get_registered_star=lambda name:NS(activated=True,star_cls=life if name=='dafeiyu_life' else self.natural) if name in ('dafeiyu_life','dafeiyu_natural_chat') else None
        before=store.path.read_bytes();completed,progress=await self.plugin.own_activity()
        self.assertIsNone(completed);self.assertEqual(progress['kind'],'drawing')
        self.assertEqual(store.path.read_bytes(),before)
        with store.db() as db:db.execute('UPDATE runs SET updated=?',(NOW-601,))
        del db;gc.collect()
        self.assertEqual(await self.plugin.own_activity(),(None,None))

    async def test_fiction_profile_is_present_without_claiming_actual_activity(self):
        e=event('在故事里继续讲小芽');await self.run_hooks(e,ProviderRequest(prompt=e.message_str))
        material=e.get_extra('dafeiyu_dialogue_evidence')
        self.assertEqual(material['character']['fictional_world']['kind'],'fictional_world')
        self.assertIsNone(material['verified_self']['verified_completed'])

    async def test_forgetting_during_review_fails_closed(self):
        e=event('年糕怎么样');response,call=await self.reviewer(e)
        evidence=e.get_extra('dafeiyu_dialogue_evidence');evidence['memory']=[memory().data()]
        catalog=NS(read=unittest.mock.Mock(side_effect=[CatalogSnapshot((memory(),)),CatalogSnapshot(())]))
        with patch.object(self.plugin,'catalog',return_value=catalog):await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,UNAVAILABLE)

    async def test_cancellation_cannot_leave_executable_unsafe_message(self):
        e=event('忙啥呢');response,call=await self.reviewer(e)
        call.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,UNAVAILABLE)
        args={'messages':[{'type':'plain','text':'我全做完了'}]}
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.review_tool_message(e,NS(name='send_message_to_user'),args)
        self.assertEqual(args['messages'],[])

    async def test_per_turn_budget_blocks_third_review_without_retry(self):
        e=event('忙啥呢');response,call=await self.reviewer(e)
        for text in ('第一份草稿','第二份草稿','第三份草稿'):
            response.completion_text=text;await self.plugin.review_response(e,response)
        self.assertEqual(call.await_count,2);self.assertEqual(response.completion_text,UNAVAILABLE)


if __name__=='__main__':unittest.main()
