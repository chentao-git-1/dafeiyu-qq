"""Source-grounded relationship revisions, privacy and native delivery; no real models or sends."""
import copy
from dataclasses import replace
import gc
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch

import test_cognition as base
import test_social_followthrough as social
from dafeiyu.dialogue.contracts import TurnContext
from dafeiyu.dialogue.generation import parse_generation
from dafeiyu.dialogue.learning import LearningStore
from dafeiyu.dialogue.relationships import RelationshipMemory,proposals
from dafeiyu.media.voice.expression import relationship,speech_plan
from data.plugins.dafeiyu_dialogue.provider_contract import isolate_background,MARKER
from data.plugins.dafeiyu_bailian_tts.main import JingjingBailianTTS

f=base.fixture
OWNER,FRIEND,ORIGIN=base.OWNER,base.FRIEND,base.ORIGIN
HUMAN='刚才是我误会了，抱歉，我愿意听你把理由讲完'
REPLY='谢谢你愿意听，我把刚才没说清的部分接着讲。'


def note(human=HUMAN,**changes):
    return dict(aspect='trust',view='有分歧时愿意重新听解释，让我更愿意坦诚交流',context='目前依据这次澄清，其他事情仍需具体了解',
        evidence=human,subject='speaker',modality='direct',supersedes='',expected_version=0,status='active')|changes


class StoredRelationships(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.addCleanup(gc.collect)
        self.now=time.time();self.learning=LearningStore(Path(self.tmp.name)/'learning.db',clock=lambda:self.now)
        self.store=RelationshipMemory(self.learning);self.mid=0

    def source(self,human=HUMAN,actor=OWNER,origin=ORIGIN):
        self.mid+=1
        return self.learning.begin(TurnContext(actor,origin,'dafeiyu-qq',str(self.mid),self.now),human)

    def saved(self,**changes):
        source=self.source();item=note(**changes)
        keys=self.store.commit(source,HUMAN,[item],{},REPLY,confirmed=True)
        self.assertEqual(len(keys),1);return source,keys[0]

    def test_confirmed_exchange_persists_once_and_reopen_keeps_subjective_context(self):
        source=self.source()
        self.assertEqual(self.store.commit(source,HUMAN,[note()],{},REPLY),[])
        key=self.store.commit(source,HUMAN,[note()],{},REPLY,confirmed=True)[0]
        self.assertEqual(self.store.commit(source,HUMAN,[note()],{},REPLY,confirmed=True),[])
        reopened=RelationshipMemory(LearningStore(self.learning.path))
        self.assertEqual(reopened.current(OWNER,ORIGIN)[0]['id'],key)
        row=reopened.history(OWNER,ORIGIN)[0]
        self.assertEqual(row['quote'],HUMAN);self.assertEqual(row['context'],note()['context'])
        self.assertNotIn('score',row)
        with self.learning.db() as db:
            self.assertEqual(db.execute('SELECT reply FROM relationship_versions').fetchone()[0],REPLY)

    def test_repair_counterexample_and_withdrawal_are_model_selected_revisions(self):
        _,key=self.saved();self.now+=5
        human='我刚才答应听解释，却又打断你了';source=self.source(human)
        revised=note(human,view='愿意道歉，但争论时是否能耐心听完还需要观察',context='这次打断与先前承诺不一致',supersedes=key,expected_version=1)
        self.assertEqual(self.store.commit(source,human,[revised],{key:1},'我们慢一点说。',confirmed=True),[key])
        self.assertEqual(self.store.current(OWNER,ORIGIN)[0]['version'],2)
        self.assertEqual(len(self.store.history(OWNER,ORIGIN)),2)
        self.now+=5;human='刚刚断线了，我不是故意打断你的';source=self.source(human)
        withdrawn=note(human,view='刚才关于不耐心的判断不成立',context='收到新的断线说明，暂不据此判断信任',supersedes=key,expected_version=2,status='withdrawn')
        self.store.commit(source,human,[withdrawn],{key:2},'明白，是我误会了。',confirmed=True)
        self.assertEqual(self.store.current(OWNER,ORIGIN),[])
        self.assertEqual(self.store.history(OWNER,ORIGIN)[0]['status'],'withdrawn')

    def test_peer_group_and_platform_scopes_never_share_views_or_authorize_updates(self):
        _,key=self.saved()
        group='dafeiyu-qq:GroupMessage:7000000101'
        for actor,origin in ((FRIEND,'dafeiyu-qq:FriendMessage:'+FRIEND),(OWNER,group),(OWNER,'other:FriendMessage:'+OWNER)):
            self.assertEqual(self.store.current(actor,origin),[])
        source=self.source(actor=FRIEND,origin='dafeiyu-qq:FriendMessage:'+FRIEND)
        self.assertEqual(self.store.commit(source,HUMAN,[note(supersedes=key,expected_version=1)],{key:1},REPLY,confirmed=True),[])
        source=self.source(origin=group)
        self.assertEqual(self.store.commit(source,HUMAN,[note(supersedes=key,expected_version=1)],{key:1},REPLY,confirmed=True),[])
        self.assertEqual(self.store.current(OWNER,ORIGIN)[0]['version'],1)

    def test_stale_or_unoffered_revision_cannot_overwrite_a_newer_view(self):
        _,key=self.saved()
        first,second=self.source(),self.source()
        change=note(supersedes=key,expected_version=1,view='新的有条件理解')
        self.assertEqual(self.store.commit(first,HUMAN,[change],{},REPLY,confirmed=True),[])
        self.assertEqual(self.store.commit(second,HUMAN,[change],{key:1},REPLY,confirmed=True),[key])
        self.assertEqual(self.store.commit(self.source(),HUMAN,[change],{key:1},REPLY,confirmed=True),[])
        self.assertEqual(self.store.current(OWNER,ORIGIN)[0]['version'],2)

    def test_duplicate_views_and_empty_proposals_do_not_accumulate_familiarity(self):
        self.saved()
        for _ in range(18):
            source=self.source()
            self.assertEqual(self.store.commit(source,HUMAN,[note()],{},REPLY,confirmed=True),[])
        self.assertEqual(self.store.status()['active_notes'],1)
        self.assertEqual(self.store.status()['versions'],1)
        self.assertEqual(self.store.commit(self.source(),HUMAN,[],{},REPLY,confirmed=True),[])

    def test_forged_source_or_quote_and_non_direct_material_are_rejected(self):
        source=self.source()
        for bad in (dict(source,owner=FRIEND),dict(source,origin='dafeiyu-qq:FriendMessage:'+FRIEND)):
            self.assertEqual(self.store.commit(bad,HUMAN,[note()],{},REPLY,confirmed=True),[])
        with self.assertRaises(ValueError):self.store.commit(source,'不是当前原话',[note()],{},REPLY,confirmed=True)
        for changes in (dict(subject='third_person'),dict(modality='quoted'),dict(modality='hypothetical'),dict(evidence='虚构的句子'),dict(expected_version=True)):
            self.assertEqual(proposals([note(**changes)],HUMAN),[])
        self.assertEqual(proposals([note(score=100)],HUMAN),[])

    def test_forget_all_deletes_quotes_versions_and_blocks_late_delivery(self):
        _,key=self.saved();pending=self.source()
        human='忘掉关于我的记忆';source=self.source(human)
        self.learning.apply_privacy(source,base.privacy('forget_all',human),{key})
        self.assertEqual(self.store.current(OWNER,ORIGIN),[]);self.assertEqual(self.store.history(OWNER,ORIGIN),[])
        self.assertEqual(self.store.commit(pending,HUMAN,[note()],{},REPLY,confirmed=True),[])
        with self.learning.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM relationship_versions').fetchone()[0],0)

    def test_selected_learning_source_forget_revokes_derived_relationship_revisions(self):
        source,key=self.saved()
        learned=self.learning.commit(source,HUMAN,[base.item(HUMAN)],{},confirmed=True)[0]
        self.learning.forget_entities(OWNER,{learned:1})
        self.assertEqual(self.store.current(OWNER,ORIGIN),[]);self.assertEqual(self.store.history(OWNER,ORIGIN),[])

    def test_selected_relationship_forget_keeps_other_person_and_other_notes(self):
        _,key=self.saved();_,keep=self.saved(aspect='closeness',view='愿意聊不同观点，交流感觉比较自在')
        other_origin='dafeiyu-qq:FriendMessage:'+FRIEND
        self.store.commit(self.source(actor=FRIEND,origin=other_origin),HUMAN,[note()],{},REPLY,confirmed=True)
        self.learning.forget_entities(OWNER,{key:1})
        self.assertEqual([r['id'] for r in self.store.current(OWNER,ORIGIN)],[keep])
        self.assertEqual(len(self.store.current(FRIEND,other_origin)),1)

    def test_group_forgetting_keeps_private_relationship_and_refusal_blocks_current_turn(self):
        self.saved();group='dafeiyu-qq:GroupMessage:7000000101'
        self.store.commit(self.source(origin=group),HUMAN,[note()],{},REPLY,confirmed=True)
        human='别保存这段';source=self.source(human)
        self.learning.apply_privacy(source,base.privacy('deny_turn',human),[])
        self.assertEqual(self.store.commit(source,human,[note(human)],{},REPLY,confirmed=True),[])
        self.assertEqual(len(self.store.current(OWNER,ORIGIN)),1)
        source=self.source(human,origin=group)
        self.learning.apply_privacy(source,base.privacy('forget_all',human),[])
        self.assertEqual(self.store.current(OWNER,group),[]);self.assertEqual(len(self.store.current(OWNER,ORIGIN)),1)

    def test_schema_upgrade_preserves_existing_learning_sources(self):
        source=self.source();keys=self.learning.commit(source,HUMAN,[base.item(HUMAN)],{},confirmed=True)
        before=self.learning.current(OWNER,ORIGIN)
        reopened=LearningStore(self.learning.path)
        self.assertEqual(reopened.current(OWNER,ORIGIN),before);self.assertEqual(before[0]['id'],keys[0])


class NativeRelationships(unittest.IsolatedAsyncioTestCase):
    run_hooks=f.NativeHooks.run_hooks
    async def asyncSetUp(self):
        await f.NativeHooks.asyncSetUp(self)
        self.plugin.config.update(generation_contract=True,learning_v2=True,relationship_memory_v1=True,
            all_allowed_private=True,chat_model_verification=False)
        self.model=AsyncMock(side_effect=AssertionError('No relationship review or test provider calls'))
        self.context.get_using_provider_async=AsyncMock(return_value=NS(text_chat=self.model))
        self.mid=0

    async def asyncTearDown(self):
        await self.plugin.terminate();await f.NativeHooks.asyncTearDown(self)

    async def prepare(self,human=HUMAN,uid=OWNER,changes=None):
        self.mid+=1;e=f.event(human,uid);e.message_obj.message_id='relation-'+str(self.mid)
        req=f.ProviderRequest(prompt=human,system_prompt='trusted persona')
        await self.run_hooks(e,req)
        obj=json.loads(base.envelope(REPLY));obj['relationship']=[note(human,**(changes or {}))]
        response=NS(completion_text=json.dumps(obj,ensure_ascii=False),tools_call_name=[])
        await self.plugin.review_response(e,response)
        return e,req,response

    async def deliver(self,e,response):
        e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text=response.completion_text,at=time.time())])
        await self.plugin.enqueue_learning(e);await self.plugin.deferred_learning.wait_idle()

    async def test_real_hooks_save_after_delivery_and_next_turn_uses_only_this_person_views(self):
        e,_,response=await self.prepare()
        await self.plugin.commit_learning(e);self.assertEqual(self.plugin.relationship_store().current(OWNER,ORIGIN),[])
        await self.deliver(e,response);await self.plugin.commit_learning(e)
        self.assertEqual(self.plugin.relationship_store().status()['versions'],1)
        _,req,_=await self.prepare('我们接着聊吧')
        self.assertIn(note()['view'],req.system_prompt)
        _,other,_=await self.prepare('我们接着聊吧',FRIEND)
        self.assertNotIn(note()['view'],other.system_prompt)
        self.model.assert_not_awaited();e.send.assert_not_awaited()

    async def test_bad_optional_metadata_does_not_change_reply_or_create_review_call(self):
        e,_,response=await self.prepare();obj=json.loads(base.envelope('正常接着聊。'))
        obj['relationship']={'score':99};obj['delivery']=['invalid']
        response.completion_text=json.dumps(obj);await self.plugin.review_response(e,response)
        self.assertEqual(response.completion_text,'正常接着聊。')
        await self.deliver(e,response);self.assertEqual(self.plugin.relationship_store().status()['versions'],0)
        self.model.assert_not_awaited()

    async def test_partial_delivery_and_invalid_generation_never_seed_relationship(self):
        e,_,response=await self.prepare()
        e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text=REPLY[:4])])
        await self.plugin.commit_learning(e);self.assertEqual(self.plugin.relationship_store().status()['versions'],0)
        e.set_extra('dafeiyu_generation_valid',False)
        e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text=REPLY)])
        await self.plugin.commit_learning(e);self.assertEqual(self.plugin.relationship_store().status()['versions'],0)

    async def test_memory_tools_show_revisions_and_delete_only_reviewed_relationship(self):
        e,_,response=await self.prepare();await self.deliver(e,response)
        state=json.loads(await self.plugin.cognition(e,'review'));row=state['relationship_memory'][0]
        self.assertEqual(row['aspect'],'trust')
        history=json.loads(await self.plugin.personality(e,'history'))
        self.assertEqual(history['relationship_history'][0]['quote'],HUMAN)
        removed=json.loads(await self.plugin.cognition(e,'forget',record_ids=[row['id']]))
        self.assertTrue(removed['confirmed']);self.assertEqual(self.plugin.relationship_store().current(OWNER,ORIGIN),[])
        self.model.assert_not_awaited()

    async def test_disabled_or_sensitive_turn_has_no_relationship_updates(self):
        self.plugin.config['relationship_memory_v1']=False
        e,req,response=await self.prepare();await self.deliver(e,response)
        self.assertNotIn('BEGIN_DAFEIYU_RELATIONSHIP',req.system_prompt)
        self.assertEqual(self.plugin.relationship_store().status()['versions'],0)
        self.plugin.config['relationship_memory_v1']=True
        e,req,response=await self.prepare('密码: synthetic credential');await self.deliver(e,response)
        self.assertNotIn('BEGIN_DAFEIYU_RELATIONSHIP',req.system_prompt)
        self.assertEqual(self.plugin.relationship_store().status()['versions'],0)

    async def test_forget_during_generation_cannot_reintroduce_pending_view(self):
        e,_,response=await self.prepare()
        human='忘掉我的记忆';source=self.plugin.learning_store().begin(replace(f.identity(),message_id='withdraw-relationship'),human)
        self.plugin.learning_store().apply_privacy(source,base.privacy('forget_all',human),[])
        await self.deliver(e,response)
        self.assertEqual(self.plugin.relationship_store().status()['versions'],0)

    async def test_relationship_data_is_removed_from_copied_background_prompt(self):
        _,req,_=await self.prepare()
        payload={'messages':[dict(role='system',content=req.system_prompt)]};isolate_background(payload)
        self.assertNotIn('BEGIN_DAFEIYU_RELATIONSHIP',payload['messages'][0]['content'])
        self.assertNotIn(MARKER,payload['messages'][0]['content'])

    async def test_voice_hook_uses_model_delivery_without_reading_or_writing_old_counts(self):
        plugin=JingjingBailianTTS(self.context)
        plugin.relations_path=self.root/'relations.json';plugin.relations_path.write_text('{"people":{"synthetic":{"turns":9000}}}')
        before=plugin.relations_path.read_bytes();e,req,response=await self.prepare()
        await plugin.relationship_context(e,req)
        self.assertEqual(plugin.relations_path.read_bytes(),before)
        text=response.completion_text;spec=dict(intent='chat',delivery='放松但认真，解释处稍慢',text_hash=base.packet_key({'text':text}))
        self.assertIn(spec['delivery'],speech_plan(text,dict(semantic_generation=True,expression=spec))['instruction'])
        self.assertNotIn(spec['delivery'],speech_plan('另一句',dict(semantic_generation=True,expression=spec))['instruction'])


class CountFreeVoice(unittest.TestCase):
    def test_counts_never_change_identity_or_speech_style(self):
        uid='7000000099';old=dict(turns=90000,days=list(range(31)))
        self.assertEqual(relationship(uid),relationship(uid,old))
        self.assertNotIn('level',relationship(uid,old))
        self.assertEqual(relationship(OWNER)['role'],'dad')
        text='我们慢慢聊';spec=dict(intent='comfort',text_hash=base.packet_key({'text':text}))
        plans=[speech_plan(text,dict(profile=dict(role=role),semantic_generation=True,expression=spec)) for role in ('dad','friend')]
        self.assertEqual(plans[0]['instruction'],plans[1]['instruction'])


class RelationshipFollowthrough(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=social.SocialIntegration.asyncSetUp
    asyncTearDown=social.SocialIntegration.asyncTearDown
    row=social.SocialIntegration.row

    async def test_same_contact_memory_can_inform_private_contact_without_extra_model_generation(self):
        controller=self.engine.active('dafeiyu_dialogue')
        controller.relationship_profile=AsyncMock(return_value=[dict(id='private-note',view='这个人愿意接着聊具体的话题')])
        self.provider.text_chat.return_value=NS(completion_text=json.dumps(social.decision(self.now,public='接住你的邀请。')))
        await self.engine.handle_comment(self.post,self.comment,self.provider)
        packet=json.loads(self.provider.text_chat.await_args.kwargs['prompt'])
        self.assertTrue(packet['private_context']['relationship_memory'])
        self.assertNotIn('relationship_memory',packet['public_material'])
        await self.engine.tick()
        self.provider.text_chat.assert_awaited_once();self.context.send_message.assert_awaited_once()
        turns=[call.args[0] for call in controller.relationship_profile.await_args_list]
        self.assertTrue(all(turn.actor==social.PEER and turn.origin==self.engine.origin(social.PEER) for turn in turns))


if __name__=='__main__':unittest.main()
