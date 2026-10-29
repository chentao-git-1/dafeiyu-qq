"""Administrator exemptions with real quota accounting and temporary SQLite."""
import datetime as dt
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

STAGE = Path(sys.argv.pop(1))
spec = importlib.util.spec_from_file_location('admin_quota', STAGE/'dafeiyu_event_limits/quota.py')
quota = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quota)


class AdminQuota(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = dt.datetime(2026, 10, 29, 10, tzinfo=quota.BEIJING).timestamp()
        self.admins = {'7000000001'}
        self.path = Path(self.tmp.name)/'quota.db'
        self.ledger = quota.ReplyLedger(self.path, clock=lambda:self.now, exempt_users=lambda:self.admins)

    def sent(self, turn, user, group=''):
        reserved = self.ledger.reserve(turn, user, group)
        if reserved:
            self.ledger.finish(turn, True)
        return reserved

    def fill_group(self):
        for i in range(15):
            self.sent('group-'+str(i), 'user-'+str(i), 'group')

    def test_admin_can_reply_in_full_group_without_charging(self):
        self.fill_group()
        for i in range(40):
            self.assertIsNone(self.ledger.blocked('7000000001', 'group'))
            self.assertFalse(self.sent('admin-'+str(i), '7000000001', 'group'))
        self.assertEqual(self.ledger.summary()['charged_replies_in_window'], 15)
        self.assertEqual(self.ledger.blocked('ordinary', 'group'), 'group')

    def test_private_admin_unlimited_and_retry_immediate(self):
        for i in range(40):
            self.assertFalse(self.sent('private-'+str(i), '7000000001'))
        self.assertEqual(self.ledger.retry_at('7000000001'), self.now)
        self.assertEqual(self.ledger.summary()['charged_replies_in_window'], 0)

    def test_old_admin_entries_are_removed_without_resetting_others(self):
        self.admins.clear()
        for i in range(10):
            self.sent('old-admin-'+str(i), '7000000001', 'group')
        for i in range(5):
            self.sent('regular-'+str(i), 'ordinary', 'group')
        self.assertEqual(self.ledger.blocked('another', 'group'), 'group')
        self.admins.add('7000000001')
        self.assertIsNone(self.ledger.blocked('another', 'group'))
        self.assertEqual(self.ledger.summary()['charged_replies_in_window'], 5)
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM replies WHERE user_id=?', ('ordinary',)).fetchone()[0], 5)

    def test_administrator_changes_apply_without_rebuilding_ledger(self):
        self.admins.add('new-admin')
        self.assertFalse(self.sent('exempt', 'new-admin'))
        self.admins.remove('new-admin')
        for i in range(10):
            self.assertTrue(self.sent('regular-'+str(i), 'new-admin'))
        self.assertEqual(self.ledger.blocked('new-admin'), 'user')
        with self.assertRaises(quota.ReplyLimitExceeded):
            self.sent('over-limit', 'new-admin')

    def test_regular_user_still_shares_personal_limit_across_destinations(self):
        for i in range(10):
            self.sent('mixed-'+str(i), 'ordinary', '' if i%2 else 'group')
        self.assertEqual(self.ledger.blocked('ordinary', 'elsewhere'), 'user')
        self.assertTrue(self.sent('different-user', 'another', 'group'))

    def test_anonymous_group_messages_do_not_inherit_admin_exemption(self):
        self.fill_group()
        with self.assertRaises(quota.ReplyLimitExceeded):
            self.sent('proactive', '', 'group')

    def test_blank_admin_entry_does_not_erase_proactive_group_budget(self):
        self.admins.add('')
        for i in range(15):
            self.sent('proactive-'+str(i), '', 'group')
        self.assertEqual(self.ledger.blocked('', 'group'), 'group')
        self.assertEqual(self.ledger.summary()['charged_replies_in_window'], 15)

    def test_restart_preserves_regular_counts_and_admin_exemption(self):
        for i in range(10):
            self.sent('regular-'+str(i), 'ordinary')
        reopened = quota.ReplyLedger(self.path, clock=lambda:self.now, exempt_users=lambda:self.admins)
        self.assertEqual(reopened.blocked('ordinary'), 'user')
        self.assertIsNone(reopened.blocked('7000000001', 'group'))


if __name__ == '__main__':
    unittest.main()
