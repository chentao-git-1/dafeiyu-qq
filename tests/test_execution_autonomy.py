"""Self-chosen execution time, changing one's mind and semantic result feedback."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
import test_cognition as base
from test_self_direction import proposal
from data.plugins.dafeiyu_life.direction import DirectionStore,packet,assess_step
from data.plugins.dafeiyu_life.life import LifeStore,LifeEngine,TZ
from data.plugins.dafeiyu_life.planning import SelfJournal


def generated():
    return dict(note='潮水退去后，沙滩上留下细细的纹路。我把最后一句留在那里。',thought='留一点空白也能表达变化。',
        story='小屋的灯光落在手帐边缘。',evidence='',reflection=dict(insight='用余下的痕迹表达变化',
        evidence='沙滩上留下细细的纹路',next_question='如果只留下声音会怎样',status='active'))


def context(provider):
    controller=NS(owner_policy_enabled=lambda flag:True,natural=lambda:NS(actor_owner=lambda:base.OWNER),metered_provider=lambda p,*args:p)
    return NS(get_registered_star=lambda name:NS(activated=True,star_cls=controller),get_using_provider_async=AsyncMock(return_value=provider))


class ExecutionAutonomy(unittest.IsolatedAsyncioTestCase):
    async def test_own_time_at_night_and_reconsidering_no_action_keep_the_real_activity_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);life=LifeStore(root);journal=SelfJournal(root)
            now=[dt.datetime(2026,11,1,23,tzinfo=TZ).timestamp()];direction=DirectionStore(root,clock=lambda:now[0])
            materials=packet(life,journal);choice=proposal(materials,execute_after_minutes=10,reconsider_after_minutes=1)
            direction.finish(direction.claim(materials),choice)
            provider=NS(text_chat=AsyncMock(side_effect=[NS(completion_text=json.dumps(generated(),ensure_ascii=False)),
                NS(completion_text=json.dumps(dict(accepted=False,evidence='',reason='作品还没有实际解决下一步的具体要求')))]))
            engine=LifeEngine(life,context(provider));engine._direction=direction
            none=dict(kind='none',project_id='',expected_version=0,title='',question='',source_ids=[],reason='先不行动',direction_version=0)
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]), \
                    patch('data.plugins.dafeiyu_life.planning.SelfJournal',side_effect=lambda path:SelfJournal(path,clock=lambda:now[0])), \
                    patch('data.plugins.dafeiyu_life.planning.choose_activity',AsyncMock(return_value=none)) as select:
                self.assertEqual((await engine.advance())['status'],'waiting_for_own_time');select.assert_not_awaited()
                now[0]+=601;self.assertEqual((await engine.advance())['status'],'chose_no_activity')
                await engine.advance();select.assert_awaited_once();self.assertEqual(life.count(),0)
                now[0]+=301;materials=packet(life,journal)
                direction.finish(direction.claim(materials),proposal(materials,1,execute_after_minutes=0))
                select.return_value=dict(kind='writing',project_id='',expected_version=0,title='潮汐的余白',question='写一段留白的文字',
                    source_ids=['operator:seed'],reason='现在想推进自己的打算',direction_version=2)
                result=await engine.advance();self.assertTrue(result['advanced']);self.assertEqual(life.count(),1)
                self.assertEqual(direction.current()['status'],'active')
                self.assertEqual(direction.assessments()[0]['accepted'],0)
                self.assertEqual(provider.text_chat.await_count,2)
                await engine.advance();self.assertEqual(provider.text_chat.await_count,2);self.assertEqual(life.count(),1)

    async def test_step_check_failure_preserves_real_work_and_keeps_the_intention_unconfirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);life=LifeStore(root);journal=SelfJournal(root);direction=DirectionStore(root)
            materials=packet(life,journal);direction.finish(direction.claim(materials),proposal(materials))
            provider=NS(text_chat=AsyncMock(side_effect=[NS(completion_text=json.dumps(generated(),ensure_ascii=False)),TimeoutError()]))
            engine=LifeEngine(life,context(provider));engine._direction=direction
            selected=dict(kind='writing',project_id='',expected_version=0,title='潮汐的余白',question='写一段留白的文字',
                source_ids=['operator:seed'],reason='推进自己的打算',direction_version=1)
            with patch('data.plugins.dafeiyu_life.planning.choose_activity',AsyncMock(return_value=selected)):
                self.assertTrue((await engine.advance(bootstrap=True))['advanced'])
            self.assertEqual(life.count(),1);self.assertEqual(len(life.verified_records()),1)
            self.assertEqual(direction.current()['status'],'active')
            self.assertEqual(direction.assessments()[0]['accepted'],0)
            self.assertIn('TimeoutError',direction.assessments()[0]['reason'])

    async def test_failure_feedback_and_independent_exact_evidence_are_used_without_more_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);life=LifeStore(root);plan=life.schedule();life.claim(plan['day'],'reading');life.fail(plan['day'],ValueError())
            materials=packet(life,SelfJournal(root));outcomes=materials['activity_outcomes']
            self.assertEqual(outcomes[0]['status'],'failed');self.assertEqual(outcomes[0]['kind'],'reading')
            self.assertIn(outcomes[0]['id'],{r['id'] for r in materials['sources']});self.assertEqual(life.count(),0)
            record=dict(id='work',kind='writing',note='潮水退去，留下细细的纹路。',thought='余白也会说话',source={})
            provider=NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(
                dict(accepted=True,evidence='这句话没有出现在作品里',reason='只看类型相同')))))
            with self.assertRaises(ValueError):await assess_step(provider,proposal(materials),record)
            provider.text_chat.return_value=NS(completion_text=json.dumps(dict(accepted=True,evidence='留下细细的纹路',reason='具体表达步骤已得到作品支持')))
            self.assertTrue((await assess_step(provider,proposal(materials),record))['accepted'])
            self.assertIsNone(provider.text_chat.call_args.kwargs['func_tool']);self.assertEqual(life.count(),0)


if __name__=='__main__':unittest.main()
