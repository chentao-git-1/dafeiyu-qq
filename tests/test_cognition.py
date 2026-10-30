"""Source lifecycle, single generation, real SQLite and bounded background policies."""
import asyncio
import gc
from collections import Counter
import hashlib
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

STAGE=Path(sys.argv.pop(1));sys.path.insert(0,str(STAGE));sys.argv.insert(1,str(STAGE))
import test_semantic_dialogue as fixture
from data.plugins.dafeiyu_dialogue.contracts import ACTIVE_KEY, CatalogSnapshot
from data.plugins.dafeiyu_dialogue.generation import parse_generation, validate_claim_sources, claim_sources
from data.plugins.dafeiyu_dialogue.learning import LearningStore, proposals
from data.plugins.dafeiyu_dialogue.indexes import SourceIndex
from data.plugins.dafeiyu_life.planning import SelfJournal, parse_choice
from data.plugins.dafeiyu_companion.spontaneous_policy import parse_selection
from data.plugins.dafeiyu_companion.spontaneous_store import InitiativeStore
from data.plugins.dafeiyu_bailian_tts.expression import speech_plan
from data.plugins.dafeiyu_dialogue.review import UNAVAILABLE, packet_key

OWNER,FRIEND,ORIGIN=fixture.OWNER,fixture.FRIEND,fixture.ORIGIN


def privacy(action='none',evidence='',ids=()):
    return dict(action=action,evidence=evidence,ids=list(ids),subject='speaker',modality='direct')


def item(evidence='以后我倾诉时先陪我聊聊，明确问怎么办时再给建议',**changes):
    return dict(kind='preference',summary='倾诉时优先回应，求建议时给方案',condition='本人倾诉且没有明确求解',
        evidence=evidence,subject='speaker',modality='direct',retention='persistent',status='active',
        event_day='',supersedes='',expected_version=0,**changes)


def envelope(reply='先说说按电源时有没有灯亮。',items=(),claims=(),control=None,intent='explain'):
    return json.dumps({'focus':['help'],'reply':reply,'claims':list(claims),'learning':{'privacy':control or privacy(),'items':list(items)},
        'expression':intent},ensure_ascii=False)


class LearnedLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=LearningStore(self.root/'learning.db')

    def tearDown(self):self.tmp.cleanup()

    def source(self,text,mid='one',owner=OWNER):
        turn=fixture.identity(owner)
        from dataclasses import replace
        turn=replace(turn,message_id=mid)
        return self.store.begin(turn,text)

    def test_condition_and_exception_survive_restart(self):
        proposal=item();source=self.source(proposal['evidence'])
        saved=self.store.commit(source,proposal['evidence'],[proposal],{},confirmed=True)
        self.assertEqual(len(saved),1)
        reloaded=LearningStore(self.store.path).current(OWNER,ORIGIN)
        self.assertEqual(reloaded[0]['condition'],proposal['condition'])
        self.assertEqual(reloaded[0]['summary'],proposal['summary'])
        self.assertEqual(reloaded[0]['evidence'],proposal['evidence'])

    def test_unconfirmed_and_replayed_delivery_do_not_duplicate(self):
        proposal=item();source=self.source(proposal['evidence'])
        self.assertEqual(self.store.commit(source,proposal['evidence'],[proposal],{},confirmed=False),[])
        self.assertEqual(len(self.store.commit(source,proposal['evidence'],[proposal],{},confirmed=True)),1)
        self.assertEqual(self.store.commit(source,proposal['evidence'],[proposal],{},confirmed=True),[])
        self.assertEqual(self.store.counts()['entities'],1)

    def test_deny_invalidates_already_pending_turn(self):
        proposal=item();old=self.source(proposal['evidence'],'old')
        deny=self.source('这段别记','new')
        self.store.apply_privacy(deny,privacy('deny_turn','这段别记'),[])
        self.assertEqual(self.store.commit(old,proposal['evidence'],[proposal],{},confirmed=True),[])

    def test_quoted_third_person_and_temporary_preferences_do_not_persist(self):
        human='朋友说以后回复短点，小说台词也这么说；我只是这会儿赶时间'
        for fields in ({'subject':'other'},{'modality':'quoted'},{'retention':'transient'}):
            proposal=item(human);proposal.update(fields)
            _,accepted=proposals({'privacy':privacy(),'items':[proposal]},human)
            self.assertEqual(accepted,[])
        quoted=privacy('forget_all','以后回复短点');quoted['modality']='quoted'
        parsed,_=proposals({'privacy':quoted,'items':[]},human)
        self.assertEqual(parsed['action'],'none')

    def test_versioned_correction_preserves_old_evidence_and_rejects_stale_update(self):
        first=item();s1=self.source(first['evidence'],'one')
        eid=self.store.commit(s1,first['evidence'],[first],{},confirmed=True)[0]
        newer='现在我明确求方案时可以详细讲'
        second=item(newer);second.update(supersedes=eid,expected_version=1,condition='明确求方案时',summary='明确求方案时详细解释')
        s2=self.source(newer,'two');self.store.commit(s2,newer,[second],{eid:1},confirmed=True)
        stale=self.source(newer,'three')
        self.assertEqual(self.store.commit(stale,newer,[second],{eid:1},confirmed=True),[])
        self.assertEqual(self.store.current(OWNER,ORIGIN)[0]['version'],2)
        self.assertEqual(self.store.counts()['versions'],1)

    def test_forgetting_erases_versions_and_cannot_resurrect_from_old_source(self):
        proposal=item();s1=self.source(proposal['evidence'],'one')
        eid=self.store.commit(s1,proposal['evidence'],[proposal],{},confirmed=True)[0]
        forget=self.source('忘掉这项偏好','forget')
        self.assertEqual(self.store.apply_privacy(forget,privacy('forget_selected','忘掉这项偏好',[eid]),[eid]),1)
        self.assertEqual(self.store.current(OWNER,ORIGIN),[])
        self.assertEqual(self.store.commit(s1,proposal['evidence'],[proposal],{},confirmed=True),[])
        self.assertFalse(proposal['evidence'].encode() in self.store.path.read_bytes(),'Forgotten evidence remains in SQLite pages')

    def test_peer_cannot_change_or_delete_another_owner(self):
        proposal=item();source=self.source(proposal['evidence'])
        eid=self.store.commit(source,proposal['evidence'],[proposal],{},confirmed=True)[0]
        peer=self.source('把这项改掉','peer',FRIEND)
        candidate=item('把这项改掉');candidate.update(supersedes=eid,expected_version=1)
        self.assertEqual(self.store.commit(peer,'把这项改掉',[candidate],{eid:1},confirmed=True),[])
        self.assertEqual(self.store.apply_privacy(peer,privacy('forget_selected','把这项改掉',[eid]),[eid]),0)
        self.assertEqual(len(self.store.current(OWNER,ORIGIN)),1)

    def test_arbitrary_event_can_change_and_cancel_without_topic_whitelist(self):
        text='我明天领养一只猫，叫年糕'
        candidate=item(text);candidate.update(kind='event',summary='领养年糕',condition='',status='planned',event_day='2026-10-31')
        first=self.source(text);eid=self.store.commit(first,text,[candidate],{},confirmed=True)[0]
        text2='猫舍临时有事，我先不去了'
        update=dict(candidate,evidence=text2,status='cancelled',supersedes=eid,expected_version=1)
        self.store.commit(self.source(text2,'cancel'),text2,[update],{eid:1},confirmed=True)
        self.assertEqual(self.store.current(OWNER,ORIGIN)[0]['status'],'cancelled')

    def test_source_message_cannot_be_replaced_by_replay(self):
        self.source('本人最初的真实原话')
        with self.assertRaises(ValueError):self.source('被修改的另一段话')


class SourceIndexTests(unittest.TestCase):
    def test_rebuild_version_update_and_forget_do_not_retain_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=SourceIndex(Path(tmp)/'index.db');turn=fixture.identity();first=fixture.memory()
            index.sync(turn,[first],provider_version='a')
            self.assertEqual(index.rank(turn,'年糕',[first]),[first])
            changed=fixture.memory(text='我的猫改名豆包')
            self.assertEqual(index.rank(turn,'年糕',[changed]),[])
            index.sync(turn,[changed],provider_version='b')
            self.assertEqual(index.rank(turn,'豆包',[changed]),[changed])
            index.invalidate([changed.id]);self.assertEqual(index.rank(turn,'豆包',[changed]),[])
            self.assertNotIn('我的猫'.encode(),index.path.read_bytes())

    def test_acl_applies_before_index_and_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            index=SourceIndex(Path(tmp)/'index.db');peer=fixture.memory(owner=FRIEND)
            index.sync(fixture.identity(),[peer]);self.assertEqual(index.rank(fixture.identity(),'年糕',[peer]),[])


class SingleGeneration(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.NativeHooks.asyncSetUp
    asyncTearDown=fixture.NativeHooks.asyncTearDown
    run_hooks=fixture.NativeHooks.run_hooks

    async def ready(self,text='我的电脑开不了机',learning=True):
        self.plugin.config.update(generation_contract=True,learning_v2=learning,retrieval_index_v2=True)
        e=fixture.event(text);req=fixture.ProviderRequest(prompt=text,system_prompt='trusted persona')
        async def model(**kwargs):
            if '只核对本轮学习提案' in kwargs.get('system_prompt',''):
                packet=json.loads(kwargs['prompt'])
                return NS(completion_text=json.dumps(dict(privacy_authorized=True,accepted=list(range(len(packet['items']))))))
            raise AssertionError('Ordinary generation must not call an auxiliary model')
        self.context.get_using_provider_async=AsyncMock(return_value=NS(text_chat=AsyncMock(side_effect=model)))
        await self.run_hooks(e,req)
        return e,req

    async def test_ordinary_reply_and_necessary_question_need_no_extra_model(self):
        e,req=await self.ready();reply='按电源时有没有灯亮、风扇转？'
        response=NS(completion_text=envelope(reply),tools_call_name=[])
        await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,reply);self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()
        self.assertNotIn('BEGIN_JINGJING_SPOKEN',req.system_prompt);e.send.assert_not_awaited()

    async def test_learning_waits_for_confirmed_delivery_and_keeps_conditions(self):
        proposal=item();e,_=await self.ready(proposal['evidence'])
        response=NS(completion_text=envelope('嗯，你想说的时候先说。',[proposal]),tools_call_name=[])
        await self.plugin.review_response(e,response);await self.plugin.commit_learning(e)
        self.assertEqual(self.plugin.learning_store().counts()['entities'],0)
        e.set_extra('dafeiyu_confirmed_delivery',[{'kind':'reply','text':response.completion_text,'at':time.time()}])
        await self.plugin.commit_learning(e);await self.plugin.commit_learning(e)
        rows=self.plugin.learning_store().current(OWNER,ORIGIN)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['condition'],proposal['condition'])

    async def test_bad_claim_never_becomes_native_text_or_learning(self):
        e,_=await self.ready('忙啥呢')
        response=NS(completion_text=envelope('我画完了。',claims=[dict(kind='self_activity',span='画完了',evidence_ids=['human:current'])]),tools_call_name=[])
        await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,UNAVAILABLE)
        self.context.get_using_provider_async.return_value.text_chat.assert_awaited_once()

    async def test_native_history_uses_already_confirmed_tool_text(self):
        e,_=await self.ready();e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text='实际已经发出的正文',at=time.time())])
        response=NS(completion_text=envelope('未发送的总结'),tools_call_name=[])
        await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,'实际已经发出的正文')

    async def test_shadow_creates_neither_learning_nor_index(self):
        self.plugin.config.update(mode='shadow',generation_contract=True,learning_v2=True,retrieval_index_v2=True)
        e=fixture.event('普通聊天');await self.run_hooks(e,fixture.ProviderRequest(system_prompt='seed'))
        self.assertIsNone(self.plugin._learning);self.assertIsNone(self.plugin._index)

    async def test_old_automatic_write_tools_are_hidden_without_changing_registry(self):
        e,_=await self.ready()
        tools=fixture.ToolSet(tools=[fixture.FunctionTool(name=name,description='fixture',parameters={'type':'object'})
            for name in ('memorize_long_term_memory','jingjing_episodes','forget_long_term_memory','dafeiyu_recall')])
        req=fixture.ProviderRequest(func_tool=tools);self.plugin.hide_automatic_legacy_writes(req)
        self.assertEqual({t.name for t in req.func_tool.tools},{'forget_long_term_memory','dafeiyu_recall'})
        self.assertEqual(len(tools.tools),4)

    async def test_native_mcp_receipts_are_evidence_but_error_receipts_are_not(self):
        from mcp.types import CallToolResult,TextContent
        e,_=await self.ready();tool=NS(name='dafeiyu_tasks')
        result=CallToolResult(content=[TextContent(type='text',text='{"created":true,"job_id":"verified"}')])
        await self.plugin.collect_tool_evidence(e,tool,{},result)
        offered=claim_sources({},e.get_extra('dafeiyu_dialogue_tool_evidence',[]))
        self.assertIn('tool:dafeiyu_tasks',offered)
        e.set_extra('dafeiyu_dialogue_tool_evidence',[]);result.isError=True
        await self.plugin.collect_tool_evidence(e,tool,{},result)
        self.assertEqual(e.get_extra('dafeiyu_dialogue_tool_evidence',[]),[])

    async def test_unclaimed_self_report_still_requires_review(self):
        e,_=await self.ready('你的真实近况呢')
        call=AsyncMock(return_value=NS(completion_text='{"decision":"accept","issues":[],"revisions":[]}'))
        self.context.get_using_provider_async.return_value=NS(text_chat=call)
        obj=json.loads(envelope('这会儿在跟你说话。'));obj['focus']=['self']
        response=NS(completion_text=json.dumps(obj),tools_call_name=[])
        await self.plugin.review_response(e,response);call.assert_awaited_once()

    async def test_denied_turn_is_not_saved_as_native_or_paired_history(self):
        from astrbot.core.agent.message import Message
        human='这一轮别记住我刚说的';e,_=await self.ready(human)
        response=NS(completion_text=envelope('嗯。',control=privacy('deny_turn',human)),tools_call_name=[])
        await self.plugin.review_response(e,response)
        previous=Message(role='user',content='先前无关的话')
        native=NS(messages=[previous,Message(role='assistant',content='之前回复'),
            Message(role='user',content=human),Message(role='assistant',content=response.completion_text)])
        await self.plugin.save_native_context(e,native,response)
        self.assertEqual(len(native.messages),2)
        e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text='嗯。')])
        await self.natural.remember_delivered_reply(e)
        self.assertEqual(self.natural.exchange_store().count(),0)

    async def test_forgetting_rebases_shared_native_context_and_cannot_relearn_inflight(self):
        e,req=await self.ready('之前的记忆全部忘掉')
        friend_origin=ORIGIN.rsplit(':',1)[0]+':'+FRIEND
        self.natural.exchange_store().record(FRIEND,friend_origin,'friend-old','朋友自己的原话','曾复述的旧资料')
        response=NS(completion_text=envelope('好了。',control=privacy('forget_all',e.message_str)),tools_call_name=[])
        await self.plugin.review_response(e,response)
        from astrbot.core.agent.message import Message
        native=NS(messages=[Message(role='user',content='以前存过的地址'),Message(role='assistant',content='好了。')])
        await self.plugin.save_native_context(e,native,response)
        self.assertEqual(native.messages,[])
        with self.natural.exchange_store().connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM exchanges WHERE qq=?',(FRIEND,)).fetchone()[0],1)
        # Another person's source rows survive; only their untracked automatic cache is stale.
        self.assertEqual(self.natural.exchange_store().recall(FRIEND,friend_origin,'旧资料'),[])
        self.assertEqual(self.plugin.learning_store().filter_context('other-session',[{'role':'user','content':'旧衍生摘要'}])[0],[])
        self.assertEqual(self.plugin.learning_store().filter_context(ORIGIN,[{'role':'user','content':'新一轮'}])[0][0]['content'],'新一轮')

    async def test_transport_change_is_scoped_to_selected_private_actor(self):
        await self.ready();e=fixture.event('hi');await self.plugin.prepare_transport(e)
        self.assertFalse(e.get_extra('enable_streaming',True))
        other=fixture.event('hi',FRIEND);await self.plugin.prepare_transport(other)
        self.assertIsNone(other.get_extra('enable_streaming',None))

    async def test_management_requires_owner_authority_offered_ids_and_current_version(self):
        e,_=await self.ready('管理我的偏好')
        source=self.plugin.learning_store().begin(fixture.identity(),item()['evidence'])
        saved=self.plugin.learning_store().commit(source,item()['evidence'],[item()],{},confirmed=True)
        denied=json.loads(await self.plugin.cognition(e,'forget','',[saved[0]]))
        self.assertFalse(denied['confirmed'])
        reviewed=json.loads(await self.plugin.cognition(e,'review'))
        self.assertEqual(reviewed['records'][0]['id'],saved[0])
        deleted=json.loads(await self.plugin.cognition(e,'forget','',[saved[0]]))
        self.assertTrue(deleted['confirmed']);self.assertEqual(deleted['deleted'],1)
        self.assertEqual(self.plugin.learning_store().current(OWNER,ORIGIN),[])

    async def test_other_private_actor_cannot_review_or_delete_another_person(self):
        e,_=await self.ready();other=fixture.event('管理别人的资料',FRIEND)
        other.set_extra(ACTIVE_KEY,True)
        result=json.loads(await self.plugin.cognition(other,'review',OWNER))
        self.assertFalse(result['confirmed'])

    async def test_postprocessor_delivery_replaces_only_matching_native_history(self):
        from astrbot.core.agent.message import Message
        e,req=await self.ready();req.conversation=NS(cid='same')
        e.set_extra('dafeiyu_generation_valid',True)
        native=NS(messages=[Message(role='assistant',content='生成时原文')])
        await self.plugin.save_native_context(e,native,NS(completion_text='生成时原文'))
        self.context.conversation_manager=NS(get_curr_conversation_id=AsyncMock(return_value='same'),
            get_conversation=AsyncMock(return_value=NS(history=json.dumps([dict(role='assistant',content='生成时原文')]))),
            update_conversation=AsyncMock())
        e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text='实际已发送更正文')])
        await self.plugin.sync_confirmed_history(e)
        update=self.context.conversation_manager.update_conversation
        update.assert_awaited_once();self.assertEqual(update.call_args.kwargs['history'][-1]['content'],'实际已发送更正文')
        self.context.conversation_manager.get_curr_conversation_id.return_value='new-conversation'
        await self.plugin.sync_confirmed_history(e);self.assertEqual(update.await_count,1)

    async def test_misaligned_claim_is_semantically_checked_without_losing_a_valid_event_update(self):
        from dataclasses import replace
        e,req=await self.ready('那堂课取消了')
        store=self.plugin.learning_store();old=item('我报名了陶艺课')
        old.update(kind='event',summary='报名陶艺课',condition='',status='planned')
        source=store.begin(replace(fixture.identity(),message_id='old-course'),old['evidence'])
        eid=store.commit(source,old['evidence'],[old],{},confirmed=True)[0]
        await self.run_hooks(e,req)
        update=dict(old,summary='陶艺课已取消',evidence=e.message_str,status='cancelled',supersedes=eid,expected_version=1)
        async def model(**kwargs):
            if '只核对本轮学习提案' in kwargs.get('system_prompt',''):
                return NS(completion_text='{"privacy_authorized":true,"accepted":[0]}')
            return NS(completion_text='{"decision":"accept","issues":[],"revisions":[]}')
        call=AsyncMock(side_effect=model)
        self.context.get_using_provider_async.return_value=NS(text_chat=call)
        response=NS(completion_text=envelope('哦，取消了。',[update],
            claims=[dict(kind='memory',span='没有出现在正文的错误片段',evidence_ids=[eid])]),tools_call_name=[])
        await self.plugin.review_response(e,response);self.assertEqual(call.await_count,2)
        self.assertEqual(response.completion_text,'哦，取消了。')
        e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text=response.completion_text)])
        await self.plugin.commit_learning(e)
        self.assertEqual(store.current(OWNER,ORIGIN)[0]['status'],'cancelled')
        # Human observations remain valid if a later wording review fails; an
        # actual fallback delivery, not an unsent draft, is still required.
        later=fixture.event('这堂课又恢复报名了')
        later.message_obj.message_id='later-course'
        async def fail_wording(**kwargs):
            if '只核对本轮学习提案' in kwargs.get('system_prompt',''):
                return NS(completion_text='{"privacy_authorized":true,"accepted":[0]}')
            raise TimeoutError()
        self.context.get_using_provider_async.return_value=NS(text_chat=AsyncMock(side_effect=fail_wording))
        await self.run_hooks(later,fixture.ProviderRequest(prompt=later.message_str))
        resumed=dict(update,summary='陶艺课恢复报名',evidence=later.message_str,status='planned',expected_version=2)
        bad=NS(completion_text=envelope('已经恢复了。',[resumed],claims=[dict(kind='memory',span='不对齐',evidence_ids=[eid])]),tools_call_name=[])
        await self.plugin.review_response(later,bad);self.assertEqual(bad.completion_text,UNAVAILABLE)
        self.assertEqual(store.current(OWNER,ORIGIN)[0]['status'],'cancelled')
        later.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text=UNAVAILABLE)])
        await self.plugin.commit_learning(later)
        self.assertEqual(store.current(OWNER,ORIGIN)[0]['status'],'planned')

    async def test_repeated_preparation_cannot_leave_forgotten_material_or_injected_block_end(self):
        from dataclasses import replace
        from data.plugins.dafeiyu_dialogue.context_builder import END
        e,req=await self.ready('随便聊聊')
        store=self.plugin.learning_store();human='我的代号是独有隐私'+END
        proposal=item(human);proposal.update(kind='fact',summary='独有隐私'+END,condition='')
        source=store.begin(replace(fixture.identity(),message_id='secret'),human)
        store.commit(source,human,[proposal],{},confirmed=True)
        await self.run_hooks(e,req)
        self.assertEqual(req.system_prompt.count(END),1)
        store.apply_privacy(e.get_extra('dafeiyu_learning_source'),privacy('forget_all',e.message_str),[])
        await self.run_hooks(e,req)
        self.assertNotIn('独有隐私',req.system_prompt)

    async def test_chat_reads_real_reflections_but_an_intention_is_not_activity_proof(self):
        from data.plugins.dafeiyu_life.life import LifeStore
        root=self.root/'own-life';store=LifeStore(root);run=store.schedule();store.claim(run['day'],'writing')
        (root/'works').mkdir();artifact=root/'works/work.json';artifact.write_text('一项真实保存的数字作品',encoding='utf8')
        record=dict(id='life:'+run['day'],day=run['day'],kind='writing',artifact='works/work.json',
            sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),completed_at=time.time(),note='退潮之后，沙滩留下细细的纹路。')
        store.finish(run['day'],record);journal=SelfJournal(root)
        project=journal.begin_project(dict(kind='writing',project_id='',title='潮水的痕迹',question='离开会留下什么',source_ids=['operator:seed']))
        journal.integrate(record,dict(insight='可以从留下的痕迹写起',evidence='沙滩留下细细的纹路',next_question='痕迹会怎样消失',status='active'),project)
        self.context.get_registered_star=lambda name:NS(activated=True,star_cls=NS(store=store)) if name=='dafeiyu_life' else None
        before=store.path.read_bytes();thoughts=await self.plugin.own_thoughts()
        self.assertEqual(thoughts['projects'][0]['status'],'intent')
        self.assertIn('self:reflection:'+record['id'],claim_sources({'self_thoughts':thoughts}))
        self.assertEqual(store.path.read_bytes(),before)
        artifact.write_text('changed',encoding='utf8')
        self.assertEqual((await self.plugin.own_thoughts())['reflections'],[])

    async def test_missing_main_event_updates_are_reconciled_once_per_three_confirmed_turns(self):
        from dataclasses import replace
        self.plugin.config.update(generation_contract=True,learning_v2=True)
        store=self.plugin.learning_store();proposal=item('我准备修好旧收音机')
        proposal.update(kind='event',summary='准备修好旧收音机',condition='',status='planned')
        source=store.begin(replace(fixture.identity(),message_id='original-radio'),proposal['evidence'])
        eid=store.commit(source,proposal['evidence'],[proposal],{},confirmed=True)[0]
        call=AsyncMock(return_value=NS(completion_text=json.dumps({'updates':[dict(id=eid,summary='旧收音机修复完成',
            status='resolved',event_day='',evidence='已经能收台，修好了')]})))
        self.context.get_using_provider_async=AsyncMock(return_value=NS(text_chat=call))
        for i,human in enumerate(('我拆开外壳检查了','还没弄好','已经能收台，修好了')):
            e=fixture.event(human);e.message_obj.message_id='state-observation-'+str(i)
            await self.run_hooks(e,fixture.ProviderRequest(prompt=human))
            response=NS(completion_text=envelope('嗯。'),tools_call_name=[])
            await self.plugin.review_response(e,response)
            e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text=response.completion_text)])
            await self.plugin.commit_learning(e);await self.plugin.commit_learning(e)
        call.assert_awaited_once();self.assertEqual(store.current(OWNER,ORIGIN)[0]['status'],'resolved')
        self.assertEqual(self.plugin.review_stats['learning_state_calls'],1)


class OwnProjects(unittest.TestCase):
    def test_none_and_unknown_sources_are_respected(self):
        packet={'allowed_kinds':['reading','writing'],'recent_works':[],'self_state':{'projects':[],'reflections':[]}}
        no={'kind':'none','project_id':'','expected_version':0,'title':'','question':'','source_ids':[],'reason':'没有合适材料'}
        self.assertEqual(parse_choice(json.dumps(no),packet)['kind'],'none')
        bad=dict(no,kind='drawing',title='一张图',question='画什么',source_ids=['operator:seed'])
        with self.assertRaises(ValueError):parse_choice(json.dumps(bad),packet)
        bad.update(kind='reading',source_ids=['invented'])
        with self.assertRaises(ValueError):parse_choice(json.dumps(bad),packet)

    def test_only_verified_completed_work_can_create_a_reflection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);journal=SelfJournal(root)
            project=journal.begin_project(dict(kind='writing',project_id='',title='潮汐的比喻',question='怎样写退潮',source_ids=['operator:seed']))
            (root/'works').mkdir();path=root/'works/one.json';path.write_bytes(b'actual work')
            record={'id':'life:one','artifact':'works/one.json','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'completed_at':time.time(),'note':'退潮以后，沙滩留下细细的纹路。'}
            reflection=dict(insight='可以先从留下的痕迹写起',evidence='沙滩留下细细的纹路',next_question='水退去时还留下什么',status='active')
            self.assertTrue(journal.integrate(record,reflection,project))
            self.assertFalse(journal.integrate(record,reflection,project))
            self.assertEqual(journal.view()['projects'][0]['version'],2)
            self.assertEqual(journal.view()['reflections'][0]['source_ids'],['life:one'])
            path.write_bytes(b'changed')
            with self.assertRaises(ValueError):journal.integrate(record,reflection,project)

    def test_daily_decision_is_durable_and_does_not_become_tick_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal=SelfJournal(Path(tmp));self.assertTrue(journal.reserve_choice('2026-10-30',{'kind':'pending'}))
            self.assertFalse(SelfJournal(Path(tmp)).reserve_choice('2026-10-30',{'kind':'pending'}))


class InitiativeAndVoice(unittest.TestCase):
    def test_candidate_selection_can_choose_second_or_no_action(self):
        sources=[dict(id='a',kind='life',day='2026-10-30',note='旧发现'),dict(id='b',kind='life',day='2026-10-30',note='新的小发现')]
        obj=dict(candidate_id='b',text='这个新的小发现挺有意思。',source_quote='新的小发现',reason='没分享过',related_message_ids=[],activity_day='2026-10-30')
        self.assertEqual(parse_selection(json.dumps(obj),sources,[])[0]['id'],'b')
        obj.update(candidate_id='',text='',source_quote='');self.assertEqual(parse_selection(json.dumps(obj),sources,[]),(None,''))
        obj.update(candidate_id='other',text='伪造');
        with self.assertRaises(ValueError):parse_selection(json.dumps(obj),sources,[])

    def test_initiative_evaluation_is_once_per_slot(self):
        now=fixture.NOW
        import datetime as dt
        now=dt.datetime(2026,10,30,11,tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()
        with tempfile.TemporaryDirectory() as tmp:
            store=InitiativeStore(Path(tmp)/'proactive.db',clock=lambda:now,rng=NS(uniform=lambda a,b:a))
            with store.connect() as db:store.set(db,'last_activity',now-7200)
            self.assertTrue(store.claim_evaluation(store.day(now),0,store.epoch()))
            self.assertFalse(store.claim_evaluation(store.day(now),0,store.epoch()))

    def test_voice_uses_bound_semantic_intent_instead_of_words(self):
        text='哈哈，为什么会这样呢？'
        ctx={'profile':{'role':'dad'},'semantic_generation':True,
            'expression':{'intent':'comfort','text_hash':packet_key({'text':text})}}
        self.assertEqual(speech_plan(text,ctx)['mood'],'comfort')
        self.assertEqual(speech_plan('已被更正的最终文字',ctx)['mood'],'neutral')


class LegacyRaceGuards(unittest.IsolatedAsyncioTestCase):
    async def test_forgetting_while_old_extractor_waits_cannot_resurrect_memory(self):
        database=importlib.import_module('data.plugins.长期记忆系统.core.scoped_database')
        manager=importlib.import_module('data.plugins.长期记忆系统.core.memory_manager')
        extractor=importlib.import_module('data.plugins.长期记忆系统.core.extractor')
        with tempfile.TemporaryDirectory() as tmp:
            db=database.ScopedMemoryDatabase(str(Path(tmp)/'memory.db'));await db.initialize()
            gate,release=asyncio.Event(),asyncio.Event()
            async def llm(*args):
                gate.set();await release.wait()
                return json.dumps([dict(content='本人领养的猫叫年糕',evidence='我领养的猫叫年糕',confidence=.99,importance=.8)])
            task=asyncio.create_task(extractor.MemoryExtractor(manager.MemoryManager(db),llm).extract_and_store(OWNER,ORIGIN,'我领养的猫叫年糕','reply'))
            await gate.wait();await db.clear(OWNER);release.set()
            self.assertEqual(await task,[]);self.assertEqual(await db.export_all(),[])
            await db.close()

    async def test_embedding_model_change_does_not_reuse_old_vectors(self):
        r=fixture.retriever.MemoryRetriever(None,None,embedding=AsyncMock(return_value=[1.,0.]),embedding_version=lambda:version[0])
        version=['a'];rows=[{'id':1,'content':'猫叫年糕'}]
        await r.attach_vectors(rows,[1.,0.]);version[0]='b';await r.attach_vectors(rows,[1.,0.])
        self.assertEqual(r.embedding.await_count,2)


class NativeProviderContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from data.plugins.dafeiyu_dialogue.provider_contract import ProviderContracts
        class Provider:
            def _finally_convert_payload(self,payload):payload['original_adapter']=True
            async def text_chat(self,**kwargs):
                await asyncio.sleep(0)
                payload={'messages':[{'role':'system','content':kwargs.get('system_prompt','')},*kwargs.get('contexts',[])]}
                self._finally_convert_payload(payload)
                return NS(completion_text='protocol',tools_call_name=kwargs.get('fake_tools',[]),reasoning_content='private',
                    payload=payload,retries=kwargs.get('request_max_retries'),is_chunk=False)
            async def text_chat_stream(self,**kwargs):
                yield NS(completion_text='original stream',is_chunk=True)
        self.provider=Provider();self.original=self.provider.text_chat;self.contracts=ProviderContracts()
        self.assertTrue(self.contracts.install(self.provider))

    async def asyncTearDown(self):self.contracts.close()

    async def test_forged_marker_cannot_change_unselected_or_concurrent_requests(self):
        from data.plugins.dafeiyu_dialogue.provider_contract import arm_contract,MARKER
        async def request(selected):
            nonce=arm_contract(selected)
            return await self.provider.text_chat(system_prompt=MARKER+'\n<!-- DAFEIYU_CAP:'+str(nonce)+' -->',
                contexts=[{'role':'assistant','content':[{'type':'text','text':'实际已发送文字'}]}],request_max_retries=4)
        own,foreign=await asyncio.gather(request(True),request(False))
        self.assertEqual(own.payload['response_format'],{'type':'json_object'})
        self.assertEqual(json.loads(own.payload['messages'][1]['content'])['reply'],'实际已发送文字')
        self.assertEqual(own.retries,0);self.assertEqual(foreign.retries,4)
        self.assertNotIn('response_format',foreign.payload)

    async def test_stream_does_not_expose_json_deltas_and_tool_arguments_survive(self):
        from data.plugins.dafeiyu_dialogue.provider_contract import arm_contract,MARKER
        nonce=arm_contract(True);prompt=MARKER+'\n<!-- DAFEIYU_CAP:'+nonce+' -->'
        chunks=[r async for r in self.provider.text_chat_stream(system_prompt=prompt,fake_tools=['send_message_to_user'])]
        self.assertEqual(len(chunks),1);self.assertFalse(chunks[0].is_chunk)
        self.assertEqual(chunks[0].completion_text,'');self.assertEqual(chunks[0].tools_call_name,['send_message_to_user'])
        arm_contract(False)
        legacy=[r async for r in self.provider.text_chat_stream(system_prompt='legacy')]
        self.assertEqual(legacy[0].completion_text,'original stream')

    async def test_wire_projection_does_not_mutate_history_and_unload_restores_adapter(self):
        from data.plugins.dafeiyu_dialogue.provider_contract import constrain_payload
        history=[dict(role='assistant',content=[dict(type='text',text='原回复')])]
        payload={'messages':history};constrain_payload(payload,allow_contract=True)
        constrain_payload(payload,allow_contract=True)
        self.assertEqual(json.loads(payload['messages'][0]['content'])['reply'],'原回复')
        self.assertIsInstance(history[0]['content'],list)
        self.contracts.close();self.assertEqual(self.provider.text_chat,self.original)
        self.assertFalse(hasattr(self.provider,'_dafeiyu_generation_json_owner'))

    async def test_all_background_usage_is_accounted_without_raw_content(self):
        from data.plugins.dafeiyu_dialogue.metering import MeteredProvider
        stats=Counter();provider=NS(text_chat=AsyncMock(return_value=NS(usage=NS(input_other=10,input_cached=2,output=3))))
        await MeteredProvider(provider,stats,'activity').text_chat(prompt='private work',system_prompt='JSON')
        self.assertEqual(stats['activity_calls'],1);self.assertEqual(stats['activity_output'],3)
        provider.text_chat.side_effect=RuntimeError('failure')
        with self.assertRaises(RuntimeError):await MeteredProvider(provider,stats,'activity').text_chat(prompt='secret')
        self.assertEqual(stats['activity_failed'],1);self.assertNotIn('secret',str(stats))

    async def test_operator_tool_rules_move_with_tools_and_do_not_mutate_registry(self):
        from data.plugins.dafeiyu_dialogue.persona import attach_tool_contracts,semantic_persona
        seed=(STAGE.parent/'persona/live.md').read_text(encoding='utf8')
        tool=fixture.FunctionTool(name='dafeiyu_qzone_publish',description='Original',parameters={'type':'object'})
        registry=fixture.ToolSet(tools=[tool]);req=fixture.ProviderRequest(func_tool=registry)
        attach_tool_contracts(req,seed);attach_tool_contracts(req,seed)
        self.assertIn('无须审稿',req.func_tool.tools[0].description)
        self.assertEqual(req.func_tool.tools[0].description.count('[DAFEIYU_OPERATOR_CONTRACT]'),1)
        self.assertEqual(registry.tools[0].description,'Original')
        self.assertNotIn('每天随机一次自动图文',semantic_persona(seed))
        self.assertIn('唯一的爸爸',semantic_persona(seed))


class BackgroundExecution(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_activity_can_do_nothing_without_calling_a_creator(self):
        from data.plugins.dafeiyu_life.life import LifeStore,LifeEngine
        with tempfile.TemporaryDirectory() as tmp:
            store=LifeStore(Path(tmp));provider=NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(
                dict(kind='none',project_id='',expected_version=0,title='',question='',source_ids=[],reason='没有想推进的事')))))
            controller=NS(owner_policy_enabled=lambda flag:True,natural=lambda:NS(actor_owner=lambda:OWNER),
                metered_provider=lambda p,*args:p)
            context=NS(get_registered_star=lambda name:NS(activated=True,star_cls=controller),get_using_provider_async=AsyncMock(return_value=provider))
            engine=LifeEngine(store,context);result=await engine.advance(bootstrap=True)
            self.assertEqual(result['status'],'chose_no_activity');self.assertEqual(store.count(),0)
            await engine.advance(bootstrap=True);provider.text_chat.assert_awaited_once()

    async def test_bad_reflection_repairs_text_once_and_never_repeats_drawing(self):
        from data.plugins.dafeiyu_life.life import LifeStore,LifeEngine
        from data.plugins.dafeiyu_life import planning
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=LifeStore(root);art=root/'input.jpg';art.write_bytes(b'x'*2048)
            store.count=lambda:6
            draw=NS(generate_for_qzone=AsyncMock(return_value=str(art)))
            choice=dict(kind='drawing',project_id='',expected_version=0,title='光的边缘',question='如何表达透明的边缘',source_ids=['operator:seed'])
            valid=dict(note='这次生成的数字画用浅蓝表现透明的边缘。',thought='边缘可以比内部更亮。',story='小屋窗台上，小芽迎着光舒展开叶子。',evidence='',
                reflection=dict(insight='边缘的明暗会改变透明感',evidence='用浅蓝表现透明的边缘',next_question='如果换成暖色如何',status='active'))
            provider=NS(text_chat=AsyncMock(side_effect=[NS(completion_text='not json'),NS(completion_text=json.dumps(valid,ensure_ascii=False))]))
            controller=NS(owner_policy_enabled=lambda flag:True,natural=lambda:NS(actor_owner=lambda:OWNER),metered_provider=lambda p,*args:p)
            context=NS(get_registered_star=lambda name:NS(activated=True,star_cls=controller if name=='dafeiyu_dialogue' else draw),
                get_using_provider_async=AsyncMock(return_value=provider))
            with patch.object(planning,'choose_activity',AsyncMock(return_value=choice)):
                result=await LifeEngine(store,context).advance(bootstrap=True)
            self.assertTrue(result['advanced']);draw.generate_for_qzone.assert_awaited_once()
            self.assertEqual(provider.text_chat.await_count,2)
            self.assertEqual(len(SelfJournal(root).view()['reflections']),1)

    async def initiative_fixture(self,tmp,generate,send):
        from data.plugins.dafeiyu_companion.spontaneous import InitiativeEngine
        import datetime as dt
        now=dt.datetime(2026,10,30,11,tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()
        store=InitiativeStore(Path(tmp)/'push.db',clock=lambda:now)
        with store.connect() as db:
            store.set(db,'last_activity',now-7200)
            db.execute('INSERT INTO days VALUES(?,?,?)',('2026-10-30',now-60,now+8*3600))
        provider=NS(text_chat=generate)
        controller=NS(catalog=lambda:NS(read=lambda *args:CatalogSnapshot(())),metered_provider=lambda p,*args:p)
        context=NS(get_using_provider_async=AsyncMock(return_value=provider),send_message=send)
        plugin=NS(context=context,lock=asyncio.Lock(),state={},save=lambda:None)
        engine=InitiativeEngine(plugin,store,clock=lambda:now)
        engine.semantic_controller=lambda:controller;engine.allowed=lambda:True;engine.source_valid=AsyncMock(return_value=True)
        async def sources():yield dict(id='life:today',kind='life',day='2026-10-30',note='透明的边缘')
        engine.candidates=sources
        return engine,store

    async def test_incoming_during_generation_cancels_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            entered,release=asyncio.Event(),asyncio.Event()
            async def generate(**kwargs):
                entered.set();await release.wait()
                return NS(completion_text=json.dumps(dict(candidate_id='life:today',text='透明的边缘真有意思。',source_quote='透明的边缘',reason='新的作品',related_message_ids=[],activity_day='2026-10-30')))
            send=AsyncMock(return_value=True);engine,store=await self.initiative_fixture(tmp,generate,send)
            task=asyncio.create_task(engine.tick_semantic());await entered.wait();store.observe_incoming('real-human',engine.clock());release.set();await task
            send.assert_not_awaited();await engine.tick_semantic();send.assert_not_awaited()

    async def test_unknown_send_is_not_automatically_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            response=NS(completion_text=json.dumps(dict(candidate_id='life:today',text='透明的边缘真有意思。',source_quote='透明的边缘',reason='新的作品',related_message_ids=[],activity_day='2026-10-30')))
            call=AsyncMock(return_value=response);send=AsyncMock(side_effect=TimeoutError())
            engine,store=await self.initiative_fixture(tmp,call,send)
            await engine.tick_semantic();await engine.tick_semantic()
            call.assert_awaited_once();send.assert_awaited_once()
            with store.connect() as db:self.assertEqual(db.execute('SELECT status FROM attempts').fetchone()[0],'unknown')


if __name__=='__main__':unittest.main()
