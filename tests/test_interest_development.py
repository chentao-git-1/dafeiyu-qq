"""Model-owned new interests and project lifecycle with fake providers only."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

from test_execution_autonomy import context
from test_personality_growth import seed_works
from test_self_direction import proposal
from data.plugins.dafeiyu_life.direction import DirectionStore, packet, parse
from data.plugins.dafeiyu_life.planning import (
    SelfJournal, creation_context, interest_context, parse_choice,
)
from data.plugins.dafeiyu_life.life import LifeEngine


def idea(**changes):
    return dict(kind='writing', project_id='', expected_version=0, title='两个人抢一句话',
        question='写一段互相抢话的短对话', source_ids=[], reason='现在想试试对话节奏',
        direction_version=0, use_world=False, **changes)


def offered(journal, records=()):
    return dict(allowed_kinds=['reading', 'writing', 'drawing'], recent_works=list(records),
        self_state=journal.view())


class InterestPersistence(unittest.TestCase):
    def test_new_intention_needs_no_old_interest_or_fabricated_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); life = seed_works(root); journal = SelfJournal(root)
            materials = packet(life, journal); store = DirectionStore(root)
            choice = proposal(materials, title='对白节奏', question='抢话怎样变有趣',
                source_ids=[], source_quote='', next_step='写两个人互相抢话的对话')
            self.assertTrue(store.finish(store.claim(materials), choice))
            self.assertEqual(DirectionStore(root).visible(life)['title'], '对白节奏')
            self.assertEqual(store.current()['source_ids'], [])
            with self.assertRaises(ValueError):
                parse(json.dumps(dict(choice, source_quote='不存在的历史摘录')), materials, None)
            with self.assertRaises(ValueError):
                parse(json.dumps(dict(choice, source_ids=['private:unknown'], source_quote='私聊')), materials, None)

    def test_new_or_existing_topics_all_use_the_same_source_free_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = SelfJournal(Path(tmp)); materials = offered(journal)
            for title in ('对白节奏', '留白与不确定', '机器齿轮的形状'):
                for kind in ('reading', 'writing', 'drawing'):
                    choice = idea() | dict(title=title, kind=kind)
                    self.assertEqual(parse_choice(json.dumps(choice), materials), choice)
            with self.assertRaises(ValueError):
                parse_choice(json.dumps(idea() | dict(source_ids=['invented'])), materials)

    def test_pause_resume_and_stale_multi_project_update_are_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); journal = SelfJournal(root)
            first = journal.begin_project(idea())
            second = journal.begin_project(idea() | dict(title='另一件事', question='另一个问题'))
            choice = idea() | dict(kind='none', project_updates=[
                dict(project_id=first['id'], expected_version=1, status='paused'),
                dict(project_id=second['id'], expected_version=99, status='paused')])
            with self.assertRaises(ValueError):
                journal.begin_project(choice)
            self.assertTrue(all(p['status'] == 'active' and p['version'] == 1 for p in journal.view()['projects']))
            choice['project_updates'].pop()
            journal.begin_project(parse_choice(json.dumps(choice), offered(journal)))
            reopened = SelfJournal(root)
            paused = next(p for p in reopened.view()['projects'] if p['id'] == first['id'])
            self.assertEqual((paused['status'], paused['version']), ('paused', 2))
            resume = idea() | dict(project_id=first['id'], expected_version=2, project_updates=[
                dict(project_id=first['id'], expected_version=2, status='active')])
            active = reopened.begin_project(parse_choice(json.dumps(resume), offered(reopened)))
            self.assertEqual((active['status'], active['version']), ('active', 3))
            with self.assertRaises(ValueError):
                reopened.begin_project(resume)

    def test_project_pause_does_not_forge_completion_or_change_source_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); life = seed_works(root); journal = SelfJournal(root)
            project = journal.begin_project(idea())
            record = life.verified_records(1)[0]
            journal.integrate(record, dict(insight='先放下这件事', evidence=record['note'][:8],
                next_question='', status='paused'), project)
            current = SelfJournal(root).view()['projects'][0]
            self.assertEqual(current['status'], 'paused')
            self.assertEqual(life.count(), 3)
            self.assertEqual(life.verified_records(1)[0], record)
            invalid = idea() | dict(kind='none', project_updates=[
                dict(project_id=current['id'], expected_version=current['version'], status='completed')])
            with self.assertRaises(ValueError):
                journal.begin_project(invalid)

    def test_overview_compresses_a_project_without_erasing_its_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); life = seed_works(root); journal = SelfJournal(root)
            project = journal.begin_project(idea() | dict(title='短句的余味'))
            records = life.verified_records(60)
            for record in reversed(records):
                journal.integrate(record, dict(insight='这次的表达取舍', evidence=record['note'][:8],
                    next_question='再看看', status='active'), project)
                project = journal.view()['projects'][0]
            records[0] = dict(records[0], project_id=project['id'])
            state, works = interest_context(journal, records)
            self.assertEqual(len(works), 1)
            self.assertEqual(len(state['reflections']), 1)
            self.assertEqual(works[0]['id'], records[0]['id'])
            self.assertEqual(len(journal.view()['reflections']), 3)
            self.assertEqual(life.count(), 3)

    def test_new_creation_excludes_old_work_but_explicit_selection_restores_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); life = seed_works(root); journal = SelfJournal(root)
            records = life.verified_records(60)
            old = journal.begin_project(idea() | dict(title='旧项目'))
            journal.integrate(records[0], dict(insight='过去的反思', evidence=records[0]['note'][:8],
                next_question='旧问题', status='active'), old)
            new = journal.begin_project(idea())
            works, state = creation_context(journal, records, idea(), new)
            self.assertEqual(works, []); self.assertEqual(state['reflections'], [])
            selected = idea() | dict(source_ids=[records[-1]['id']])
            works, state = creation_context(journal, records, selected, new)
            self.assertEqual([r['id'] for r in works], [records[-1]['id']])
            self.assertEqual(state['project']['id'], new['id'])


class InterestExecution(unittest.IsolatedAsyncioTestCase):
    async def test_new_work_uses_only_selected_context_without_extra_calls_or_daily_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); life = seed_works(root); before = life.records(60)
            generated = dict(note='“你先说。”“好，我——”“等等，让我先猜。”两个人笑着同时停住了。',
                thought='抢半句也能让对话跑起来。', story='', evidence='',
                reflection=dict(insight='短暂打断可以构成节奏', evidence='等等，让我先猜', next_question='', status='completed'))
            provider = NS(text_chat=AsyncMock(side_effect=[
                NS(completion_text=json.dumps(idea(), ensure_ascii=False)),
                NS(completion_text=json.dumps(generated, ensure_ascii=False))]))
            engine = LifeEngine(life, context(provider))
            self.assertTrue((await engine.advance(bootstrap=True))['advanced'])
            prompt = provider.text_chat.call_args.kwargs['prompt']
            for record in before:
                self.assertNotIn(record['note'], prompt)
                self.assertNotIn(record['thought'], prompt)
            self.assertEqual(life.count(), 4)
            self.assertEqual(life.records(1)[0]['note'], generated['note'])
            self.assertEqual(SelfJournal(root).view()['projects'][0]['status'], 'completed')
            await engine.advance(bootstrap=True)
            self.assertEqual(provider.text_chat.await_count, 2)

    async def test_none_can_pause_a_project_without_writing_or_searching(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); life = seed_works(root); journal = SelfJournal(root)
            project = journal.begin_project(idea())
            choice = idea() | dict(kind='none', project_updates=[
                dict(project_id=project['id'], expected_version=1, status='paused')])
            provider = NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(choice))))
            engine = LifeEngine(life, context(provider)); engine.reading = AsyncMock()
            self.assertEqual((await engine.advance(bootstrap=True))['status'], 'chose_no_activity')
            self.assertEqual(SelfJournal(root).view()['projects'][0]['status'], 'paused')
            self.assertEqual(life.count(), 3)
            await engine.advance(bootstrap=True)
            provider.text_chat.assert_awaited_once(); engine.reading.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
