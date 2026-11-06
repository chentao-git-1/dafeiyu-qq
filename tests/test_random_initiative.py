"""Model-led initiative: no message quota, durable waiting and one claimed decision."""
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


def stamp(hour=23):return dt.datetime(2026,11,1,hour,tzinfo=TZ).timestamp()


class AutonomousInitiative(unittest.TestCase):
    def test_legacy_zero_quota_and_cooldown_do_not_limit_new_sends_or_erase_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];path=Path(tmp)/'push.db';store=InitiativeStore(path,clock=lambda:now[0])
            with store.connect() as db:
                db.execute('INSERT INTO random_days VALUES(?,?,?,?)',(store.day(now[0]),0,'[]',now[0]))
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)',('2026-10-31',0,'old work','life','sent',now[0]-86400,'old body',''))
                store.set(db,'next_push_at',now[0]+7200)
            for i in range(6):
                day,slot=store.due();epoch=store.epoch()
                self.assertTrue(store.reserve(day,slot,'new work:'+str(i),'care',epoch))
                self.assertTrue(store.begin_send(day,slot,epoch,'a grounded thought'))
                store.finish(day,slot,'sent');now[0]+=61
                store.observe_incoming('reply:'+str(i),now[0])
            reopened=InitiativeStore(path,clock=lambda:now[0]);status=reopened.status()
            self.assertEqual(status['sent_today'],6);self.assertIsNone(status['daily_limit'])
            self.assertEqual(status['frequency_mode'],'model_decides');self.assertEqual(status['windows_beijing'],[])
            self.assertIsNone(status['cooldown_minutes_range']);self.assertTrue(reopened.used('old work'))
            with reopened.connect() as db:
                self.assertEqual(db.execute('SELECT budget FROM random_days').fetchone()[0],0)
                self.assertEqual(db.execute("SELECT body FROM attempts WHERE source='old work'").fetchone()[0],'old body')

    def test_model_wait_survives_restart_and_new_context_can_be_considered_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];path=Path(tmp)/'push.db';store=InitiativeStore(path,clock=lambda:now[0])
            day,slot=store.due();self.assertTrue(store.claim_evaluation(day,slot,store.epoch(),'same context'))
            store.finish_evaluation(day,slot,'no_action',180);now[0]+=120
            reopened=InitiativeStore(path,clock=lambda:now[0]);day,slot=reopened.due()
            self.assertFalse(reopened.claim_evaluation(day,slot,reopened.epoch(),'same context'))
            self.assertTrue(reopened.claim_evaluation(day,slot,reopened.epoch(),'new actual work'))
            reopened.finish_evaluation(day,slot,'no_action',30);now[0]+=1801
            day,slot=reopened.due();self.assertTrue(reopened.claim_evaluation(day,slot,reopened.epoch(),'new actual work'))

    def test_pending_reply_and_unknown_delivery_remain_blocked_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];path=Path(tmp)/'push.db';store=InitiativeStore(path,clock=lambda:now[0])
            day,slot=store.due();epoch=store.epoch();self.assertTrue(store.reserve(day,slot,'work','life',epoch))
            self.assertTrue(store.begin_send(day,slot,epoch,'a thought'));store.finish(day,slot,'unknown','TimeoutError')
            now[0]+=86400;store=InitiativeStore(path,clock=lambda:now[0])
            self.assertIsNone(store.due());self.assertTrue(store.used('work'))
            store.observe_incoming('actual reply',now[0]);self.assertIsNotNone(store.due())
            day,slot=store.due();self.assertFalse(store.reserve(day,slot,'work','life',store.epoch()))

    def test_concurrent_evaluations_claim_once_and_failed_assessment_is_not_tick_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];path=Path(tmp)/'push.db'
            stores=[InitiativeStore(path,clock=lambda:now[0]) for _ in range(2)];day,slot=stores[0].due()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda store:store.claim_evaluation(day,slot,store.epoch(),'same'),stores))
            self.assertEqual(sorted(results),[False,True]);now[0]+=120
            restarted=InitiativeStore(path,clock=lambda:now[0]);day,slot=restarted.due()
            self.assertFalse(restarted.claim_evaluation(day,slot,restarted.epoch(),'same'))


class NoActionDecision(unittest.IsolatedAsyncioTestCase):
    initiative_fixture=base.BackgroundExecution.initiative_fixture

    async def test_model_chooses_wait_and_unchanged_context_does_not_cause_more_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            model=AsyncMock(return_value=NS(completion_text=json.dumps(dict(candidate_id='',text='',source_quote='',
                reason='刚聊过，没有新内容',related_message_ids=[],activity_day='',reconsider_after_minutes=150))))
            send=AsyncMock();engine,store=await self.initiative_fixture(tmp,model,send)
            now=[engine.clock()];engine.clock=store.clock=lambda:now[0]
            await engine.tick_semantic();now[0]+=120;await engine.tick_semantic()
            engine.store=InitiativeStore(store.path,clock=lambda:now[0]);await engine.tick_semantic()
            model.assert_awaited_once();send.assert_not_awaited()
            self.assertEqual(store.status()['attempts_today'],0)
            self.assertEqual(store.status()['next_consideration_at'],now[0]-120+150*60)
            timing=json.loads(model.call_args.kwargs['prompt'])['timing']
            self.assertIsNone(timing['daily_message_limit']);self.assertIn('beijing_now',timing)
            now[0]+=150*60;await engine.tick_semantic();self.assertEqual(model.await_count,2)


if __name__=='__main__':unittest.main()
