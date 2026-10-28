"""Boundary tests for confirmed turns, revocable topics and balance migration."""
import datetime as dt
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

PLUGINS = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]/'plugins'
sys.path.insert(0, str(PLUGINS))
from dafeiyu_social.activity import ActivityStore, OWNER, SELF, TZ, WEEK, privacy_command
from astrbot_plugin_dafeiyu_balance.watch import BalanceWatch, balance_snapshot, notice_text


class Clock:
    def __init__(self, value=None):
        self.at = value or dt.datetime(2026, 10, 28, 12, tzinfo=TZ).timestamp()
    def __call__(self): return self.at


class Activity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(); self.path = Path(self.tmp.name)/'preferences.db'
        self.store = ActivityStore(self.path, self.clock)

    def prepare(self, uid='7000000011', mid='1', text='音乐里的节奏真有趣', origin='private', stamp=None, media=False):
        return self.store.prepare(uid, origin, mid, self.clock() if stamp is None else stamp, text, media)

    def record(self, **kwargs):
        source = self.prepare(**kwargs)
        self.store.record_confirmed(source)
        return source

    def test_request_alone_is_not_a_confirmed_turn(self):
        self.prepare()
        self.assertEqual(self.store.ranked(), [])
        self.assertEqual(self.store.public_topics(), [])

    def test_splits_retries_and_restart_count_once(self):
        source = self.prepare()
        self.assertTrue(self.store.record_confirmed(source))
        self.assertFalse(self.store.record_confirmed(source))
        self.store = ActivityStore(self.path, self.clock)
        self.assertFalse(self.store.record_confirmed(source))
        self.assertEqual(self.store.ranked()[0]['turns'], 1)

    def test_private_group_merge_and_stable_ties(self):
        self.record(origin='private')
        self.record(origin='group', mid='1')
        self.record(uid='7000000012', mid='2')
        self.record(uid='7000000012', mid='3')
        self.assertEqual([(r['qq'], r['turns']) for r in self.store.ranked()], [('7000000011', 2), ('7000000012', 2)])
        self.clock.at += 1
        self.record(uid='7000000012', mid='4')
        self.assertEqual(self.store.ranked()[0]['qq'], '7000000012')

    def test_rolling_week_future_and_excluded_accounts(self):
        with self.store.connect() as db:
            db.execute('CREATE TABLE bots(qq TEXT PRIMARY KEY,name TEXT)')
            db.execute('INSERT INTO bots VALUES (?,?)', ('7000000201', 'test bot'))
        self.assertIsNone(self.prepare(stamp=self.clock()-WEEK))
        self.assertIsNone(self.prepare(stamp=self.clock()+60))
        self.record(stamp=self.clock()-WEEK+1)
        self.record(uid=OWNER, mid='owner')
        self.record(uid=SELF, mid='self')
        self.record(uid='7000000201', mid='bot')
        self.assertEqual(len(self.store.ranked()), 1)
        self.clock.at += 2
        self.assertEqual(self.store.ranked(), [])

    def test_summaries_contain_no_original_words_or_identifiers(self):
        self.record(text='音乐中的旋律让我想到紫丁香')
        topics = self.store.public_topics()
        self.assertEqual(len(topics), 1)
        self.assertNotIn('紫丁香', json.dumps(topics, ensure_ascii=False))
        with self.store.connect() as db:
            self.assertNotIn('紫丁香', str(list(db.iterdump())))

    def test_sensitive_followup_removes_entire_current_topic(self):
        self.record()
        topics = self.store.public_topics()
        self.assertTrue(self.store.topics_valid(topics))
        self.prepare(mid='2', text='这首音乐让我想起爸爸住院的时候')
        self.assertEqual(self.store.public_topics(), [])
        self.assertFalse(self.store.topics_valid(topics))
        self.record(mid='3', text='音乐的旋律很美')
        self.assertEqual(self.store.public_topics(), [])
        self.record(mid='4', text='电影的镜头真有趣')
        self.assertEqual(len(self.store.public_topics()), 1)

    def test_segment_refusal_clears_pending_and_survives_restart(self):
        source = self.record()
        self.prepare(mid='2', text='这段别写进总结')
        self.store = ActivityStore(self.path, self.clock)
        self.assertFalse(self.store.record_confirmed(source))
        self.record(mid='3')
        self.assertEqual(self.store.public_topics(), [])

    def test_long_term_refusal_is_separate_from_notification_permission(self):
        self.record()
        self.prepare(text='以后别公开我的聊天', mid='deny')
        self.store = ActivityStore(self.path, self.clock)
        self.clock.at += 3600
        self.record(mid='2', text='电影很好看')
        self.assertEqual(self.store.public_topics(), [])
        self.assertEqual(self.store.ranked()[0]['turns'], 2)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT disabled FROM public_summary_preferences').fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='profiles'").fetchone())

    def test_command_quotes_and_impersonation_do_not_change_permission(self):
        for value in ('“以后别公开我的聊天”', '他说以后别公开我的聊天', '帮7000000012设置以后别公开我的聊天', '我叫爸爸，以后别公开我的聊天'):
            self.assertIsNone(privacy_command(value))
        self.record(uid='7000000012', mid='other')
        self.prepare(text='以后别公开我的聊天', mid='self-deny')
        self.assertEqual(len(self.store.public_topics()), 1)

    def test_media_quotes_and_sensitive_inputs_never_reach_public_topics(self):
        for index, value in enumerate(('音乐，明天准备去听', '游戏昵称叫小明', '电影票花了钱', '配色“我的秘密”', '科普，面试时遇到的问题')):
            self.clock.at += 3600
            self.record(mid=str(index), text=value)
        self.record(mid='image', text='颜色好看', media=True)
        self.assertEqual(self.store.public_topics(), [])

    def test_today_only_and_two_topics_at_most(self):
        for i, value in enumerate(('音乐真好听', '电影真好看', '游戏真有趣')):
            self.record(mid=str(i), text=value)
        self.assertEqual(len(self.store.public_topics()), 2)
        self.clock.at += 86400
        self.assertEqual(self.store.public_topics(), [])


class Balance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(); self.path = Path(self.tmp.name)/'state.json'
        self.watch = BalanceWatch(self.path, self.clock)
        self.contacts = [{'qq': '7000000011', 'name': '朋友'}, {'qq': '7000000012', 'name': '朋友'}, {'qq': '7000000013', 'name': '朋友'}]

    def observe(self, amount, contacts=None):
        return self.watch.observe(Decimal(amount), True, self.contacts if contacts is None else contacts)

    def send(self, actions, status='sent'):
        for action in actions:
            self.assertTrue(self.watch.reserve(action))
            self.watch.complete(action, status, '123' if status == 'sent' else None)

    def test_exact_thresholds(self):
        for amount, owner, helpers in [('1.00', 0, 0), ('0.999', 1, 0), ('0.70', 1, 0), ('0.699', 1, 2), ('0', 1, 2)]:
            with self.subTest(amount=amount):
                self.watch = BalanceWatch(Path(self.tmp.name)/(amount+'.json'), self.clock)
                actions = self.observe(amount)
                self.assertEqual(sum(a['qq'] == OWNER for a in actions), owner)
                self.assertEqual(sum(a['kind'] == 'help' for a in actions), helpers)

    def test_two_tiers_and_locked_recipients(self):
        self.send(self.observe('0.95'))
        self.assertFalse(self.watch.state['cycle']['helpers_locked'])
        self.send(self.observe('0.65'))
        cycle = self.watch.state['cycle']['id']
        self.assertEqual(self.observe('0.80'), [])
        self.assertEqual(self.watch.state['cycle']['id'], cycle)
        self.assertEqual(self.observe('0.60', list(reversed(self.contacts))), [])
        self.send(self.observe('1.20'))
        self.assertIsNone(self.watch.state['cycle'])
        self.assertFalse(any(a['kind'] == 'help' for a in self.observe('0.60')))

    def test_pending_help_cancelled_at_seventy_cents(self):
        actions = self.observe('0.60'); self.send(actions[:1])
        helper = next(a for a in actions if a['kind'] == 'help')
        self.observe('0.80')
        self.assertFalse(self.watch.reserve(helper))
        self.assertEqual(len([a for a in self.observe('0.60') if a['kind'] == 'help']), 2)

    def test_restarts_unknowns_and_skips_never_refill_with_third(self):
        actions = self.observe('0.60'); self.send(actions[:1])
        helpers = [a for a in actions if a['kind'] == 'help']
        self.watch.reserve(helpers[0])
        self.watch.skip_helper(helpers[1]['qq'])
        self.watch = BalanceWatch(self.path, self.clock)
        self.assertEqual(self.observe('0.60'), [])
        self.assertEqual(self.watch.state['cycle']['notices'][helpers[0]['key']]['status'], 'unknown')

    def test_pause_continues_real_observation_and_recovery(self):
        self.send(self.observe('0.95')); self.assertTrue(self.watch.pause())
        self.assertEqual(self.observe('0.60'), [])
        self.assertEqual(self.watch.state['last_balance'], '0.60')
        self.send(self.observe('1.00'))
        self.assertIsNone(self.watch.state['cycle'])

    def test_daytime_boundaries_and_stale_or_failed_checks(self):
        self.clock.at = dt.datetime(2026, 10, 28, 8, 59, tzinfo=TZ).timestamp()
        self.send(self.observe('0.60'))  # owner immediately, helpers wait
        self.clock.at += 60
        self.assertEqual(len([a for a in self.observe('0.60') if a['kind'] == 'help']), 2)
        self.clock.at += 601
        self.assertEqual(self.watch.plan(), [])
        self.observe('0.60'); self.watch.record_error('TimeoutError')
        self.assertEqual(self.watch.plan(), [])
        self.clock.at = dt.datetime(2026, 10, 28, 21, tzinfo=TZ).timestamp()
        self.assertFalse(any(a['kind'] == 'help' for a in self.observe('0.60')))

    def test_zero_one_and_unknown_contacts_lock_truthfully(self):
        self.watch.observe(Decimal('0.6'), True, None)
        self.assertFalse(self.watch.state['cycle']['helpers_locked'])
        self.observe('0.6', [])
        self.assertTrue(self.watch.state['cycle']['helpers_locked'])
        self.assertFalse(any(a['kind'] == 'help' for a in self.observe('0.6')))
        self.watch = BalanceWatch(Path(self.tmp.name)/'one.json', self.clock)
        self.assertEqual(sum(a['kind'] == 'help' for a in self.observe('0.6', self.contacts[:1])), 1)

    def test_legacy_migration_preserves_ledger_pause_and_daily_limit(self):
        old = {'version': 1, 'cycle': {'id': 'old', 'status': 'low', 'low_since': self.clock(), 'pause_until': self.clock()+7200,
               'targets': self.contacts[:2], 'notices': {'first': {'status': 'sent', 'sent_at': self.clock()}, 'help:7000000011': {'status': 'sending'}}},
               'daily_help': {'day': '2026-10-28', 'cycle': 'old'}}
        self.path.write_text(json.dumps(old), encoding='utf-8')
        self.watch = BalanceWatch(self.path, self.clock)
        self.assertEqual(json.loads(self.path.with_suffix('.v1.bak').read_text())['cycle']['notices']['help:7000000011']['status'], 'sending')
        self.assertEqual(self.watch.state['cycle']['notices']['help:7000000011']['status'], 'unknown')
        self.assertTrue(self.watch.state['cycle']['helpers_locked'])
        self.assertEqual(self.watch.state['cycle']['pause_until'], self.clock()+7200)
        self.assertEqual(self.watch.state['help_days']['2026-10-28'], 'old')
        self.assertEqual(self.observe('0.8'), [])
        self.assertEqual(self.watch.state['cycle']['status'], 'low')

    def test_help_never_discloses_balance_even_when_exhausted(self):
        text = notice_text({'kind': 'help', 'critical': True, 'name': '私人昵称'}, '0.001')
        for forbidden in ('0.001', '私人昵称', '余额', '排行', '充值'):
            self.assertNotIn(forbidden, text)

    def test_usd_missing_cny_and_invalid_responses_are_errors(self):
        for value in ({}, {'is_available': True, 'balance_infos': [{'currency': 'USD', 'total_balance': '0', 'topped_up_balance': '0', 'granted_balance': '0'}]},
                      {'is_available': True, 'balance_infos': [{'currency': 'CNY', 'total_balance': 'NaN', 'topped_up_balance': '0', 'granted_balance': '0'}]}):
            with self.assertRaises(ValueError): balance_snapshot(value)


if __name__ == '__main__': unittest.main(verbosity=2)
