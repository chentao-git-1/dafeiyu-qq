"""New materials, model-selected curiosity and real same-day continuity; no network or models."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from test_nightly_agenda import choice, day_plan, fixture, generated, response, stamp
from dafeiyu.life.agenda import materials, parse_plan
from dafeiyu.life.planning import SelfJournal, interest_context
from dafeiyu.life.reading import ReadingError, explore, read_article, selected_discovery
from dafeiyu.dialogue.growth import own_experiences
from dafeiyu.dialogue.self_state import self_state
from data.plugins.dafeiyu_life.life import LifeStore


RESULTS=[dict(url='https://example.org/sound',title='回声与空间的形状',snippet='声音在不同表面反射，形成不同的空间听觉。'),
    dict(url='https://example.org/letters',title='文字形状的演变',description='书写工具和字形的关系。')]


def discovered(source, selected=True):
    value=generated()
    value.update(note='浏览时看到回声与空间的形状，我想知道不同房间的回声为什么不同。',
        thought='声音也能描绘空间，想接着看看。',story='',selected_source_id=source['candidates'][0]['id'] if selected else None,
        evidence=RESULTS[0]['title'] if selected else '',reflection=dict(insight='对声音的空间感产生兴趣',
            evidence='回声与空间的形状',next_question='不同空间如何改变回声',status='active'))
    return value


def discovery_plan():
    value=day_plan(3)
    value['items'][0]['activity']=choice('浏览自己想了解的内容',kind='exploring',question='声音与建筑的有趣现象')
    value['items'][1].update(after=['09:00'],activity=choice('继续了解发现的内容',kind='reading',question='阅读早上选中的来源，了解它具体说了什么'))
    value['items'][2].update(after=['09:00','10:00'],activity=choice('把发现写成作品',question='根据今天真实看过的内容，自选一种表达形式'))
    return value


class DiscoveryTransport(unittest.IsolatedAsyncioTestCase):
    async def test_one_search_keeps_public_results_and_no_full_article_claim(self):
        tool=AsyncMock(return_value={'results':[dict(url='http://127.0.0.1',title='private'),*RESULTS,RESULTS[0]]})
        source=await explore(tool,'声音与建筑的有趣现象')
        tool.assert_awaited_once_with('search',{'query':'声音与建筑的有趣现象','count':8,'need_content':False})
        self.assertEqual(len(source['candidates']),2)
        self.assertEqual(source['candidates'][1]['summary'],RESULTS[1]['description'])
        self.assertEqual(source['content_nature'],'search_summaries_not_full_articles')
        selected=selected_discovery(source,discovered(source))
        self.assertEqual(selected['selected']['url'],RESULTS[0]['url'])
        self.assertNotIn('candidates',selected)
        self.assertIsNone(selected_discovery(source,discovered(source,False))['selected'])
        for fields in ({'selected_source_id':'invented'}, {'evidence':'模型编造的引用'}, {'selected_source_id':None}):
            with self.subTest(fields=fields),self.assertRaises(ValueError):selected_discovery(source,discovered(source)|fields)

    async def test_reading_selected_discovery_fetches_exact_url_without_search(self):
        tool=AsyncMock(return_value=dict(title='回声与空间',content='声音从表面反射后再到达耳朵。'))
        source=await read_article(tool,'读这次发现',RESULTS[0]['url'])
        tool.assert_awaited_once_with('fetch',{'url':RESULTS[0]['url']})
        self.assertEqual(source['url'],RESULTS[0]['url']);self.assertIn('body',source)
        with self.assertRaises(ValueError):await read_article(tool,'读资料','https://127.0.0.1/private')
        self.assertEqual(tool.await_count,1)

    async def test_search_failure_is_not_retried(self):
        tool=AsyncMock(return_value={'results':[]})
        with self.assertRaises(ReadingError):await explore(tool,'随自己兴趣看看')
        tool.assert_awaited_once()


class DiscoveryAgenda(unittest.IsolatedAsyncioTestCase):
    async def test_actual_discovery_flows_into_reading_and_writing_without_extra_model_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];provider=NS(text_chat=AsyncMock());source=await explore(AsyncMock(return_value={'results':RESULTS}),'声音与建筑')
            read=generated();read['evidence']='声音从表面反射后再到达耳朵'
            provider.text_chat.side_effect=[response(discovery_plan()),response(discovered(source)),response(read),response(generated())]
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider);engine.owner_wishes='希望生活更丰富，主题由自己决定'
                async def web(name,payload):
                    ongoing=self_state(progress=life.in_progress())['verified_in_progress']
                    self.assertEqual(ongoing['kind'],'exploring' if name=='search' else 'reading')
                    return {'results':RESULTS} if name=='search' else dict(title='回声与空间',content=read['evidence']+'。')
                engine.web_tool=AsyncMock(side_effect=web)
                self.assertTrue(await engine.plan_next_day())
                packet=json.loads(provider.text_chat.call_args.kwargs['prompt'])
                self.assertEqual(packet['owner_wishes'],engine.owner_wishes);self.assertIn('exploring',packet['allowed_kinds'])
                self.assertEqual(engine.agenda_status()['tomorrow']['items'][2]['after'],['09:00','10:00'])
                self.assertEqual(own_experiences(life,now=now[0]),[])
                for hour in (9,10,11):
                    now[0]=stamp(7,hour);self.assertTrue((await engine.advance())['advanced'])
                self.assertEqual(provider.text_chat.await_count,4)
                self.assertEqual([call.args[0] for call in engine.web_tool.await_args_list],['search','fetch'])
                self.assertEqual(engine.web_tool.await_args_list[1].args[1]['url'],RESULTS[0]['url'])
                records=life.verified_records();self.assertEqual(len(records),3)
                exploration=next(r for r in records if r['kind']=='exploring')
                self.assertEqual(exploration['source']['selected']['url'],RESULTS[0]['url'])
                self.assertEqual(exploration['content_nature'],'search_summaries_not_full_articles')
                writing=next(r for r in records if r['kind']=='writing')
                self.assertIn(exploration['id'],writing['input_ids'])
                prompt=provider.text_chat.call_args.kwargs['prompt']
                self.assertIn(RESULTS[0]['url'],prompt);self.assertIn('摘要不等于已读全文',prompt)
                experiences=own_experiences(life,now=now[0]);self.assertEqual(len(experiences),3)
                self.assertEqual(next(r for r in experiences if r['payload']['activity']=='exploring')['payload']['activity_context'],
                    'browsed_search_summaries_not_full_articles')
                state,overview=interest_context(SelfJournal(life.root),records)
                self.assertEqual(sum(r['completed'] for r in state['recent_activity_distribution']),3)
                self.assertEqual(len(overview),3)

    async def test_no_discovery_or_failure_skips_dependent_work_but_allows_independent_work(self):
        for failed in (False,True):
            with self.subTest(failed=failed),tempfile.TemporaryDirectory() as tmp:
                now=[stamp()];value=discovery_plan();value['items'][2]['after']=[]
                source=await explore(AsyncMock(return_value={'results':RESULTS}),'声音与建筑')
                responses=[response(value),*([response(discovered(source,False))] if not failed else []),response(generated())]
                provider=NS(text_chat=AsyncMock(side_effect=responses))
                with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                    life,engine=fixture(Path(tmp),now,provider)
                    engine.exploring=AsyncMock(side_effect=ReadingError('search','timeout') if failed else None,return_value=source)
                    await engine.plan_next_day();now[0]=stamp(7,9)
                    if failed:
                        with self.assertRaises(ReadingError):await engine.advance()
                    else:self.assertTrue((await engine.advance())['advanced'])
                    now[0]=stamp(7,10);self.assertFalse((await engine.advance())['advanced'])
                    self.assertEqual(engine.agenda_status()['today']['items'][1]['status'],'skipped')
                    now[0]=stamp(7,11);self.assertTrue((await engine.advance())['advanced'])
                    engine.exploring.assert_awaited_once();self.assertEqual(provider.text_chat.await_count,2 if failed else 3)

    async def test_withdrawn_discovery_artifact_cannot_become_a_later_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[stamp()];source=await explore(AsyncMock(return_value={'results':RESULTS}),'声音与建筑')
            provider=NS(text_chat=AsyncMock(side_effect=[response(discovery_plan()),response(discovered(source))]))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=fixture(Path(tmp),now,provider);engine.exploring=AsyncMock(return_value=source)
                await engine.plan_next_day();now[0]=stamp(7,9);await engine.advance()
                (life.root/life.records(1)[0]['artifact']).unlink()
                now[0]=stamp(7,10);self.assertFalse((await engine.advance())['advanced'])
                self.assertEqual(provider.text_chat.await_count,2)


class PlanningMaterials(unittest.TestCase):
    def test_invalid_future_or_missing_dependencies_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            life=LifeStore(Path(tmp));packet=materials(life,SelfJournal(life.root),'2026-11-07',9,21,'2.00',now=stamp())
            for refs in (['11:00'],['10:00'],['08:00'],['09:00','09:00'],[1], '09:00'):
                value=discovery_plan();value['items'][1]['after']=refs
                with self.subTest(refs=refs),self.assertRaises(ValueError):parse_plan(json.dumps(value),packet)

    def test_repetition_is_reported_as_facts_without_copying_every_old_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal=SelfJournal(Path(tmp));project=journal.begin_project(choice('老兴趣'))
            records=[dict(id='life:'+str(i),day='2026-11-0'+str(i+1),kind='writing',project_id=project['id'],
                note='重复的旧文字'+str(i),source={}) for i in range(5)]
            state,overview=interest_context(journal,records)
            self.assertEqual(len(overview),1)
            self.assertEqual(state['recent_activity_distribution'][0]['completed'],5)
            self.assertEqual(state['recent_activity_distribution'][0]['distinct_days'],5)
            self.assertEqual(state['recent_activity_distribution'][0]['kinds'],{'writing':5})


if __name__=='__main__':unittest.main()
