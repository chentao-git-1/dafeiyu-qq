"""Native runner/adapter and bounded failure recovery; no network or QQ sends."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

import test_cognition as base
f=base.fixture
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import Message
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.provider.entities import LLMResponse,ProviderRequest
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from data.plugins.dafeiyu_dialogue.generation import request_contract,parse_generation
from data.plugins.dafeiyu_dialogue.provider_contract import ProviderContracts,arm_contract
from data.plugins.dafeiyu_dialogue.review import UNAVAILABLE


class NativeContract(unittest.IsolatedAsyncioTestCase):
    async def test_native_context_request_is_json_while_copied_summary_and_foreign_request_are_not(self):
        provider=ProviderOpenAIOfficial({'id':'synthetic','type':'openai_chat_completion','model':'synthetic',
            'key':['synthetic'],'modalities':['text','tool_use']},{})
        contracts=ProviderContracts();original=ToolLoopAgentRunner._sanitize_contexts_for_provider
        contracts.install(provider);nonce=arm_contract(True)
        req=ProviderRequest(prompt='你好',system_prompt=request_contract('trusted operator rules',{},False,nonce),
            contexts=[dict(role='assistant',content='以前实际发出的文字。')])
        before=copy.deepcopy(req.contexts);contracts.bind_request(req,nonce)
        payloads=[]
        async def query(payload,*args,**kwargs):
            payloads.append(copy.deepcopy(payload))
            return LLMResponse(role='assistant',completion_text=base.envelope('嗯，在呢。'))
        provider._query=AsyncMock(side_effect=query)
        runner=ToolLoopAgentRunner()
        try:
            await runner.reset(provider,req,ContextWrapper(context=None),None,BaseAgentRunHooks())
            # The real runner puts system messages in contexts and starts another task.
            async def native():return [r async for r in runner._iter_llm_responses()]
            replies=await asyncio.create_task(native())
            self.assertEqual(parse_generation(replies[0].completion_text).reply,'嗯，在呢。')
            self.assertEqual(payloads[0]['response_format'],{'type':'json_object'})
            self.assertIn('BEGIN_DAFEIYU_GENERATION',payloads[0]['messages'][0]['content'])
            self.assertEqual(json.loads(payloads[0]['messages'][1]['content'])['reply'],before[0]['content'])
            copied=copy.deepcopy(runner.run_context.messages)
            await provider.text_chat(contexts=copied)
            self.assertNotIn('response_format',payloads[1])
            self.assertNotIn('BEGIN_DAFEIYU_GENERATION',payloads[1]['messages'][0]['content'])
            self.assertIn('trusted operator rules',payloads[1]['messages'][0]['content'])
            foreign=ToolLoopAgentRunner()
            await foreign.reset(provider,ProviderRequest(prompt='其他会话',system_prompt=req.system_prompt),
                ContextWrapper(context=None),None,BaseAgentRunHooks())
            _=[r async for r in foreign._iter_llm_responses()]
            self.assertNotIn('response_format',payloads[2])
            self.assertEqual(req.contexts,before)
        finally:
            contracts.close();arm_contract(False);await provider.terminate()
        self.assertIs(ToolLoopAgentRunner._sanitize_contexts_for_provider,original)


class Recovery(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=f.NativeHooks.asyncSetUp
    run_hooks=f.NativeHooks.run_hooks
    ready=base.SingleGeneration.ready
    async def asyncTearDown(self):
        await self.plugin.terminate();await f.NativeHooks.asyncTearDown(self)

    async def test_confirmed_image_suppresses_invalid_tail_and_excludes_unsent_text_from_history(self):
        event,_=await self.ready('我看一下')
        event.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text='',media_types=['image'])])
        response=NS(completion_text='已经画好了。',tools_call_name=[])
        await self.plugin.review_response(event,response)
        self.assertEqual(response.completion_text,'')
        self.assertTrue(event.get_extra('dafeiyu_generation_delivery_only'))
        self.assertFalse(event.get_extra('dafeiyu_generation_valid'))
        self.context.get_using_provider_async.return_value.text_chat.assert_not_awaited()
        last=Message(role='assistant',content='已经画好了。')
        await self.plugin.save_native_context(event,NS(messages=[last]),response)
        self.assertTrue(last._no_save)
        event.send.assert_not_awaited()

    async def test_unknown_delivery_still_requires_recovery_and_cannot_be_treated_as_sent(self):
        event,_=await self.ready('普通聊天')
        event.set_extra('dafeiyu_reply_delivery',dict(status='unknown'))
        response=NS(completion_text='非协议文字',tools_call_name=[])
        self.context.get_using_provider_async.return_value.text_chat=AsyncMock(return_value=NS(completion_text=base.envelope('嗯。')))
        await self.plugin.review_response(event,response)
        self.assertEqual(response.completion_text,'嗯。')
        self.assertFalse(event.get_extra('dafeiyu_generation_delivery_only',False))
        self.assertEqual(self.plugin.review_stats['generation_repaired'],1)

    async def test_rejected_revision_is_rewritten_once_from_precise_violations_and_verified_again(self):
        event,_=await self.ready('你在做什么')
        first='我刚出门看了月亮。';better='我在想，月光这种题材画成蓝灰色可能挺好。'
        revise=lambda old,new:json.dumps(dict(decision='revise',issues=[dict(segment_id=0,kind='unsupported_fact',span=old)],
            revisions=[dict(segment_id=0,text=new)]),ensure_ascii=False)
        call=AsyncMock(side_effect=[NS(completion_text=revise('在公园散步',first)),
            NS(completion_text=json.dumps(dict(supported=False,violations=[dict(segment_id=0,span='出门看了月亮')]))),
            NS(completion_text=revise('出门看了月亮',better)),NS(completion_text='{"supported":true,"violations":[]}')])
        self.context.get_using_provider_async.return_value=NS(text_chat=call)
        raw=json.loads(base.envelope('我在公园散步。'));raw['focus']=['self']
        response=NS(completion_text=json.dumps(raw),tools_call_name=[])
        await self.plugin.review_response(event,response)
        self.assertEqual(response.completion_text,better);self.assertEqual(call.await_count,4)
        packet=json.loads(call.await_args_list[2].kwargs['prompt'])
        self.assertEqual(packet['materials']['rejected_revision']['violations'],[dict(segment_id=0,span='出门看了月亮')])
        self.assertEqual(self.plugin.review_stats['revision_repaired'],1)

    async def test_second_rejection_stops_without_sending_unsupported_text(self):
        event,_=await self.ready('你在做什么')
        revise=json.dumps(dict(decision='revise',issues=[dict(segment_id=0,kind='unsupported_fact',span='散步')],
            revisions=[dict(segment_id=0,text='我在散步。')]))
        rejected=json.dumps(dict(supported=False,violations=[dict(segment_id=0,span='散步')]))
        call=AsyncMock(side_effect=[NS(completion_text=revise),NS(completion_text=rejected),
            NS(completion_text=revise),NS(completion_text=rejected)])
        self.context.get_using_provider_async.return_value=NS(text_chat=call)
        raw=json.loads(base.envelope('我在散步。'));raw['focus']=['self']
        response=NS(completion_text=json.dumps(raw),tools_call_name=[])
        await self.plugin.review_response(event,response)
        self.assertEqual(response.completion_text,UNAVAILABLE);self.assertEqual(call.await_count,4)
        self.assertFalse(event.get_extra('dafeiyu_generation_valid'));event.send.assert_not_awaited()


if __name__=='__main__':unittest.main()
