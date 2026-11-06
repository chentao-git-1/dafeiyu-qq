"""Reproduce the reading/feedback loop without external requests or model calls."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from test_execution_autonomy import context
from test_personality_growth import seed_works
from test_self_direction import proposal
from data.plugins.dafeiyu_life.life import LifeEngine, LifeStore
from data.plugins.dafeiyu_life.direction import DirectionStore, packet, choose
from data.plugins.dafeiyu_life.planning import SelfJournal, interest_context
from dafeiyu.life.reading import ReadingError, checked_url, decode_response, read_limited
from dafeiyu.life.store import failure_code
import time


class Scope:
    def __init__(self, value):
        self.value = value
    async def __aenter__(self):
        return self.value
    async def __aexit__(self, *args):
        return False


def split_response(payload):
    raw = json.dumps(payload, ensure_ascii=False).encode()
    reader = aiohttp.StreamReader(NS(_reading_paused=False, connected=True,
        resume_reading=lambda **kwargs: None, pause_reading=lambda **kwargs: None), 2 ** 16)
    reader.feed_data(raw[:7])
    def finish():
        reader.feed_data(raw[7:]); reader.feed_eof()
    asyncio.get_running_loop().call_soon(finish)
    return NS(status=200, content=reader)


class ReadingLoop(unittest.IsolatedAsyncioTestCase):
    async def test_split_json_response_is_fully_read_before_parsing(self):
        response = split_response({'results': [{'title': '一个新问题', 'url': 'https://example.org/paper'}]})
        session = NS(post=lambda *a, **kw: Scope(response))
        engine = LifeEngine(None, NS(get_config=lambda: {'provider_settings': {'websearch_deepseek_key': ['test-key']}}))
        with patch('data.plugins.dafeiyu_life.life.aiohttp.ClientSession', return_value=Scope(session)):
            result = await engine.web_tool('search', {'query': '任意主题'})
        self.assertEqual(result['results'][0]['title'], '一个新问题')

    async def test_model_question_can_reach_a_public_source_outside_the_old_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = LifeEngine(LifeStore(Path(tmp)), NS())
            engine.web_tool = AsyncMock(side_effect=[
                {'results': [{'title': 'Review', 'url': 'https://example.org/review'}]},
                {'title': 'Review', 'content': '这是实际返回的文章正文，包含对研究对象的描述。' * 12},
            ])
            article = await engine.reading('查一篇研究综述')
            self.assertEqual(article['url'], 'https://example.org/review')
            self.assertEqual(engine.web_tool.call_args_list[0].args[1]['query'], '查一篇研究综述')
            self.assertEqual(engine.web_tool.await_count, 2)

    async def test_revisiting_a_source_and_long_questions_do_not_force_an_old_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            life=seed_works(Path(tmp)); record=life.records(1)[0]; url='https://www.cas.cn/kj/202311/t20231127_4988496.shtml'
            record['source']={'url':url}
            with life.db() as db:db.execute('UPDATE runs SET record=? WHERE day=?',(json.dumps(record),record['day']))
            engine=LifeEngine(life,NS()); engine.web_tool=AsyncMock(side_effect=[
                {'results':[{'url':url,'title':'原文'}]}, {'content':'这里包含需要再次查看的原文。','title':'原文'}])
            result=await engine.reading('一个新角度的问题'*30)
            self.assertEqual(result['url'],url)
            query=engine.web_tool.call_args_list[0].args[1]['query']
            self.assertEqual(query,('一个新角度的问题'*30)[:128])
            self.assertEqual(engine.web_tool.await_count,2)

    async def test_bounded_body_and_invalid_json_report_stage_without_private_text(self):
        stream=NS(read=AsyncMock(side_effect=[b'1234',b'5678']))
        with self.assertRaises(ReadingError) as caught:await read_limited(stream,'fetch',limit=6)
        self.assertEqual(str(caught.exception),'reading_fetch_response_too_large')
        with self.assertRaises(ReadingError) as caught:decode_response(b'<html>private-api-error</html>','search')
        self.assertEqual(str(caught.exception),'reading_search_invalid_json')
        self.assertNotIn('private-api-error',str(caught.exception))

    async def test_http_failure_and_cancellation_do_not_retry_or_expose_response(self):
        response=NS(status=429,content=NS(read=AsyncMock(return_value=b'private-provider-body')))
        post=NS(calls=0)
        def request(*args,**kwargs):post.calls+=1;return Scope(response)
        engine=LifeEngine(None,NS(get_config=lambda:{'provider_settings':{'websearch_deepseek_key':['test-key']}}))
        with patch('data.plugins.dafeiyu_life.life.aiohttp.ClientSession',return_value=Scope(NS(post=request))):
            with self.assertRaises(ReadingError) as caught:await engine.web_tool('search',{'query':'主题'})
            self.assertEqual(str(caught.exception),'reading_search_http_429')
            response.content.read.assert_not_awaited(); self.assertEqual(post.calls,1)
            response.status=200; response.content.read=AsyncMock(side_effect=asyncio.CancelledError())
            with self.assertRaises(asyncio.CancelledError):await engine.web_tool('fetch',{'url':'https://example.org/article'})
            self.assertEqual(post.calls,2)

    async def test_empty_results_are_a_search_outcome_and_do_not_fetch_or_invent_a_source(self):
        engine=LifeEngine(None,NS()); engine.web_tool=AsyncMock(return_value={'results':[]})
        with self.assertRaises(ReadingError) as caught:await engine.reading('查一下这个问题')
        self.assertEqual(caught.exception.code,'reading_search_no_public_results')
        engine.web_tool.assert_awaited_once()

    async def test_planning_retains_the_intention_without_replaying_its_old_rationale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); life=seed_works(root); journal=SelfJournal(root); now=[time.time()]
            store=DirectionStore(root,clock=lambda:now[0]); materials=packet(life,journal)
            store.finish(store.claim(materials),proposal(materials,reason='过去那次检查要求继续，这个旧解释不应成为新证据',reconsider_after_minutes=1))
            old=store.current(); now[0]+=301; claim=store.claim(packet(life,journal))
            provider=NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(proposal(materials,revision=1)))))
            await choose(provider,claim)
            sent=json.loads(provider.text_chat.call_args.kwargs['prompt'])
            self.assertNotIn('reason',sent['current'])
            self.assertEqual(sent['current']['question'],old['question'])
            self.assertEqual(store.current(),old)
            provider.text_chat.assert_awaited_once()


class FailurePersistence(unittest.TestCase):
    def test_reading_failure_survives_a_successful_writing_alternative(self):
        with tempfile.TemporaryDirectory() as tmp:
            life = seed_works(Path(tmp)); plan = life.schedule()
            self.assertTrue(life.claim(plan['day'], 'reading'))
            life.fail(plan['day'], ValueError())
            failed = life.schedule()
            self.assertTrue(life.claim(plan['day'], 'writing', failed_at=failed['updated']))
            life.finish(plan['day'], {'id': 'life:' + plan['day'], 'kind': 'writing'})
            self.assertEqual(life.schedule()['status'], 'completed')
            self.assertTrue(any(row['day'] == plan['day'] and row['kind'] == 'reading' and row['status'] == 'failed'
                for row in LifeStore(Path(tmp)).outcomes()))

    def test_specific_failure_and_execution_state_survive_restart_without_duplicate_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            life=LifeStore(Path(tmp)); plan=life.schedule(); life.claim(plan['day'],'reading')
            life.fail(plan['day'],ReadingError('fetch','timeout'))
            reopened=LifeStore(Path(tmp)); outcomes=reopened.outcomes()
            self.assertEqual(len(outcomes),1)
            self.assertEqual(outcomes[0]['error'],'reading_fetch_timeout')
            self.assertFalse(reopened.activity_state()['can_start_reading_today'])
            self.assertTrue(reopened.activity_state()['failed_reading_may_choose_writing'])
            self.assertEqual(failure_code(NS(code='secret-provider-response')),'SimpleNamespace')

    def test_previous_step_rejection_is_not_feedback_for_a_new_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); life=seed_works(root); journal=SelfJournal(root); now=[time.time()]
            direction=DirectionStore(root,clock=lambda:now[0]); materials=packet(life,journal)
            direction.finish(direction.claim(materials),proposal(materials,reconsider_after_minutes=1))
            record=life.records(1)[0]
            direction.save_assessment(1,record,dict(accepted=False,evidence='',reason='上一版步骤未解决'))
            self.assertEqual(len(packet(life,journal)['step_assessments']),1)
            now[0]+=301; materials=packet(life,journal)
            direction.finish(direction.claim(materials),proposal(materials,revision=1))
            self.assertEqual(packet(life,journal)['step_assessments'],[])
            self.assertEqual(len(direction.assessments()),1)

    def test_legacy_alternative_is_labelled_from_its_choice_ledger_not_its_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); life=seed_works(root); journal=SelfJournal(root); record=life.records(1)[0]
            journal.reserve_choice(record['day']+':adaptation',{'kind':'writing'})
            _,works=interest_context(journal,life.verified_records(60))
            self.assertEqual(works[0]['activity_context'],'alternative_after_failed_reading')
            self.assertNotIn('activity_context',life.records(1)[0])
            today=life.schedule(); life.claim(today['day'],'writing'); life.finish(today['day'],{'kind':'writing'})
            state=packet(life,journal)['execution_state']
            self.assertEqual(state['completed_today'],1)
            self.assertFalse(state['can_start_reading_today'])

    def test_public_source_validation_does_not_allow_local_or_credential_urls(self):
        self.assertEqual(checked_url('https://docs.example.org/article?q=x#part'),'https://docs.example.org/article?q=x')
        for value in ('https://localhost/a','https://127.0.0.1/a','https://127.1/a','https://10.0.0.1/a',
                'https://[::1]/a','https://name:password@example.org/a','https://host.internal/a','https://example.org:444/a'):
            with self.subTest(value=value),self.assertRaises(ValueError):checked_url(value)


if __name__ == '__main__':
    unittest.main()
