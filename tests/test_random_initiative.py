"""Random opportunity plans with durable no-action, delivery and quiet-time gates."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

import test_cognition as base
from data.plugins.dafeiyu_companion.spontaneous_store import InitiativeStore,TZ


def stamp(hour=9,day=31):return dt.datetime(2026,10,day,hour,tzinfo=TZ).timestamp()
def rng(count):return NS(randint=lambda a,b:count,uniform=lambda a,b:(a+b)/2)


class RandomInitiative(unittest.TestCase):
    def test_daily_counts_and_times_vary_but_restart_cannot_redraw_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            for count in (0,1,3,4):
                now=[stamp()];path=Path(tmp)/('push'+str(count)+'.db')
                store=InitiativeStore(path,clock=lambda:now[0],rng=rng(count));plan=store.plan()
                self.assertEqual(plan['budget'],count);self.assertEqual(len(plan['opportunities']),count)
                self.assertTrue(all(stamp()<=s['at']<s['until']<=stamp(21) for s in plan['opportunities']))
                self.assertEqual(InitiativeStore(path,clock=lambda:now[0],rng=rng(4-count)).plan(),plan)
                self.assertEqual(store.status()['daily_limit'],count)
                if count==0:self.assertIsNone(store.due());self.assertFalse(store.eligible())

    def test_each_scheduled_opportunity_can_be_evaluated_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];store=InitiativeStore(Path(tmp)/'push.db',clock=lambda:now[0],rng=rng(3));plan=store.plan()
            first,second=plan['opportunities'][:2]
            self.assertFalse(store.claim_evaluation(plan['day'],0,store.epoch()))
            now[0]=first['at']+1
            self.assertTrue(store.claim_evaluation(plan['day'],0,store.epoch()))
            store.finish_evaluation(plan['day'],0,'no_action')
            self.assertFalse(store.claim_evaluation(plan['day'],0,store.epoch()));self.assertIsNone(store.due())
            now[0]=second['at']+1;self.assertEqual(store.due(),(plan['day'],1))

    def test_third_real_send_is_possible_when_the_random_budget_allows_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];store=InitiativeStore(Path(tmp)/'push.db',clock=lambda:now[0],rng=rng(3));plan=store.plan()
            for item in plan['opportunities']:
                now[0]=item['at']+1;epoch=store.epoch()
                self.assertTrue(store.reserve(plan['day'],item['slot'],'work:'+str(item['slot']),'life',epoch))
                self.assertTrue(store.begin_send(plan['day'],item['slot'],epoch,'one grounded thought'))
                store.finish(plan['day'],item['slot'],'sent')
                now[0]+=10;store.observe_incoming('reply:'+str(item['slot']),now[0])
            self.assertEqual(store.status()['sent_today'],3)
            self.assertFalse(store.reserve(plan['day'],3,'unplanned','life',store.epoch()))

    def test_pending_reply_and_random_cooldown_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];path=Path(tmp)/'push.db';store=InitiativeStore(path,clock=lambda:now[0],rng=rng(3))
            plan=store.plan();now[0]=plan['opportunities'][0]['at']+1;epoch=store.epoch()
            self.assertTrue(store.reserve(plan['day'],0,'work:one','life',epoch))
            self.assertTrue(store.begin_send(plan['day'],0,epoch,'one thought'));store.finish(plan['day'],0,'sent')
            allowed=store.status()['next_push_allowed_at'];self.assertTrue(90*60<=allowed-now[0]<=4*3600)
            store=InitiativeStore(path,clock=lambda:now[0],rng=rng(1))
            self.assertEqual(store.status()['next_push_allowed_at'],allowed)
            now[0]=allowed+1;self.assertFalse(store.eligible())
            store.observe_incoming('actual reply',now[0]);self.assertFalse(store.eligible())
            now[0]+=3601;self.assertTrue(store.eligible())

    def test_expired_opportunities_night_and_old_day_cannot_be_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];store=InitiativeStore(Path(tmp)/'push.db',clock=lambda:now[0],rng=rng(1));plan=store.plan()
            now[0]=plan['opportunities'][0]['until']+1
            self.assertIsNone(store.due());self.assertFalse(store.reserve(plan['day'],0,'old','life',store.epoch()))
            now[0]=stamp(23);self.assertIsNone(store.due())
            now[0]=dt.datetime(2026,11,1,9,tzinfo=TZ).timestamp()
            self.assertFalse(store.claim_evaluation(plan['day'],0,store.epoch()))
            self.assertEqual(store.plan()['day'],'2026-11-01')

    def test_legacy_attempts_pending_state_and_quotas_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];path=Path(tmp)/'push.db';store=InitiativeStore(path,clock=lambda:now[0],rng=rng(3))
            with store.connect() as db:
                db.execute('INSERT INTO days VALUES(?,?,?)',('2026-10-31',stamp(10),stamp(19)))
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)',('2026-10-31',0,'old work','life','sent',stamp()-10,'old body',''))
                store.set(db,'pending_at',stamp()-10);store.set(db,'last_push',stamp()-10);store.set(db,'next_push_at',stamp()+7200)
            store=InitiativeStore(path,clock=lambda:now[0],rng=rng(3));plan=store.plan()
            self.assertEqual(store.status()['attempts_today'],1);self.assertTrue(store.status()['awaiting_reply'])
            now[0]=plan['opportunities'][0]['at']+1;self.assertIsNone(store.due())
            with store.connect() as db:self.assertEqual(db.execute('SELECT body FROM attempts').fetchone()[0],'old body')
            self.assertFalse(store.eligible());self.assertTrue(store.used('old work'))

    def test_concurrent_evaluations_cannot_claim_the_same_random_opportunity(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];path=Path(tmp)/'push.db'
            stores=[InitiativeStore(path,clock=lambda:now[0],rng=rng(1)) for _ in range(2)]
            plan=stores[0].plan();now[0]=plan['opportunities'][0]['at']+1
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda store:store.claim_evaluation(plan['day'],0,store.epoch()),stores))
            self.assertEqual(sorted(results),[False,True])


class NoActionDecision(unittest.IsolatedAsyncioTestCase):
    initiative_fixture=base.BackgroundExecution.initiative_fixture

    async def test_model_can_decline_without_consuming_transport_or_repeated_model_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            model=AsyncMock(return_value=NS(completion_text=json.dumps(dict(candidate_id='',text='',source_quote='',
                reason='刚聊过，没有新内容',related_message_ids=[],activity_day=''))))
            send=AsyncMock();engine,store=await self.initiative_fixture(tmp,model,send)
            await engine.tick_semantic();await engine.tick_semantic()
            model.assert_awaited_once();send.assert_not_awaited();self.assertEqual(store.status()['attempts_today'],0)
            self.assertIsNone(store.due())


if __name__=='__main__':unittest.main()
