"""Model-owned public posts: no content vetoes, reviewers, or paid test calls."""
import asyncio
from contextlib import closing
import importlib
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_evening_native as f
from data.plugins.astrbot_plugin_qzone.core.generation_error import PostGenerationError
images=importlib.import_module('data.plugins.astrbot_plugin_qzone.core.post_images')


class FreeExpression(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=f.Diary.asyncSetUp
    candidate=f.Diary.candidate
    due=f.Diary.due
    row=f.Diary.row

    def writer(self, text):
        obj=f.llm.LLMAction.__new__(f.llm.LLMAction);obj.editorial=self.editorial
        obj.cfg=NS(llm=NS(post_provider_id='fake',post_prompt=''),client=object())
        provider=Mock(spec=f.Provider);provider.text_chat=AsyncMock(return_value=NS(completion_text=text))
        obj._get_provider=lambda *args:provider
        obj._get_persona_context=AsyncMock(side_effect=AssertionError('private persona access'))
        obj._get_group_context=AsyncMock(side_effect=AssertionError('private conversation access'))
        return obj,provider

    async def test_primary_text_is_not_vetoed_by_length_quotes_topic_or_similarity(self):
        history=[dict(text='今晚还想写天空、大海和月亮。',created_at=self.now.timestamp(),plan={})]*3
        self.editorial.recent=AsyncMock(return_value=history)
        for text in ('哈。','“天空”和“大海”，今天我还是想写它们。',history[0]['text'],'一个新想法。'*250):
            with self.subTest(text_length=len(text)):
                obj,provider=self.writer(text)
                self.assertEqual(await obj.generate_post(diary=True,now=self.now.timestamp()),text)
                provider.text_chat.assert_awaited_once()
                args=provider.text_chat.await_args.kwargs
                self.assertEqual(args['request_max_retries'],1)
                self.assertIsNone(args['func_tool']);self.assertEqual(args['contexts'],[])
                obj._get_persona_context.assert_not_awaited();obj._get_group_context.assert_not_awaited()

    async def test_plan_offers_material_without_program_selected_topic_or_frame(self):
        plan,history=await self.editorial.choose_plan(diary=True,now=self.now.timestamp())
        self.assertEqual(plan['topic'],'');self.assertNotIn('frame',plan)
        prompt=self.editorial.writing_prompt(plan,history)
        self.assertNotIn('长度：',prompt);self.assertNotIn('形式：',prompt)
        self.assertNotIn('80到160',prompt);self.assertIn('公开资料',prompt)
        self.assertEqual((await self.editorial.choose_plan('机器人也能聊哲学吗'))[0]['topic'],'机器人也能聊哲学吗')

    async def test_permission_recheck_does_not_reintroduce_content_screening(self):
        text='“天空”还想再写一遍。'*150
        await self.editorial.remember_candidate(text,dict(diary=True,day='2026-10-28',chat_topics=[]))
        self.assertTrue((await self.editorial.ensure_current(text,self.now.timestamp()))['diary'])
        await self.editorial.discard_candidate(text)
        with self.assertRaises(f.daily.DiaryCancelled):
            await self.editorial.ensure_current(text,self.now.timestamp())

    async def test_status_reports_no_reviewers_or_fixed_editorial_rules(self):
        result=await self.editorial.status()
        for key in ('text_review','image_review','content_similarity_filter','fixed_topic_selection','fixed_length_target'):
            self.assertIs(result[key],False)
        self.assertTrue(result['model_owned_content']);self.assertEqual(result['generation_attempts'],1)

    async def test_generated_image_is_returned_once_without_quality_model(self):
        draw=NS(generate_for_qzone=AsyncMock(return_value='/synthetic/image.png'))
        context=NS(get_registered_star=lambda *a:NS(activated=True,star_cls=draw))
        editorial=NS(visual_plan=AsyncMock(side_effect=AssertionError('preset image style')),
            review_image=AsyncMock(side_effect=AssertionError('image reviewer')))
        self.assertEqual(await images.prepare_post_images(context,'一段自由表达',editorial=editorial),['/synthetic/image.png'])
        draw.generate_for_qzone.assert_awaited_once();editorial.review_image.assert_not_awaited()
        editorial.visual_plan.assert_not_awaited()

    async def test_retired_image_review_never_resolves_a_model(self):
        self.editorial.context.get_provider_by_id=Mock(side_effect=AssertionError('extra model'))
        result=await self.editorial.review_image('模型的正文','/synthetic/image.png')
        self.assertEqual(result['reason'],'review_disabled')
        self.editorial.context.get_provider_by_id.assert_not_called()

    async def test_long_post_is_published_once_without_body_truncation(self):
        self.text='让一个想法顺着自己的节奏展开。'*100
        await self.candidate();self.service.llm.generate_post.return_value=self.text
        await self.due();await self.publisher._run_task('2026-10-28')
        await self.publisher._run_task('2026-10-28')
        self.assertEqual(len(self.outbound),1);self.assertEqual(self.outbound[0]['text'],self.text)
        self.assertEqual(self.row()[1],'published')

    async def test_existing_own_image_has_no_review_call(self):
        self.editorial.own_image=AsyncMock(return_value='/synthetic/completed.png')
        self.editorial.review_image=AsyncMock(side_effect=AssertionError('extra model'))
        await self.due();await self.publisher._run_task('2026-10-28')
        self.assertEqual(self.outbound[0]['images'],['/synthetic/completed.png'])
        self.editorial.review_image.assert_not_awaited();self.image_mock.assert_not_awaited()

    async def test_provider_failure_has_safe_specific_code_and_no_second_call(self):
        obj,provider=self.writer('')
        error=RuntimeError('sk-synthetic-private-key provider body');error.status_code=429
        provider.text_chat.side_effect=error
        with self.assertRaises(PostGenerationError) as caught:
            await obj.generate_post(diary=True,now=self.now.timestamp())
        self.assertEqual(str(caught.exception),'provider_http_429')
        self.assertNotIn('private-key',str(caught.exception));provider.text_chat.assert_awaited_once()

    async def test_timeout_and_empty_output_have_different_codes_without_retry(self):
        for failure,code in ((TimeoutError(),'provider_timeout'),(None,'empty_output')):
            obj,provider=self.writer('');provider.text_chat.side_effect=failure
            with self.assertRaises(PostGenerationError) as caught:
                await obj.generate_post(diary=True,now=self.now.timestamp())
            self.assertEqual(caught.exception.code,code);provider.text_chat.assert_awaited_once()

    async def test_failure_code_is_persisted_before_any_public_write(self):
        self.service.llm.generate_post.side_effect=PostGenerationError('provider_http_401')
        await self.due()
        with patch.object(f.daily,'problem',new=AsyncMock()):
            await self.publisher._run_task('2026-10-28')
        self.service.publish_post.assert_not_awaited();self.image_mock.assert_not_awaited()
        with closing(f.sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT status,error_type FROM daily_publish_runs WHERE day=?',('2026-10-28',)).fetchone(),('failed','provider_http_401'))

    async def test_cancellation_is_not_rewritten_as_a_generation_failure(self):
        obj,provider=self.writer('');provider.text_chat.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await obj.generate_post(diary=True,now=self.now.timestamp())
        provider.text_chat.assert_awaited_once()


if __name__=='__main__':unittest.main()
