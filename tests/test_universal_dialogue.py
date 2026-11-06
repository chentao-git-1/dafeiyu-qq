"""Admitted contacts/groups, attributed history, privacy boundaries and actual GCP rewrite hook."""
import asyncio,importlib,json,sqlite3,sys,time
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
import test_cognition as base
fixture=base.fixture
from data.plugins.dafeiyu_dialogue.contracts import TurnContext,MemoryItem,ACTIVE_KEY
from data.plugins.dafeiyu_dialogue.group_context import history
from data.plugins.dafeiyu_mention_guard.main import DafeiyuMentionGuard
from astrbot.core.message.components import At,Plain,Reply
from astrbot.core.platform.message_type import MessageType
from astrbot.api.event import MessageChain
OWNER,FRIEND,BOT=fixture.OWNER,fixture.FRIEND,fixture.BOT
GROUP,OTHER='7000000101','7000000102'
GORIGIN='dafeiyu-qq:GroupMessage:'+GROUP
def group_event(text,uid=FRIEND,group=GROUP,at=True):
    msg=fixture.event(text,uid).message_obj;msg.type=MessageType.GROUP_MESSAGE;msg.group_id=group;msg.session_id=group;msg.message_id='10001'
    msg.message=[At(qq=BOT),Plain(text)] if at else [Plain(text)]
    msg.raw_message.update(group_id=group,message_type='group',message=[{'type':'at','data':{'qq':BOT}}] if at else [{'type':'text','data':{'text':text}}])
    e=fixture.AiocqhttpMessageEvent(text,msg,fixture.PlatformMetadata('aiocqhttp','test','dafeiyu-qq'),group,object());e.send=AsyncMock()
    return e

class UniversalDialogue(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.NativeHooks.asyncSetUp
    asyncTearDown=fixture.NativeHooks.asyncTearDown
    async def setup_scope(self):
        self.context.get_config=lambda *a,**k:{'platform_settings':{'enable_id_white_list':True,'id_whitelist':[OWNER,FRIEND,GROUP,OTHER]}}
        self.plugin.config.update(generation_contract=True,learning_v2=True,retrieval_index_v2=True,all_allowed_private=True,all_allowed_groups=True)
        async def model(**kwargs):
            if '只核对本轮学习提案' in kwargs['system_prompt']:
                packet=json.loads(kwargs['prompt']);return NS(completion_text=json.dumps(dict(privacy_authorized=True,
                    assessments=[base.assessment(r['evidence'],index=i) for i,r in enumerate(packet['items'])])))
            return NS(completion_text='{"decision":"accept","issues":[],"revisions":[]}')
        self.context.get_using_provider_async=AsyncMock(return_value=NS(text_chat=AsyncMock(side_effect=model)))

    async def hooks(self,e,req):
        await self.plugin.choose_path(e,req);await self.natural.prepare(e,req)
        await self.natural.natural_delivery_context(e,req);await self.natural.prepare_spoken_style(e,req)
        await self.plugin.prepare(e,req)

    def seed(self,owner,origin,human,kind='fact',mid=None):
        store=self.plugin.learning_store();turn=TurnContext(owner,origin,'dafeiyu-qq',mid or human,time.time())
        item=base.item(human);item.update(kind=kind,summary=human,condition='只在这个会话' if kind=='preference' else '')
        source=store.begin(turn,human);eid=store.commit(source,human,[item],{},confirmed=True)[0]
        return eid,source

    async def test_all_existing_allowed_private_contacts_use_new_path(self):
        await self.setup_scope()
        for uid,expected in ((OWNER,True),(FRIEND,True),('7000000999',False)):
            e=fixture.event('你好',uid);req=fixture.ProviderRequest(prompt='你好')
            await self.hooks(e,req);self.assertEqual(e.get_extra(ACTIVE_KEY),expected)
            if expected:self.assertIn('BEGIN_DAFEIYU_GENERATION',req.system_prompt)

    async def test_group_requires_real_at_canonical_identity_and_human(self):
        await self.setup_scope()
        e=group_event('你好');req=fixture.ProviderRequest(prompt='你好');await self.hooks(e,req)
        self.assertTrue(e.get_extra(ACTIVE_KEY));self.assertEqual(self.plugin.identity(e).actor,FRIEND)
        for mode in ('no-at','wrong-group','notice','bot','cron','unknown-group'):
            e=group_event('你好',at=mode!='no-at',group='7000000998' if mode=='unknown-group' else GROUP)
            if mode=='wrong-group':e.message_obj.raw_message['group_id']=OTHER
            if mode=='notice':e.message_obj.raw_message['post_type']='notice'
            if mode=='bot':e.message_obj.raw_message['sender']={'is_bot':True}
            if mode=='cron':e.set_extra('cron_job',True)
            await self.plugin.choose_path(e,fixture.ProviderRequest(prompt='你好'))
            self.assertFalse(e.get_extra(ACTIVE_KEY),mode)
        e=group_event('只是引用',at=False);e.message_obj.message=[Reply(id='5',sender_id=BOT),Plain('只是引用')]
        await self.plugin.choose_path(e,fixture.ProviderRequest(prompt='只是引用'));self.assertFalse(e.get_extra(ACTIVE_KEY))

    async def test_group_catalog_excludes_private_other_group_and_other_person_preferences(self):
        await self.setup_scope()
        private,_=self.seed(FRIEND,fixture.ORIGIN.rsplit(':',1)[0]+':'+FRIEND,'私聊独有资料')
        own,_=self.seed(FRIEND,GORIGIN,'本群发言人的资料')
        other,_=self.seed(OWNER,GORIGIN,'本群另一成员的资料')
        preference,_=self.seed(OWNER,GORIGIN,'别人本群的偏好','preference')
        elsewhere,_=self.seed(FRIEND,'dafeiyu-qq:GroupMessage:'+OTHER,'另一个群的资料')
        turn=self.plugin.identity(group_event('资料'))
        ids={r.id for r in self.plugin.catalog().read(turn,time.time()).items}
        self.assertTrue({own,other}<=ids);self.assertTrue({private,preference,elsewhere}.isdisjoint(ids))
        private_turn=fixture.identity(FRIEND)
        self.assertNotIn(own,{r.id for r in self.plugin.catalog().read(private_turn,time.time()).items})

    async def test_group_learning_and_forget_all_are_owner_and_origin_bound(self):
        await self.setup_scope();store=self.plugin.learning_store()
        target,_=self.seed(FRIEND,GORIGIN,'当前群本人资料')
        peer,_=self.seed(OWNER,GORIGIN,'当前群其他人资料')
        outside,_=self.seed(FRIEND,'dafeiyu-qq:GroupMessage:'+OTHER,'另一群本人资料')
        private,_=self.seed(FRIEND,'dafeiyu-qq:FriendMessage:'+FRIEND,'私聊本人资料')
        turn=replace(self.plugin.identity(group_event('把本群关于我的资料都忘掉')),message_id='forget-group')
        source=store.begin(turn,'把本群关于我的资料都忘掉')
        self.assertEqual(store.apply_privacy(source,base.privacy('forget_all','把本群关于我的资料都忘掉'),[target]),1)
        with store.db() as db:remaining={r[0] for r in db.execute('SELECT id FROM learned')}
        self.assertNotIn(target,remaining);self.assertTrue({peer,outside,private}<=remaining)

    async def test_group_cannot_correct_private_record_even_with_forged_offered_version(self):
        await self.setup_scope();store=self.plugin.learning_store()
        eid,_=self.seed(FRIEND,'dafeiyu-qq:FriendMessage:'+FRIEND,'私聊偏好','preference')
        turn=self.plugin.identity(group_event('更正一下'));source=store.begin(turn,'更正一下')
        item=base.item('更正一下');item.update(supersedes=eid,expected_version=1)
        self.assertEqual(store.commit(source,'更正一下',[item],{eid:1},confirmed=True),[])

    async def test_group_native_forget_preserves_private_and_other_members_rows(self):
        await self.setup_scope();store=self.natural.exchange_store();private='dafeiyu-qq:FriendMessage:'+FRIEND
        for uid,origin,mid in ((FRIEND,GORIGIN,'one'),(OWNER,GORIGIN,'two'),(FRIEND,private,'three'),(FRIEND,'dafeiyu-qq:GroupMessage:'+OTHER,'four')):
            store.record(uid,origin,mid,'原话'+mid,'回答'+mid)
        self.natural.forget_semantic(FRIEND,GORIGIN)
        with store.connect() as db:rows={(r['qq'],r['origin']) for r in db.execute('SELECT qq,origin FROM exchanges')}
        self.assertNotIn((FRIEND,GORIGIN),rows);self.assertIn((OWNER,GORIGIN),rows);self.assertIn((FRIEND,private),rows)
        self.assertIn((FRIEND,'dafeiyu-qq:GroupMessage:'+OTHER),rows)

    async def test_group_management_never_returns_private_or_other_owner_records(self):
        await self.setup_scope();self.seed(OWNER,fixture.ORIGIN,'主人私聊独有资料')
        own,_=self.seed(OWNER,GORIGIN,'主人在本群的资料')
        e=group_event('查看资料',uid=OWNER);await self.hooks(e,fixture.ProviderRequest(prompt='查看资料'))
        result=json.loads(await self.plugin.cognition(e,'review'))
        self.assertEqual([r['id'] for r in result['records']],[own])
        self.assertFalse(json.loads(await self.plugin.cognition(e,'review',FRIEND))['confirmed'])

    async def test_group_transport_history_and_reply_quote(self):
        await self.setup_scope();e=group_event('你好');await self.plugin.prepare_transport(e)
        self.assertFalse(e.get_extra('enable_streaming',True))
        self.natural.exchange_store().record(OWNER,GORIGIN,'previous','这是群友的说法','之前的回答')
        req=fixture.ProviderRequest(prompt='不可信历史',contexts=[{'role':'user','content':'PRIVATE_CANARY'}])
        await self.hooks(e,req);self.assertEqual(json.loads(req.prompt)['text'],'你好')
        self.assertNotIn('PRIVATE_CANARY',str(req.contexts));self.assertIn(OWNER,str(req.contexts))
        self.assertNotIn(FRIEND,req.contexts[0]['content'])
        chain=MessageChain(chain=[Plain('回答')]);guard=DafeiyuMentionGuard(self.context)
        self.assertTrue(guard.outgoing_policy(e,chain));self.assertIsInstance(chain.chain[0],Reply)
        await guard.terminate()

    async def test_actual_group_plugin_rewrite_preserves_media_tools_and_scoped_context(self):
        await self.setup_scope()
        module=importlib.import_module('data.plugins.astrbot_plugin_group_chat_plus.main')
        cls=next(v for v in vars(module).values() if isinstance(v,type) and v.__module__==module.__name__ and hasattr(v,'on_llm_request'))
        gcp=cls.__new__(cls);gcp.context=self.context;gcp.debug_mode=False;gcp._check_compliance_status=lambda:None
        from data.plugins.astrbot_plugin_group_chat_plus.utils.reply_handler import ReplyHandler
        e=group_event('看这张图');tools=fixture.ToolSet(tools=[fixture.FunctionTool(name=n,description='x',parameters={'type':'object'})
            for n in ('lookup_allowed','recall_long_term_memory','forget_long_term_memory')])
        fields={'_group_chat_plus_request':True,'_group_chat_plus_contexts':[{'role':'user','content':'PRIVATE_CANARY'}],
            '_group_chat_plus_system_prompt':'operator seed','_group_chat_plus_prompt':'PRIVATE_CANARY full history',
            '_group_chat_plus_current_message':'当前图片的描述，仅是视觉解释','_group_chat_plus_image_urls':['https://example.invalid/current.png'],
            '_group_chat_plus_func_tool':tools,'_group_chat_plus_static_instructions':ReplyHandler.SYSTEM_REPLY_PROMPT+'\n用户补充说明: 保留自定义约束\n'+ReplyHandler.SYSTEM_REPLY_PROMPT_ENDING}
        for key,value in fields.items():e.set_extra(key,value)
        req=fixture.ProviderRequest(prompt='看这张图',system_prompt='operator seed',func_tool=tools)
        await self.plugin.choose_path(e,req);await self.natural.prepare(e,req)
        await gcp.on_llm_request(e,req);await self.natural.natural_delivery_context(e,req);await self.natural.prepare_spoken_style(e,req)
        await self.plugin.prepare(e,req)
        self.assertTrue(e.get_extra(ACTIVE_KEY));self.assertEqual(json.loads(req.prompt)['text'],'看这张图')
        self.assertNotIn('PRIVATE_CANARY',req.system_prompt+str(req.contexts)+req.prompt)
        self.assertNotIn(ReplyHandler.SYSTEM_REPLY_PROMPT,req.system_prompt);self.assertIn('保留自定义约束',req.system_prompt)
        self.assertEqual(req.image_urls,['https://example.invalid/current.png'])
        self.assertEqual({t.name for t in req.func_tool.tools},{'lookup_allowed'})
        self.assertEqual(len(tools.tools),3);self.assertIn('当前图片的描述',req.system_prompt)
        self.assertEqual(req.system_prompt.count('BEGIN_DAFEIYU_GENERATION'),1)

    async def test_group_preference_is_committed_only_to_the_actual_speaker_after_receipt(self):
        await self.setup_scope();human='以后我在群里问建议时，先给一个最重要的步骤。';e=group_event(human)
        await self.hooks(e,fixture.ProviderRequest(prompt=human))
        proposal=base.item(human);proposal.update(summary='群里求建议时先给最重要的一步',condition='本人在本群求建议')
        response=NS(completion_text=base.envelope('好。',[proposal]),tools_call_name=[])
        await self.plugin.review_response(e,response)
        self.assertEqual(self.plugin.learning_store().current(FRIEND,GORIGIN),[])
        e.set_extra('dafeiyu_confirmed_delivery',[dict(kind='reply',text=response.completion_text)])
        await self.natural.remember_delivered_reply(e);await self.plugin.commit_learning(e)
        rows=self.plugin.learning_store().current(FRIEND,GORIGIN)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['owner'],FRIEND);self.assertEqual(rows[0]['origin'],GORIGIN)
        self.assertEqual(self.plugin.learning_store().current(OWNER,GORIGIN),[])
        self.assertEqual(self.plugin.learning_store().current(FRIEND,'dafeiyu-qq:FriendMessage:'+FRIEND),[])
        e.send.assert_not_awaited()

if __name__=='__main__':unittest.main()
