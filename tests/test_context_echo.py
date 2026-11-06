"""Prevent repeated evidence from priming replies; preserve source history and explicit recall."""
import asyncio
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import test_cognition as base
from dafeiyu.dialogue.context_builder import compose,automatic_items
from dafeiyu.dialogue.contracts import CatalogSnapshot,MemoryItem,TurnContext
from dafeiyu.dialogue.indexes import SourceIndex
from dafeiyu.dialogue.retrieval import recall_candidates
from dafeiyu.runtime.outbound import TZ,MARKER,anchor,reply_note,project_history
from data.plugins.dafeiyu_dialogue.provider_contract import ProviderContracts,arm_contract
from data.plugins.dafeiyu_dialogue.generation import request_contract
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.provider.entities import LLMResponse,ProviderRequest
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

NOW=dt.datetime(2026,11,6,19,tzinfo=TZ).timestamp()
OWNER='7000000001';ORIGIN='dafeiyu-qq:FriendMessage:'+OWNER


def sent():return dict(at=NOW-3600,text='手帐上留了一页空白。',id='123')
def user(text):return dict(role='user',content=text+'<system_reminder>Current datetime: 2026-11-06 18:30 (CST)</system_reminder>')


class AutomaticRecall(unittest.TestCase):
    def test_same_question_does_not_reintroduce_old_answers_but_explicit_recall_keeps_them(self):
        turn=TurnContext(OWNER,ORIGIN,'dafeiyu-qq','test',NOW)
        pairs=[MemoryItem('exchange:'+str(i),OWNER,ORIGIN,'session','exchange',
            json.dumps(dict(human='你在干嘛呢',reply='我盯着那一页空白。'),ensure_ascii=False),NOW-i-1,'confirmed_exchange') for i in range(12)]
        fact=MemoryItem('fact:1',OWNER,ORIGIN,'session','fact','本人明确说喜欢爵士乐。',NOW-50,'versioned_human_evidence')
        snapshot=CatalogSnapshot(tuple([*pairs,fact]))
        with tempfile.TemporaryDirectory() as tmp:
            index=SourceIndex(Path(tmp)/'index.db');offered=automatic_items(snapshot.items);index.sync(turn,offered)
            ranked=index.rank(turn,'你在干嘛呢',offered)
            built=compose('',turn,snapshot,'你在干嘛呢',ranked=ranked,contract=True)
            self.assertEqual(built.evidence['memory'],[])
            self.assertNotIn('那一页',built.prompt)
            jazz=compose('',turn,snapshot,'聊聊爵士乐',ranked=index.rank(turn,'聊聊爵士乐',offered),contract=True)
            self.assertEqual([r['id'] for r in jazz.evidence['memory']],['fact:1'])
        explicit=recall_candidates(snapshot.items,'你以前为什么说那一页',OWNER)
        self.assertTrue(any(r.kind=='exchange' for r in explicit));self.assertEqual(len(snapshot.items),13)
        # A stale supplied rank cannot reintroduce excluded raw conversations.
        self.assertEqual(compose('',turn,snapshot,'你在干嘛呢',ranked=pairs,contract=True).evidence['memory'],[])


class HistoryProjection(unittest.TestCase):
    def test_only_proven_legacy_metadata_is_removed_from_a_copy(self):
        row=sent();note=reply_note([row],'新话题',[])
        old=user('我在打游戏');old['content']+=note
        history=[dict(role='assistant',content=anchor(row)),old,dict(role='assistant',content='那一页先放着'),user('介绍自己')]
        before=copy.deepcopy(history);projected=project_history(history,[row])
        self.assertNotIn(MARKER,projected[1]['content']);self.assertIn('我在打游戏',projected[1]['content'])
        self.assertEqual(projected[0],history[0]);self.assertEqual(projected[2],history[2])
        self.assertEqual(history,before)
        self.assertEqual(project_history(projected,[row]),projected)

    def test_unknown_rows_current_input_quotes_and_added_user_words_are_preserved(self):
        row=sent();note=reply_note([row],'新话题',[]);old=user('聊天');old['content']+=note
        history=[old,user('新消息')]
        self.assertEqual(project_history(history,[]),history)
        self.assertEqual(project_history(history,[dict(row,text='另一个人的消息')]),history)
        self.assertEqual(project_history([old],[row]),[old])
        quoted=[dict(role='user',content='请解释这段文字：'+note),user('继续')]
        self.assertEqual(project_history(quoted,[row]),quoted)
        added=copy.deepcopy(history);added[0]['content']+='\n我还有一句话'
        self.assertEqual(project_history(added,[row]),added)

    def test_multimodal_input_keeps_image_and_user_text(self):
        row=sent();note=reply_note([row],'新话题',[])
        original=[dict(role='user',content=[dict(type='text',text=user('看看图片')['content']),
            dict(type='image_url',image_url={'url':'data:image/png;base64,synthetic'}),dict(type='text',text=note)]),user('再聊一会儿')]
        before=copy.deepcopy(original);projected=project_history(original,[row])
        self.assertEqual(projected[0]['content'],original[0]['content'][:2]);self.assertEqual(original,before)


class NativeProjection(unittest.IsolatedAsyncioTestCase):
    async def test_model_sees_clean_history_but_runner_and_stored_request_keep_originals(self):
        row=sent();old=user('我在打游戏');old['content']+=reply_note([row],'新话题',[])
        provider=ProviderOpenAIOfficial({'id':'synthetic','type':'openai_chat_completion','model':'synthetic',
            'key':['synthetic'],'modalities':['text','tool_use']},{})
        contracts=ProviderContracts();contracts.install(provider);nonce=arm_contract(True)
        req=ProviderRequest(prompt='介绍一下自己',system_prompt=request_contract('operator',{},False,nonce),
            contexts=[dict(role='assistant',content=anchor(row)),old,dict(role='assistant',content='聊游戏')])
        before=copy.deepcopy(req.contexts);contracts.bind_request(req,nonce,outgoing=[row]);payloads=[]
        async def query(payload,*args,**kwargs):
            payloads.append(copy.deepcopy(payload));return LLMResponse(role='assistant',completion_text=base.envelope('我是鲸鲸。'))
        provider._query=AsyncMock(side_effect=query);runner=ToolLoopAgentRunner()
        try:
            await runner.reset(provider,req,ContextWrapper(context=None),None,BaseAgentRunHooks())
            _=[r async for r in runner._iter_llm_responses()]
            wire=payloads[0]['messages']
            self.assertFalse(any(MARKER in str(m.get('content')) for m in wire if m.get('role')=='user'))
            self.assertEqual(sum(row['text'] in str(m.get('content')) for m in wire),1)
            self.assertEqual(req.contexts,before);provider._query.assert_awaited_once()
            self.assertTrue(any(MARKER in str(m.content) for m in runner.run_context.messages if m.role=='user'))
        finally:contracts.close();arm_contract(False);await provider.terminate()


if __name__=='__main__':unittest.main()
