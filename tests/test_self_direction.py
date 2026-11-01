"""Persistent self-direction and private discussion initiative; no real sends or network."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
import test_cognition as base
from test_personality_growth import seed_works
from data.plugins.dafeiyu_life.direction import DirectionStore,packet,parse
from data.plugins.dafeiyu_life.planning import SelfJournal,choose_activity,parse_choice
from data.plugins.dafeiyu_life.life import LifeEngine
from data.plugins.dafeiyu_companion.spontaneous import InitiativeEngine,OWNER,SESSION
from data.plugins.dafeiyu_companion.spontaneous_store import InitiativeStore
from data.plugins.dafeiyu_natural_chat.exchanges import ExchangeStore


def proposal(materials,revision=0,**changes):
    from data.plugins.dafeiyu_natural_chat.self_profile import self_profile
    value=dict(decision='pursue',expected_version=revision,title='潮汐与留白',question='留白能否表现潮水退去的感觉',
        next_step='写一个不把结尾说满的短段落',kind='writing',source_ids=['operator:seed'],
        source_quote=self_profile()['likes'][0],reason='从自己的兴趣发展一个具体表达问题',reconsider_after_minutes=180)
    return value|changes


class DirectionPersistence(unittest.TestCase):
    def test_self_chosen_plan_pause_hold_and_wait_persist_and_stale_result_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);life=seed_works(root);journal=SelfJournal(root);materials=packet(life,journal)
            now=[time.time()];store=DirectionStore(root,clock=lambda:now[0]);claim=store.claim(materials)
            self.assertTrue(store.finish(claim,proposal(materials)));first=store.current()
            self.assertEqual(first['status'],'active');self.assertEqual(DirectionStore(root).current(),first)
            now[0]+=600;self.assertIsNone(store.claim(materials))
            now[0]+=10800;claim=store.claim(materials)
            self.assertTrue(store.finish(claim,proposal(materials,1,decision='hold')))
            self.assertEqual(store.current(),first)
            now[0]+=10801;claim=store.claim(materials)
            self.assertTrue(store.finish(claim,proposal(materials,1,decision='pause')))
            self.assertEqual(store.current()['status'],'paused');self.assertFalse(store.finish(claim,proposal(materials,1)))
            now[0]+=10801;claim=store.claim(materials)
            self.assertFalse(store.finish(claim,proposal(materials,2),unchanged=False))
            self.assertEqual(store.current()['version'],2)

    def test_only_a_real_matching_step_can_complete_an_intention_and_source_tampering_hides_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);life=seed_works(root);materials=packet(life,SelfJournal(root));store=DirectionStore(root)
            choice=proposal(materials,source_ids=[materials['sources'][1]['id']],source_quote=materials['sources'][1]['note'][:12])
            store.finish(store.claim(materials),choice);old_record=life.verified_records(1)[0]
            self.assertFalse(store.complete_step(1,old_record,life))
            path=root/'works/new-step.json';path.write_text('本轮实际完成的数字文字',encoding='utf8')
            record=dict(old_record,id='life:new-step',day='new-step',artifact='works/new-step.json',
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),completed_at=time.time())
            with life.db() as db:db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?)',
                ('new-step',time.time(),'completed','writing',json.dumps(record),'',time.time()))
            self.assertFalse(store.complete_step(1,dict(record,sha256='forged'),life))
            self.assertEqual(store.visible(life)['status'],'intent')
            self.assertTrue(store.complete_step(1,record,life));self.assertEqual(store.current()['status'],'done')
            self.assertFalse(store.complete_step(1,record,life))
            (root/old_record['artifact']).write_bytes(b'altered')
            self.assertIsNone(store.visible(life))
            bad=proposal(materials,source_ids=['private:unknown'])
            with self.assertRaises(ValueError):parse(json.dumps(bad),materials,None)


class DirectionIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_background_thought_drives_activity_and_native_context_without_private_inputs_or_extra_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);life=seed_works(root);provider=NS(text_chat=AsyncMock())
            async def generate(**kwargs):
                data=json.loads(kwargs['prompt']);return NS(completion_text=json.dumps(proposal(data)))
            provider.text_chat.side_effect=generate
            controller=NS(owner_policy_enabled=lambda flag:True,natural=lambda:NS(actor_owner=lambda:OWNER),
                metered_provider=lambda p,*args:p,growth_profile=AsyncMock(return_value={'global':{'version':1}}))
            balance=NS(watch=NS(status=lambda:dict(last_balance_cny='2',last_available=True,last_checked_at=time.time())))
            context=NS(get_registered_star=lambda name:NS(activated=True,star_cls={'dafeiyu_dialogue':controller,
                'astrbot_plugin_dafeiyu_balance':balance}.get(name)),get_using_provider_async=AsyncMock(return_value=provider))
            engine=LifeEngine(life,context);before=life.count();self.assertTrue(await engine.consider_direction())
            self.assertFalse(await engine.consider_direction());provider.text_chat.assert_awaited_once()
            self.assertEqual(life.count(),before);self.assertEqual(engine.direction_profile()['status'],'intent')
            request=json.loads(provider.text_chat.call_args.kwargs['prompt'])
            self.assertEqual(set(request),{'sources','self_state','character_growth','current'})
            activity=dict(kind='writing',project_id='',expected_version=0,title='潮汐与留白',question='写一段潮汐的文字',
                source_ids=['operator:seed'],reason='推进自己的打算',direction_version=1)
            creator=NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(activity))))
            self.assertEqual((await choose_activity(creator,SelfJournal(root),life.verified_records(12),['writing']))['direction_version'],1)
            offered=json.loads(creator.text_chat.call_args.kwargs['prompt']);self.assertEqual(offered['self_direction']['question'],proposal(request)['question'])
            with self.assertRaises(ValueError):parse_choice(json.dumps(dict(activity,kind='drawing')),offered)
            dialogue=NS(context=NS(get_registered_star=lambda name:NS(activated=True,star_cls=NS(store=life,engine=engine))))
            from data.plugins.dafeiyu_dialogue.main import Dialogue
            thoughts=await Dialogue.own_thoughts(dialogue)
            self.assertEqual(thoughts['intentions'][0]['status'],'intent');self.assertEqual(life.count(),before)

    async def test_failed_thought_is_not_retried_each_tick_and_does_not_advance_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            life=seed_works(Path(tmp));call=AsyncMock(side_effect=TimeoutError())
            controller=NS(owner_policy_enabled=lambda flag:True,natural=lambda:NS(actor_owner=lambda:OWNER),metered_provider=lambda p,*args:p)
            balance=NS(watch=NS(status=lambda:dict(last_balance_cny='2',last_available=True,last_checked_at=time.time())))
            context=NS(get_registered_star=lambda name:NS(activated=True,star_cls=controller if name=='dafeiyu_dialogue' else balance),
                get_using_provider_async=AsyncMock(return_value=NS(text_chat=call)))
            engine=LifeEngine(life,context);before=life.count()
            with self.assertRaises(TimeoutError):await engine.consider_direction()
            self.assertFalse(await engine.consider_direction());call.assert_awaited_once()
            self.assertEqual(engine.direction_store().status()['last_review'],'failed');self.assertEqual(life.count(),before)


class PrivateContinuation(unittest.IsolatedAsyncioTestCase):
    async def test_only_current_private_human_words_are_offered_and_forgetting_invalidates_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);now=time.time();exchanges=ExchangeStore(root/'exchanges.db')
            exchanges.record(OWNER,SESSION,'one','假如潮汐有颜色，我想它会是灰蓝的','这个设想有意思')
            exchanges.record('other','dafeiyu-qq:FriendMessage:other','two','他人的独有私事','回应')
            exchanges.record(OWNER,'dafeiyu-qq:GroupMessage:group','three','群里的独有话题','回应')
            natural=NS(exchange_store=lambda:exchanges,store=NS(search=lambda **kwargs:[]))
            plugin=NS(context=NS(),state={},lock=asyncio.Lock());store=InitiativeStore(root/'push.db')
            engine=InitiativeEngine(plugin,store);engine.active=lambda name:natural if name=='dafeiyu_natural_chat' else None
            engine.semantic_controller=lambda:None
            sources=[row async for row in engine.candidates()]
            self.assertEqual(len(sources),1);source=sources[0]
            self.assertEqual(source['kind'],'conversation');self.assertTrue(source['evidence'].startswith('假如'))
            self.assertTrue(await engine.source_valid(source));exchanges.forget(OWNER,SESSION,latest=True)
            self.assertFalse(await engine.source_valid(source));self.assertEqual(engine.conversation_candidates(),[])
            # A discussion-derived send retains only a dedupe fingerprint in this store.
            day,slot=store.due();epoch=store.epoch();self.assertTrue(store.reserve(day,slot,source['id'],'conversation',epoch))
            self.assertTrue(store.begin_send(day,slot,epoch,'带有私聊内容的续问'))
            with store.connect() as db:self.assertIsNone(db.execute('SELECT body FROM attempts').fetchone()[0])

    async def test_intention_can_be_shared_before_work_but_pausing_cancels_the_old_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);life=seed_works(root);journal=SelfJournal(root);materials=packet(life,journal)
            now=[time.time()];direction=DirectionStore(root,clock=lambda:now[0]);direction.finish(direction.claim(materials),proposal(materials))
            own=NS(engine=NS(direction_profile=lambda:direction.visible(life)),public_context=lambda:None)
            engine=InitiativeEngine(NS(context=NS()),InitiativeStore(root/'push.db'))
            engine.active=lambda name:own if name=='dafeiyu_life' else None
            sources=[row async for row in engine.candidates()];self.assertEqual(len(sources),1)
            self.assertEqual(sources[0]['status'],'intent');self.assertTrue(await engine.source_valid(sources[0]))
            now[0]+=10801;direction.finish(direction.claim(materials),proposal(materials,1,decision='pause'))
            self.assertFalse(await engine.source_valid(sources[0]))


if __name__=='__main__':unittest.main()
