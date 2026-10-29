"""Real admission and sending hooks with fake QQ transport and administrator IDs."""
import datetime as dt
import gc
import importlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

STAGE = Path(sys.argv.pop(1))
sys.path.insert(0, str(STAGE))
main = importlib.import_module('dafeiyu_event_limits.main')
quota = importlib.import_module('dafeiyu_event_limits.quota')
ADMIN, USER, GROUP = '7000000001', '7000000011', '7000000101'


class Event:
    def __init__(self, uid=USER, group='', mid='1', claimed_admin=False):
        self.uid, self.group = uid, group
        self.role = 'admin' if claimed_admin else 'member'
        self.message_obj = NS(raw_message={
            'post_type':'message', 'self_id':'7000000002', 'user_id':uid,
            'group_id':group, 'message_id':mid,
            'sender':{'nickname':'管理员', 'role':'admin'} if claimed_admin else {},
        })
        self.stopped = False
        self.result = NS(chain=[main.Plain('result')])
        self.send = AsyncMock()

    def get_platform_id(self):return main.PLATFORM
    def get_sender_id(self):return self.uid
    def get_group_id(self):return self.group
    def stop_event(self):self.stopped=True
    def is_stopped(self):return self.stopped
    def get_result(self):return self.result


class AdminHooks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(gc.collect)
        self.path_patch = patch.object(main, 'get_astrbot_data_path', return_value=self.tmp.name)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.sent = []
        self.fail = False
        self.config = {'admins_id':[ADMIN]}
        self.bot = object()
        self.platform = NS(bot=self.bot, meta=lambda:NS(id=main.PLATFORM))
        self.context = NS(register_web_api=lambda *a:None, get_platform=lambda *a:self.platform,
                          get_config=lambda *a:self.config)
        before = main.AiocqhttpMessageEvent.__dict__['send_message']

        async def inert_send(cls, bot, message_chain, event=None, is_group=False, session_id=None):
            if self.fail:raise RuntimeError('test transport failure')
            self.sent.append((is_group, session_id))
            return {'message_id':len(self.sent)}

        main.AiocqhttpMessageEvent.send_message = classmethod(inert_send)
        self.addCleanup(setattr, main.AiocqhttpMessageEvent, 'send_message', before)
        self.plugin = main.EventLimits(self.context)
        self.now = dt.datetime(2026, 10, 29, 10, tzinfo=quota.BEIJING).timestamp()
        self.plugin.ledger.clock = lambda:self.now
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)

    async def send(self, event=None, target=ADMIN, group=False):
        return await main.AiocqhttpMessageEvent.send_message(
            self.bot, main.MessageChain(chain=[main.Plain('captured reply')]),
            event.message_obj.raw_message if event else None, group,
            event.group if event and group else event.uid if event else target,
        )

    async def test_admin_is_unlimited_in_private_admission_and_send_hooks(self):
        for i in range(30):
            event = Event(ADMIN, mid=str(i))
            await main.RateLimitStage.process(None, event)
            await self.plugin.check_event(event)
            await self.plugin.check_result(event)
            await self.send(event)
            self.assertFalse(event.stopped)
            self.assertTrue(event.result.chain)
            event.send.assert_not_awaited()
        self.assertEqual(self.plugin.ledger.summary()['charged_replies_in_window'], 0)
        self.assertEqual(len(self.sent), 30)

    async def test_full_group_cannot_block_administrator_in_any_hook(self):
        for i in range(15):
            await self.send(Event('70000002'+str(i), GROUP, str(i)), group=True)
        event = Event(ADMIN, GROUP, 'admin')
        await main.RateLimitStage.process(None, event)
        await self.plugin.check_result(event)
        await self.send(event, group=True)
        self.assertFalse(event.stopped)
        self.assertEqual(self.plugin.ledger.summary()['charged_replies_in_window'], 15)
        with self.assertRaises(quota.ReplyLimitExceeded):
            await self.send(Event(USER, GROUP, 'regular'), group=True)

    async def test_claiming_admin_does_not_bypass_configured_identity(self):
        for i in range(10):
            await self.send(Event(USER, mid=str(i)))
        event = Event(USER, mid='over-limit', claimed_admin=True)
        await self.plugin.check_event(event)
        self.assertTrue(event.stopped)
        with self.assertRaises(quota.ReplyLimitExceeded):
            await self.send(event)
        self.assertEqual(len(self.sent), 10)

    async def test_proactive_private_to_administrator_is_exempt(self):
        for _ in range(25):
            await self.send(target=ADMIN)
        self.assertIsNone(self.plugin.ledger.blocked(ADMIN))
        self.assertEqual(self.plugin.ledger.summary()['charged_replies_in_window'], 0)

    async def test_configured_admin_list_is_read_dynamically(self):
        for i in range(10):
            await self.send(Event(USER, mid=str(i)))
        self.config['admins_id'].append(USER)
        event = Event(USER, mid='now-admin')
        await self.plugin.check_event(event)
        await self.send(event)
        self.assertFalse(event.stopped)
        self.config['admins_id'].remove(USER)
        self.assertEqual(self.plugin.ledger.blocked(USER), 'user')

    async def test_admin_transport_failure_is_not_reported_as_success(self):
        self.fail = True
        with self.assertRaises(RuntimeError):
            await self.send(Event(ADMIN))
        self.assertEqual(self.plugin.ledger.summary()['pending_sends'], 0)
        self.assertEqual(len(self.sent), 0)


if __name__ == '__main__':
    unittest.main()
