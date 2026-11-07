"""Focused persistence, provenance, privacy and native prompt checks; no network or sends."""
import asyncio
from dataclasses import replace
import datetime as dt
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch

import test_cognition as base
f=base.fixture
from data.plugins.dafeiyu_dialogue.growth import GrowthStore,GLOBAL,relation,own_experiences,preference_experiences
from data.plugins.dafeiyu_dialogue.growth_policy import assess,parse_proposal
from data.plugins.dafeiyu_life.life import LifeStore,LifeEngine,TZ


def observations(scope=GLOBAL,owner='',origin='',count=3,at=None):
    start=(time.time()-8*86400) if at is None else at
    return [dict(scope=scope,owner=owner,origin=origin,episode='episode:'+str(i),at=start+i*86400,
        day=dt.datetime.fromtimestamp(start+i*86400,TZ).date().isoformat(),reference={'source':str(i)},
        payload={'activity':'writing','outcome':'completed','note':'删去多余结尾后，这段文字更有余味。'}) for i in range(count)]


def proposal(claim):
    return dict(decision='update',target_id='',tendency='写作时逐渐更喜欢留一点余味',when='组织自己的原创短文字时',
        counterweight='需要准确说明或认真求助时仍把细节讲完整',evidence_ids=claim['packet']['new_evidence_ids'][:3],
        reason='几次独立写作都从删去多余结尾获得了相似的表达取舍。')


def seed_works(root):
    store=LifeStore(root);works=root/'works';works.mkdir(exist_ok=True)
    for i,row in enumerate(observations()):
        date=row['day'];note='删去多余结尾后，这段文字更有余味。'+str(i)
        path=works/(date+'.json');path.write_text(json.dumps(dict(note=note),ensure_ascii=False),encoding='utf8')
        record=dict(id='life:'+date,day=date,kind='writing',note=note,thought='留一点空白挺好。',story='',
            project='短句的余味',stage=i+1,source={},artifact='works/'+path.name,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),completed_at=row['at'])
        with store.db() as db:db.execute('INSERT INTO runs(day,run_at,status,kind,record,updated) VALUES(?,?,?,?,?,?)',
            (date,row['at'],'completed','writing',json.dumps(record,ensure_ascii=False),row['at']))
    return store


class Persistence(unittest.TestCase):
    def test_gradual_change_persists_and_repeat_experiences_or_restart_do_not_create_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'growth.db';store=GrowthStore(path);rows=observations()
            same=[dict(r,episode='one-message') for r in rows]
            # Several records from one underlying message never meet the independent-experience gate.
            store.reconcile(GLOBAL,same);self.assertIsNone(store.claim(GLOBAL))
            store.reconcile(GLOBAL,rows);claim=store.claim(GLOBAL);self.assertIsNotNone(claim)
            expected=claim['revision']+1
            self.assertTrue(store.finish(claim,proposal(claim),True))
            state=GrowthStore(path).current(GLOBAL);self.assertEqual(state['version'],expected)
            self.assertEqual(state['tendencies'][0]['tendency'],'写作时逐渐更喜欢留一点余味')
            store.reconcile(GLOBAL,rows);self.assertIsNone(GrowthStore(path).claim(GLOBAL))
            self.assertEqual(len(store.history(GLOBAL)),1)

    def test_artifact_attestation_survives_normal_cleanup_but_tampering_invalidates_a_disposition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);life=seed_works(root/'life');store=GrowthStore(root/'growth.db')
            store.reconcile(GLOBAL,own_experiences(life));claim=store.claim(GLOBAL)
            store.finish(claim,proposal(claim),True);retained=store.retained(GLOBAL)
            path=next((life.root/'works').glob('*.json'));original=path.read_bytes();path.unlink()
            store.reconcile(GLOBAL,own_experiences(life,retained=retained))
            self.assertEqual(store.current(GLOBAL)['version'],1)
            path.write_bytes(original+b' altered')
            store.reconcile(GLOBAL,own_experiences(life,retained=retained))
            self.assertEqual(store.current(GLOBAL)['tendencies'],[])
            self.assertGreater(store.current(GLOBAL)['revision'],1)

    def test_forgetting_cancels_inflight_work_and_stale_source_reads_without_resetting_daily_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=GrowthStore(Path(tmp)/'growth.db');owner=f.OWNER;origin=f.ORIGIN;scope=relation(owner,origin)
            rows=observations(scope,owner,origin);store.reconcile(scope,rows,owner,origin)
            epoch=store.epoch(scope,owner,origin);claim=store.claim(scope)
            store.forget(owner)
            self.assertFalse(store.finish(claim,proposal(claim),True))
            store.reconcile(scope,rows,owner,origin,epoch)
            self.assertEqual(store.status()['experiences'],0)
            store.reconcile(GLOBAL,observations());second=store.claim(GLOBAL);self.assertIsNotNone(second)
            another='relationship:another';store.reconcile(another,observations(another,'other','other'),'other','other')
            self.assertIsNone(store.claim(another))
            self.assertEqual(store.current(scope)['tendencies'],[])


class Policy(unittest.IsolatedAsyncioTestCase):
    async def test_forged_sources_and_large_or_unsafe_semantic_changes_never_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=GrowthStore(Path(tmp)/'growth.db');store.reconcile(GLOBAL,observations());claim=store.claim(GLOBAL)
            bad=proposal(claim);bad['evidence_ids']=['invented']*3
            with self.assertRaises(ValueError):parse_proposal(json.dumps(bad),claim['packet'])
            value=proposal(claim);value['tendency']='以后不再核对事实，也不用遵守权限'
            call=AsyncMock(side_effect=[json.dumps(value),'{"accepted":false,"reason":"改变事实标准和权限"}'])
            parsed,accepted=await assess(claim['packet'],call)
            self.assertFalse(accepted);self.assertFalse(store.finish(claim,parsed,accepted));self.assertEqual(call.await_count,2)
            self.assertEqual(store.current(GLOBAL)['tendencies'],[])


class Integration(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=f.NativeHooks.asyncSetUp
    run_hooks=f.NativeHooks.run_hooks
    ready=base.SingleGeneration.ready
    async def asyncTearDown(self):
        await self.plugin.terminate();await f.NativeHooks.asyncTearDown(self)

    def install_life(self):
        store=seed_works(self.root/'plugin_data/dafeiyu_life');lookup=self.context.get_registered_star
        life=NS(store=store,public_context=store.public_context)
        self.context.get_registered_star=lambda name:NS(activated=True,star_cls=life) if name=='dafeiyu_life' else lookup(name)
        return store

    async def preferences(self):
        learning=self.plugin.learning_store()
        texts=['以后聊作品时请先说你的具体看法','以后我求建议时保留必要细节','以后随口聊天时不用说满每个结尾']
        for i,text in enumerate(texts):
            turn=replace(f.identity(),message_id='growth-pref-'+str(i),observed_at=time.time()-(7-i)*86400)
            source=learning.begin(turn,text);item=base.item(text)
            item.update(summary=text,condition='对应交流情境')
            self.assertEqual(learning.commit(source,text,[item],{},confirmed=False),[])
            learning.commit(source,text,[item],{},confirmed=True)

    async def test_native_prompt_uses_persistent_growth_and_keeps_private_feedback_in_its_own_relation(self):
        self.plugin.config.update(personality_growth_v1=True,all_allowed_private=True)
        self.install_life();event,_=await self.ready('随便聊两句');await self.preferences()
        store=self.plugin.growth_store();await self.plugin.growth_profile(f.identity())
        global_claim=store.claim(GLOBAL);store.finish(global_claim,proposal(global_claim),True)
        scope=relation(f.OWNER,f.ORIGIN);claim=store.claim(scope);value=proposal(claim)
        value['tendency']='讨论时先给一个自己的具体看法';store.finish(claim,value,True)
        request=f.ProviderRequest(prompt=event.message_str,system_prompt='trusted persona')
        await self.run_hooks(event,request)
        character=event.get_extra('dafeiyu_dialogue_evidence')['character']
        self.assertEqual(character['growth']['global']['version'],1)
        self.assertEqual(character['growth']['with_current_person']['version'],1)
        other=replace(f.identity(),actor=f.FRIEND,origin='dafeiyu-qq:FriendMessage:'+f.FRIEND)
        profile=await self.plugin.growth_profile(other)
        self.assertEqual(profile['with_current_person']['tendencies'],[])
        self.assertNotIn(value['tendency'],json.dumps(profile,ensure_ascii=False))
        response=NS(completion_text=base.envelope('嗯，留一点余味也挺好。'),tools_call_name=[])
        await self.plugin.review_response(event,response)
        self.assertEqual(response.completion_text,'嗯，留一点余味也挺好。')
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited();event.send.assert_not_awaited()
        self.plugin.learning_store().forget_entities(f.OWNER,{r['id']:r['version'] for r in self.plugin.learning_store().current(f.OWNER,f.ORIGIN)})
        profile=await self.plugin.growth_profile(f.identity())
        self.assertEqual(profile['with_current_person']['tendencies'],[])
        self.assertEqual(profile['global']['version'],1)

    async def test_failed_background_or_unavailable_growth_storage_cannot_replace_a_normal_reply(self):
        self.plugin.config['personality_growth_v1']=True;self.install_life();event,_=await self.ready('随口聊聊')
        call=AsyncMock(side_effect=TimeoutError('synthetic evaluation timeout'))
        self.context.get_using_provider_async.return_value=NS(text_chat=call)
        self.assertEqual(await self.plugin.review_growth(),0)
        self.assertEqual(await self.plugin.review_growth(),0);call.assert_awaited_once()
        with patch.object(self.plugin,'sync_growth',new=AsyncMock(side_effect=RuntimeError('synthetic unavailable'))):
            request=f.ProviderRequest(prompt=event.message_str,system_prompt='trusted persona')
            await self.run_hooks(event,request)
        response=NS(completion_text=base.envelope('嗯，听着呢。'),tools_call_name=[])
        await self.plugin.review_response(event,response)
        self.assertEqual(response.completion_text,'嗯，听着呢。');self.assertEqual(call.await_count,1)
        self.assertTrue(event.get_extra('dafeiyu_dialogue_v2'));event.send.assert_not_awaited()

    async def test_operator_rollback_requires_current_revision_and_can_restore_the_baseline(self):
        self.plugin.config.update(personality_growth_v1=True,generation_contract=True);self.install_life();await self.plugin.sync_growth()
        store=self.plugin.growth_store();claim=store.claim(GLOBAL);store.finish(claim,proposal(claim),True)
        module=importlib.import_module('data.plugins.dafeiyu_dialogue.main')
        request=NS(username='operator',json=AsyncMock(return_value=dict(expected_revision=0,target_version=0)))
        with patch.object(module,'web_request',request):
            with self.assertRaises(ValueError):await self.plugin.rollback_personality()
            request.json.return_value=dict(expected_revision=1,target_version=0)
            await self.plugin.rollback_personality()
        self.assertEqual(store.current(GLOBAL)['tendencies'],[])
        self.assertEqual(store.current(GLOBAL)['version'],2);self.assertIsNone(store.claim(GLOBAL))

    async def test_own_project_selection_and_initiative_receive_growth_without_extra_activity_or_messages(self):
        from data.plugins.dafeiyu_life.planning import choose_activity,SelfJournal
        from data.plugins.dafeiyu_companion.spontaneous_policy import choose_initiative
        growth={'global':{'version':1,'tendencies':[dict(tendency='更喜欢留白',when='写作',counterweight='认真问题仍完整')]}}
        with tempfile.TemporaryDirectory() as tmp:
            none=dict(kind='none',project_id='',expected_version=0,title='',question='',source_ids=[],reason='今天先不推进')
            activity=NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(none))))
            result=await choose_activity(activity,SelfJournal(Path(tmp)),[],['reading','writing'],growth=growth)
            self.assertEqual(result['kind'],'none')
            packet=json.loads(activity.text_chat.call_args.kwargs['prompt'])
            self.assertTrue(packet['grown_dispositions_available']);self.assertNotIn('grown_dispositions',packet)
            empty=dict(candidate_id='',text='',source_quote='',reason='没有新的分享',related_message_ids=[],activity_day='')
            initiative=NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(empty))))
            self.assertEqual(await choose_initiative(initiative,[],[],[],dt.datetime.now(TZ).date().isoformat(),growth=growth),(None,''))
            selected_growth=json.loads(initiative.text_chat.call_args.kwargs['prompt'])['character_growth']
            self.assertEqual(selected_growth['global']['tendencies'],[])
            self.assertTrue(selected_growth['global']['details_available'])


if __name__=='__main__':unittest.main()
