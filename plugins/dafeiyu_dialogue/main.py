"""Source-bound dialogue for admitted contacts and groups; shadow requests remain read only."""
import asyncio
import contextlib
from collections import deque, Counter
import copy
from dataclasses import replace
import hashlib
from pathlib import Path
import sys
import time
import json
import datetime as dt
import sqlite3

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import json_response, request as web_request
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from dafeiyu.dialogue.catalog import MemoryCatalog
from dafeiyu.dialogue.context_builder import compose
from dafeiyu.dialogue.contracts import ACTIVE_KEY, TurnContext
from dafeiyu.dialogue.retrieval import lexical_rank, recall_candidates, semantic_select
from dafeiyu.dialogue.persona import replace_persona,attach_tool_contracts
from dafeiyu.dialogue.review import review, verify_revision, packet_key, ReviewResult, UNAVAILABLE, SCHEMA_RETRY
from dafeiyu.dialogue.self_state import self_state
from dafeiyu.dialogue.generation import parse_generation, claim_sources, validate_claim_sources, request_contract
from dafeiyu.dialogue.learning import LearningStore, proposals
from dafeiyu.dialogue.indexes import SourceIndex
from .provider_contract import ProviderContracts,arm_contract
from .metering import MeteredProvider
from dafeiyu.dialogue.growth import GrowthStore,GLOBAL,own_experiences,preference_experiences,relation,perspective
from dafeiyu.dialogue.growth_policy import assess as assess_growth
from dafeiyu.dialogue.self_context import default_character
from .growth_lifecycle import GrowthLifecycle
from .evidence_context import EvidenceContext
from .generation_workflow import GenerationWorkflow


@register('dafeiyu_dialogue', 'Local', '统一对话、来源学习与单次主生成', '0.6.0')
class Dialogue(GrowthLifecycle, EvidenceContext, GenerationWorkflow, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.root = Path(get_astrbot_data_path())
        self.samples = deque(maxlen=200)
        self.errors = 0
        self.review_stats = Counter()
        self._learning = None
        self._index = None
        self.provider_contracts=ProviderContracts()
        self._growth=None;self.growth_task=None;self.growth_lock=asyncio.Lock()
        self.learning_ticks=Counter();self.learning_seen=deque(maxlen=512);self.learning_state_inflight=set()
        context.register_web_api('/dafeiyu_dialogue/status', self.status, ['GET'], '统一对话资料数值状态（无正文）')
        context.register_web_api('/dafeiyu_dialogue/personality',self.personality_status,['GET'],'长期性格成长版本与运行状态')
        context.register_web_api('/dafeiyu_dialogue/personality/rollback',self.rollback_personality,['POST'],'管理面板按版本回退全局成长倾向')

    def natural(self):
        meta = self.context.get_registered_star('dafeiyu_natural_chat')
        return getattr(meta, 'star_cls', None) if meta and meta.activated else None

    async def initialize(self):
        self.provider_contracts.install_runner()
        if self.growth_enabled():self.growth_task=asyncio.create_task(self.growth_loop())

    async def terminate(self):
        if self.growth_task:
            self.growth_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.growth_task
        self.provider_contracts.close()







    async def personality_status(self):
        enabled=self.growth_enabled()
        current=await self.sync_growth() if enabled else dict(revision=0,version=0,tendencies=[])
        return json_response(dict(enabled=enabled,global_personality=current,
            history=await asyncio.to_thread(self.growth_store().history,GLOBAL) if enabled else [],
            statistics=await asyncio.to_thread(self.growth_store().status) if enabled else {},
            background_running=bool(self.growth_task and not self.growth_task.done()),
            max_reviews_per_day=2,max_model_calls_per_review=2,min_distinct_experiences=3,min_span_hours=24,
            private_feedback_scope='same_person_same_private_session',identity_mutable=False,contains_chat_content=False))

    async def rollback_personality(self):
        if not web_request.username or not self.growth_enabled():raise ValueError('Authenticated operator required')
        body=await web_request.json(default={})
        if (not isinstance(body,dict) or set(body)!={'expected_revision','target_version'}
                or any(type(body[k]) is not int or body[k]<0 for k in body)):raise ValueError('Explicit personality revisions required')
        await self.sync_growth()
        version=await asyncio.to_thread(self.growth_store().rollback,GLOBAL,body['expected_revision'],body['target_version'])
        return json_response(dict(rolled_back=True,version=version))

    def metered_provider(self,provider,purpose,structured=True):
        self.provider_contracts.install(provider)
        return MeteredProvider(provider,self.review_stats,purpose,structured)

    def feature(self, name):
        return bool(self.config.get(name, False))

    def owner_policy_enabled(self, name):
        natural = self.natural()
        owner = natural.actor_owner() if natural and hasattr(natural, 'actor_owner') else None
        return bool(owner and self.config.get('mode') == 'active' and self.feature(name)
            and owner in {str(x) for x in self.config.get('active_private_users', [])})

    def learning_store(self):
        if self._learning is None:
            self._learning = LearningStore(self.root / 'plugin_data/dafeiyu_dialogue/learning.db')
        return self._learning

    def source_index(self):
        if self._index is None:
            self._index = SourceIndex(self.root / 'plugin_data/dafeiyu_dialogue/source_index.db')
        return self._index

    def identity(self, event):
        natural = self.natural()
        if (natural is None or event.get_extra('cron_job', None) or event.get_extra('cron_payload', None)):
            raise ValueError('Not a current human turn')
        actor = natural.actor(event)
        raw=getattr(event.message_obj,'raw_message',{})
        if actor==str(event.get_self_id()) or raw.get('anonymous') or (raw.get('sender') or {}).get('is_bot'):
            raise ValueError('Not an attributable human')
        group=str(event.get_group_id() or '')
        if group:
            from data.plugins.dafeiyu_mention_guard.main import DafeiyuMentionGuard
            if (event.is_private_chat() or str(raw.get('group_id'))!=group
                    or event.unified_msg_origin!=event.get_platform_id()+':GroupMessage:'+group
                    or not DafeiyuMentionGuard.explicitly_mentioned(event)):
                raise ValueError('Group input was not admitted')
        elif not event.is_private_chat():raise ValueError('Invalid private input')
        return TurnContext(actor, event.unified_msg_origin, event.get_platform_id(),
            str(getattr(event.message_obj, 'message_id', '')), time.time())

    def selected(self,turn):
        if self.config.get('mode','shadow')!='active' or getattr(self.natural(),'dialogue_context_protocol',0)!=4:return False
        if turn.is_group:
            return bool(self.config.get('all_allowed_groups',False) or turn.origin.rsplit(':',1)[1] in
                {str(x) for x in self.config.get('active_group_ids',[])})
        return bool(self.config.get('all_allowed_private',False) or turn.actor in
            {str(x) for x in self.config.get('active_private_users',[])})

    def catalog(self, automatic=False):
        meta = self.context.get_registered_star('长期记忆系统')
        memory = getattr(meta, 'star_cls', None) if meta and meta.activated else None
        path = getattr(getattr(memory, 'db', None), '_db_path', None)
        config = getattr(memory, 'config', {}) or {}
        enabled = memory is not None and config.get('enable', True)
        if automatic and not config.get('auto_recall', True):
            enabled = False
        return MemoryCatalog(self.root, path, include_long_term=enabled)

    @staticmethod
    def hide_recall(req):
        if getattr(req, 'func_tool', None) is not None:
            tools = copy.copy(req.func_tool)
            tools.remove_tool('dafeiyu_recall')
            tools.remove_tool('dafeiyu_cognition')
            tools.remove_tool('dafeiyu_self')
            req.func_tool = tools

    @staticmethod
    def hide_automatic_legacy_writes(req,group=False):
        if getattr(req,'func_tool',None) is not None:
            tools=copy.copy(req.func_tool)
            names=('memorize_long_term_memory','jingjing_episodes')
            if group:names+=('recall_long_term_memory','forget_long_term_memory','dafeiyu_memory_review')
            for name in names:tools.remove_tool(name)
            req.func_tool=tools

    @filter.on_llm_request(priority=sys.maxsize + 100)
    async def choose_path(self, event, req):
        # Fail closed and avoid changing tools shared with the framework registry.
        req._dafeiyu_generation_binding=None
        event.set_extra(ACTIVE_KEY, False)
        try:
            turn = self.identity(event)
            selected = self.selected(turn)
        except ValueError:
            selected = False
        event.set_extra(ACTIVE_KEY, selected)
        event.set_extra('dafeiyu_group_contract',bool(selected and turn.is_group))
        event.set_extra('dafeiyu_generation_contract', selected and self.config.get('generation_contract', True))
        event.set_extra('dafeiyu_generation_capability',arm_contract(event.get_extra('dafeiyu_generation_contract',False)))
        if selected:
            if turn.is_group:
                event.set_extra('dafeiyu_group_current_rendering',event.get_extra('_group_chat_plus_current_message','') or '')
            personas = getattr(getattr(self.context, 'persona_manager', None), 'personas', [])
            matching = {p.system_prompt for p in personas if isinstance(getattr(p, 'system_prompt', None), str)
                        and p.system_prompt and req.system_prompt.count(p.system_prompt) == 1}
            event.set_extra('dafeiyu_dialogue_persona', next(iter(matching)) if len(matching) == 1 else '')
        if not selected:
            self.hide_recall(req)
        elif event.get_extra('dafeiyu_generation_contract',False) and (self.feature('learning_v2') or turn.is_group):
            self.hide_automatic_legacy_writes(req,turn.is_group)

    @filter.event_message_type(filter.EventMessageType.ALL,priority=sys.maxsize+99)
    async def prepare_transport(self,event):
        try:turn=self.identity(event)
        except ValueError:return
        if self.selected(turn) and self.config.get('generation_contract',True):
            event.set_extra('enable_streaming',False)
            event.set_extra('enable_reasoning',False)

    @filter.on_llm_request(priority=-sys.maxsize - 90)
    async def prepare(self, event, req):
        # The group plugin may rebuild tools after choose_path; enforce again at the final hook.
        if not event.get_extra(ACTIVE_KEY, False):
            self.hide_recall(req)
        elif event.get_extra('dafeiyu_generation_contract',False) and (self.feature('learning_v2') or event.get_extra('dafeiyu_group_contract',False)):
            self.hide_automatic_legacy_writes(req,event.get_extra('dafeiyu_group_contract',False))
        if self.config.get('mode', 'shadow') == 'off':
            return
        try:
            turn = self.identity(event)
            natural = self.natural()
            source = natural.input_snapshot(event)
            if event.get_extra('dafeiyu_generation_contract',False):event.set_extra('dafeiyu_native_request',req)
            if event.get_extra('dafeiyu_generation_contract',False) and self.feature('learning_v2'):
                req.contexts,epoch=await asyncio.to_thread(self.learning_store().filter_context,turn.origin,req.contexts)
                event.set_extra('dafeiyu_context_epoch',epoch)
                event.set_extra('dafeiyu_native_request',req)
            snapshot = await asyncio.to_thread(self.catalog(automatic=True).read, turn, time.time())
            if turn.is_group and event.get_extra('dafeiyu_generation_contract',False):
                from .group_context import history
                req.prompt=json.dumps({'speaker_qq':turn.actor,'text':source['text']},ensure_ascii=False)
                if not event.get_extra('dafeiyu_group_context_prepared',False):
                    req.contexts=history(snapshot.items)
                    event.set_extra('dafeiyu_group_context_prepared',True)
            ranked = None
            if event.get_extra(ACTIVE_KEY, False) and self.feature('retrieval_index_v2'):
                index = self.source_index()
                await asyncio.to_thread(index.sync, turn, snapshot.items)
                ranked = await asyncio.to_thread(index.rank, turn, source['text'], snapshot.items)
            # Only a verified artifact may supply the bot's own recent activity.
            record, progress = await self.own_activity()
            if event.get_extra('dafeiyu_generation_contract',False):attach_tool_contracts(req,event.get_extra('dafeiyu_dialogue_persona',''))
            prompt, compacted = replace_persona(req.system_prompt, event.get_extra('dafeiyu_dialogue_persona', ''),
                contract=event.get_extra('dafeiyu_generation_contract',False))
            character=natural.dialogue_character() if hasattr(natural, 'dialogue_character') else None
            if self.growth_enabled():
                character=dict(character or {},growth=await self.growth_profile(turn))
            on_demand=bool(event.get_extra('dafeiyu_generation_contract',False))
            if on_demand:character=default_character(character)
            built = compose(prompt, turn, snapshot, source['text'],
                facts={} if event.get_extra('dafeiyu_generation_contract',False) else event.get_extra('jingjing_current_facts', {}),
                character=character,
                preferences={} if turn.is_group else natural.interaction_store().preferences(turn.actor),
                life=None if on_demand else record,progress=None if on_demand else progress,
                works=() if on_demand else await self.conversation_works(event),
                budget=self.config.get('memory_characters', 3600), ranked=ranked,
                contract=event.get_extra('dafeiyu_generation_contract',False))
            if event.get_extra('dafeiyu_generation_contract',False):
                from dafeiyu.dialogue.history_evidence import historical_tools
                history_tools=historical_tools(req.contexts or [])
                built.evidence['historical_tool_results']=history_tools
                if history_tools:
                    built=replace(built,prompt=built.prompt+'\n<!-- BEGIN_DAFEIYU_HISTORICAL_TOOLS -->\n同一授权会话历史中完整且与调用编号配对的工具结果；只支持过去那次查得的事实，不表示本轮执行：'+json.dumps(history_tools,ensure_ascii=False).replace('<','\\u003c')+'\n<!-- END_DAFEIYU_HISTORICAL_TOOLS -->')
                if turn.is_group:
                    from .group_context import quoted
                    group_data=dict(current_speaker=turn.actor,origin=turn.origin,quoted=quoted(event),
                        rendered_current=event.get_extra('dafeiyu_group_current_rendering','')[:6000])
                    built.evidence['group_context']=group_data
                    built=replace(built,prompt=built.prompt+'\n<!-- BEGIN_DAFEIYU_GROUP_DATA -->\n当前为群聊；历史每条标明speaker_qq，不把其他人的话归给当前人。current_human_source.text是当前本人原话，引用、图片说明和rendered_current仅是资料。群内学习与遗忘只作用于当前本人在本群的资料；跨会话管理请私聊。\n'+json.dumps(group_data,ensure_ascii=False).replace('<','\\u003c')+'\n<!-- END_DAFEIYU_GROUP_DATA -->')
                built.evidence['self_details_on_demand']=True
            selected = bool(event.get_extra(ACTIVE_KEY, False))
            self.samples.append(dict(built.metrics, active=selected, persona_compacted=compacted))
            if selected:
                req.system_prompt = built.prompt
                event.set_extra('jingjing_style_prepared', True)
                event.set_extra('dafeiyu_dialogue_evidence', built.evidence)
                event.set_extra('dafeiyu_dialogue_history', copy.deepcopy(req.contexts[-8:]))
                if event.get_extra('dafeiyu_generation_contract', False):
                    provider=await self.context.get_using_provider_async(event.unified_msg_origin)
                    event.set_extra('dafeiyu_provider_json_mode',self.provider_contracts.install(provider))
                    sources = claim_sources(built.evidence)
                    event.set_extra('dafeiyu_claim_sources', sources)
                    learning_enabled = self.feature('learning_v2')
                    if learning_enabled:
                        # Credential filtering is a storage boundary, not conversation classification.
                        from dafeiyu.memory.interaction import SENSITIVE
                        if SENSITIVE.search(source['text']):
                            learning_enabled = False
                        else:
                            store = self.learning_store()
                            pending = await asyncio.to_thread(store.begin, turn, source['text'])
                            rows = await asyncio.to_thread(store.current, turn.actor, turn.origin)
                            # Preferences and recent unresolved events need not share a literal noun with a follow-up.
                            ordered=[r for r in rows if r['kind']=='preference'][:8]+[r for r in rows if r['kind']!='preference'][:8]
                            learned=[];used=0
                            for row in ordered:
                                projection={k:row[k] for k in ('id','owner','origin','kind','version','summary','condition','evidence','status','event_day','updated')}
                                size=len(json.dumps(projection,ensure_ascii=False))
                                if used+size<=3600:learned.append(projection);used+=size
                            event.set_extra('dafeiyu_learning_source', pending)
                            event.set_extra('dafeiyu_learning_versions', {r['id']: r['version'] for r in learned})
                            built.evidence['learned_context'] = learned
                            req.system_prompt += '\n<!-- BEGIN_DAFEIYU_LEARNING_DATA -->\n本人已有语义资料（保留版本、条件与来源）：'+json.dumps(learned,ensure_ascii=False).replace('<','\\u003c')+'\n<!-- END_DAFEIYU_LEARNING_DATA -->'
                    sources=claim_sources(built.evidence)
                    event.set_extra('dafeiyu_claim_sources',sources)
                    req.system_prompt = request_contract(req.system_prompt, sources, learning_enabled,
                        nonce=event.get_extra('dafeiyu_generation_capability',''))
                    self.provider_contracts.bind_request(req,event.get_extra('dafeiyu_generation_capability'))
                # Final fact/receipt guards and confirmed-delivery recording remain active.
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors += 1
            # Do not silently send an unguarded v2 response if context building fails.
            event.set_extra(ACTIVE_KEY, False)
            event.set_extra('dafeiyu_generation_contract', False)
            arm_contract(False)
            logger.warning('Dialogue context unavailable: %s', type(error).__name__)
            if self.natural() and event.get_extra('dafeiyu_dialogue_legacy_skipped', False):
                await self.natural().prepare(event, req)
                await self.natural().natural_delivery_context(event, req)
                await self.natural().prepare_spoken_style(event, req)
            self.hide_recall(req)






    @filter.on_llm_response(priority=-sys.maxsize + 1)
    async def review_response(self, event, response):
        if (not event.get_extra(ACTIVE_KEY, False) or getattr(response, 'tools_call_name', [])):
            return
        if event.get_extra('dafeiyu_generation_contract', False):
            await self.finalize_generation(event, response)
            return
        if not response.completion_text.strip():return
        try:
            result = await self.reviewed(event, [response.completion_text], event.unified_msg_origin)
            response.completion_text = result.segments[0]
        except asyncio.CancelledError:
            response.completion_text = UNAVAILABLE
            raise
        except Exception as error:
            self.review_stats['failed'] += 1
            event.set_extra('dafeiyu_dialogue_last_review', 'unavailable')
            logger.warning('Dialogue review unavailable: %s', type(error).__name__)
            if self.config.get('response_review', 'enforce') != 'audit':
                response.completion_text = UNAVAILABLE



    @filter.on_agent_done(priority=-sys.maxsize + 1)
    async def save_native_context(self,event,run_context,response):
        if not event.get_extra('dafeiyu_generation_contract',False):return
        event.set_extra('dafeiyu_native_context',run_context)
        event.set_extra('dafeiyu_native_expected_text',str(getattr(response,'completion_text','') or ''))
        if event.get_extra('dafeiyu_generation_delivery_only',False) and run_context.messages:
            last=run_context.messages[-1]
            if getattr(last,'role',None)=='assistant' and not getattr(last,'tool_calls',None):last._no_save=True
        if not self.feature('learning_v2'):return
        epoch=await asyncio.to_thread(self.learning_store().context_epoch)
        action=event.get_extra('dafeiyu_privacy_action','none')
        if action=='deny_turn':
            last=max((i for i,m in enumerate(run_context.messages) if getattr(m,'role',None)=='user'),default=len(run_context.messages))
            del run_context.messages[last:]
        elif (action in ('forget_all','forget_selected') or event.get_extra('dafeiyu_context_epoch',epoch)!=epoch):
            run_context.messages.clear()
            req=event.get_extra('dafeiyu_native_request',None)
            if req is not None:req.contexts=[]
        await asyncio.to_thread(self.learning_store().acknowledge_context,event.unified_msg_origin,epoch)

    @filter.after_message_sent(priority=-sys.maxsize)
    async def sync_confirmed_history(self,event):
        if not event.get_extra('dafeiyu_generation_valid',False) or event.get_extra('dafeiyu_semantic_forgetting',False):return
        from dafeiyu.memory.content import content_text
        receipts=event.get_extra('dafeiyu_confirmed_delivery',[])
        actual='\n'.join(dict.fromkeys(r['text'] for r in receipts if r.get('kind')=='reply' and r.get('text')))
        expected=event.get_extra('dafeiyu_native_expected_text','')
        native=event.get_extra('dafeiyu_native_context',None)
        if not actual or not expected or actual==expected or not native or not native.messages:return
        last=native.messages[-1]
        if getattr(last,'role',None)!='assistant' or getattr(last,'tool_calls',None):return
        from astrbot.core.agent.message import TextPart
        last.content=[TextPart(text=actual)]
        # Depending on native pipeline yielding, persistence may precede or follow delivery.
        req=event.get_extra('dafeiyu_native_request',None)
        cid=getattr(getattr(req,'conversation',None),'cid',None)
        manager=getattr(self.context,'conversation_manager',None)
        if not cid or manager is None:return
        try:
            if await manager.get_curr_conversation_id(event.unified_msg_origin)!=cid:return
            conversation=await manager.get_conversation(event.unified_msg_origin,cid)
            history=json.loads(conversation.history or '[]')
            if (history and history[-1].get('role')=='assistant' and not history[-1].get('tool_calls')
                    and content_text(history[-1].get('content'))==expected):
                history[-1]['content']=actual
                await manager.update_conversation(event.unified_msg_origin,cid,history=history)
                self.review_stats['confirmed_history_sync']+=1
        except Exception as error:
            self.review_stats['history_sync_failed']+=1
            logger.warning('Confirmed native history sync unavailable: %s',type(error).__name__)

    @filter.after_message_sent(priority=-sys.maxsize + 1)
    async def commit_learning(self, event):
        if (not self.feature('learning_v2') or not event.get_extra(ACTIVE_KEY,False)
                or event.get_extra('dafeiyu_generation_final_text',None) is None):
            return
        source = event.get_extra('dafeiyu_learning_source', None)
        items = event.get_extra('dafeiyu_learning_pending', [])
        receipts = event.get_extra('dafeiyu_confirmed_delivery', [])
        if not source or not any(r.get('kind') == 'reply' and r.get('text') for r in receipts):
            return
        try:
            if event.get_extra('dafeiyu_semantic_forgetting',False):return
            if event.get_extra('dafeiyu_context_epoch',None)!=await asyncio.to_thread(self.learning_store().context_epoch):return
            versions=dict(event.get_extra('dafeiyu_learning_versions',{}))
            if source['id'] not in self.learning_seen:
                self.learning_seen.append(source['id']);scope=(source['owner'],source['origin']);self.learning_ticks[scope]+=1
                cadence=max(3,min(10,int(self.config.get('event_state_batch',3))))
                if self.learning_ticks[scope]%cadence==0 or event.get_extra('dafeiyu_learning_state_urgent',False):
                    current=await asyncio.to_thread(self.learning_store().current,source['owner'],source['origin'])
                    events=[r for r in current if r['kind']=='event' and r['status'] in ('planned','ongoing')
                        and not any(i['supersedes']==r['id'] and i['expected_version']==r['version'] for i in items)][:8]
                    if events:
                        from dafeiyu.dialogue.event_state import updates
                        self.learning_state_inflight.add(source['owner'])
                        try:
                            from dafeiyu.memory.content import content_text
                            history=[content_text(m.get('content')) for m in event.get_extra('dafeiyu_dialogue_history',[])
                                if m.get('role')=='user'][-2:]
                            extra=await updates(events,self.natural().input_snapshot(event)['text'],history,
                                lambda prompt,policy:self.provider_call(event,prompt,policy,'learning_state'))
                            versions.update({r['id']:r['version'] for r in events})
                            items=[*items,*extra][:5]
                        except Exception as error:
                            self.review_stats['learning_state_failed']+=1
                            logger.warning('Event state batch unavailable: %s',type(error).__name__)
                        finally:self.learning_state_inflight.discard(source['owner'])
            if event.get_extra('dafeiyu_context_epoch',None)!=await asyncio.to_thread(self.learning_store().context_epoch):return
            saved = await asyncio.to_thread(self.learning_store().commit, source,
                self.natural().input_snapshot(event)['text'], items,
                versions, confirmed=True)
            self.review_stats['learned_entities'] += len(saved)
        except Exception as error:
            self.review_stats['learning_failed'] += 1
            logger.warning('Dialogue learning unavailable: %s', type(error).__name__)

    @filter.on_using_llm_tool(priority=-sys.maxsize + 1)
    async def review_tool_message(self, event, tool, arguments):
        if not event.get_extra(ACTIVE_KEY, False) or getattr(tool, 'name', '') != 'send_message_to_user':
            return
        messages = arguments.get('messages', [])
        if not isinstance(messages, list):
            return
        positions = [i for i, part in enumerate(messages) if isinstance(part, dict)
            and part.get('type') == 'plain' and isinstance(part.get('text'), str) and part['text'].strip()]
        if not positions:
            return
        try:
            result = await self.reviewed(event, [messages[i]['text'] for i in positions],
                arguments.get('session') or event.unified_msg_origin)
            for i, text in zip(positions, result.segments):
                messages[i]['text'] = text
        except asyncio.CancelledError:
            # Raising alone cannot stop the framework's tool executor.
            arguments['messages'] = []
            raise
        except Exception as error:
            self.review_stats['failed'] += 1
            logger.warning('Dialogue tool review unavailable: %s', type(error).__name__)
            if self.config.get('response_review', 'enforce') != 'audit':
                arguments['messages'] = []

    @filter.on_llm_tool_respond(priority=-sys.maxsize + 1)
    async def collect_tool_evidence(self, event, tool, arguments, result):
        if not event.get_extra(ACTIVE_KEY, False) or getattr(result,'isError',False):
            return
        text = result if isinstance(result, str) else None
        if text is None:
            parts=getattr(result,'content',None)
            if isinstance(parts,list):
                texts=[getattr(part,'text',None) for part in parts if getattr(part,'type',None)=='text']
                if len(texts)==len(parts) and all(isinstance(p,str) for p in texts):text='\n'.join(texts)
            elif isinstance(result,dict):text=json.dumps(result,ensure_ascii=False)
        rows = event.get_extra('dafeiyu_dialogue_tool_evidence', [])
        if len(rows) < 8:
            rows.append({'tool': getattr(tool, 'name', ''), 'result': text if text and len(text) <= 5000 else None,
                         'complete': bool(text and len(text) <= 5000)})
            event.set_extra('dafeiyu_dialogue_tool_evidence', rows)
        if getattr(tool, 'name', '') == 'send_message_to_user':
            await self.commit_learning(event)

    @filter.llm_tool(name='dafeiyu_recall')
    async def recall(self, event, query: str) -> str:
        """按语义查有归属和来源的往事。只读，不发消息、不保存或创建任务。

        Args:
            query(string): 想核对的人物、事情、时间或旧话的自然描述；查不到时不编原话
        """
        try:
            turn = self.identity(event)
            if not event.get_extra(ACTIVE_KEY, False) or not 1 <= len(query.strip()) <= 600:
                raise ValueError('Semantic recall is not enabled for this turn')
            snapshot = await asyncio.to_thread(self.catalog().read, turn, time.time())
            if self.feature('retrieval_index_v2'):
                await asyncio.to_thread(self.source_index().sync, turn, snapshot.items)
            # Do not cache source bodies; a forgotten record must not reappear in another turn.
            lexical = lexical_rank(snapshot.items, query, limit=5)
            candidates = recall_candidates(snapshot.items, query, turn.actor)
            async def call(prompt, policy):
                return await self.provider_call(event, prompt, policy, 'recall')

            try:
                rows = await semantic_select(query, candidates, call, actor=turn.actor)
                method = 'semantic_selection'
            except (ValueError, TypeError, TimeoutError):
                rows = lexical[:5]
                method = 'literal_fallback'
            # Re-read ACL and current records after an awaited call, including concurrent forgetting.
            current = await asyncio.to_thread(self.catalog().read, turn, time.time())
            offered = {item.id: item for item in current.items}
            rows = [item for item in rows if offered.get(item.id) == item]
            materials=event.get_extra('dafeiyu_dialogue_evidence',None)
            if isinstance(materials,dict):
                materials['memory']=list({r['id']:r for r in [*materials.get('memory',[]),*[r.data() for r in rows]]}.values())
                event.set_extra('dafeiyu_claim_sources',claim_sources(materials,event.get_extra('dafeiyu_dialogue_tool_evidence',[])))
            return json.dumps({'records': [item.data() for item in rows], 'method': method,
                'unavailable_sources': current.unavailable, 'complete_history': False}, ensure_ascii=False)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors += 1
            return json.dumps({'records': [], 'unavailable': True, 'error_type': type(error).__name__})

    @filter.llm_tool(name='dafeiyu_self')
    async def self_details(self,event,section:str='thoughts')->str:
        """按当前讨论需要，只读查询鲸鲸自己的资料；不执行活动或发送消息。

        普通问候、应声和别人的话题无需查询。明确问自己的兴趣、近况、作品或故事时，
        自行选择相关部分；可表达当下看法，不为凑话题读取旧进度。打算不证明正在做或做完。

        Args:
            section(string): thoughts当前打算与反思；works实际作品与进行中记录；interests偏好；fiction已有虚构世界
        """
        try:
            turn=self.identity(event)
            if not event.get_extra(ACTIVE_KEY,False) or section not in ('thoughts','works','interests','fiction'):
                raise ValueError('Self lookup not authorized')
            evidence=event.get_extra('dafeiyu_dialogue_evidence',{})
            if section=='thoughts':
                thoughts=await self.own_thoughts()
                data={key:list(thoughts.get(key,[]))[:1] for key in ('projects','reflections','intentions')}
                evidence['self_thoughts']=data
            elif section=='works':
                completed,running=await self.own_activity()
                data=self_state(completed,running,(await self.conversation_works(event))[:1])
                evidence['verified_self']=data
            else:
                character=self.natural().dialogue_character()
                if section=='interests':
                    data=dict(stable_profile=character.get('stable_profile'),growth=await self.growth_profile())
                else:
                    completed,_=await self.own_activity()
                    data=dict(fictional_world=character.get('fictional_world'),
                        previous_story=completed.get('story') if completed else None,nature='fiction_not_real_activity')
                evidence['requested_character']=data
            evidence['self_details_requested']=True
            event.set_extra('dafeiyu_dialogue_evidence',evidence)
            offered=claim_sources(evidence,event.get_extra('dafeiyu_dialogue_tool_evidence',[]))
            event.set_extra('dafeiyu_claim_sources',offered)
            return json.dumps(dict(section=section,data=data,read_only=True,executes_activities=False,
                claim_evidence={key:row['kind'] for key,row in offered.items()}),ensure_ascii=False)
        except asyncio.CancelledError:raise
        except Exception as error:return json.dumps(dict(unavailable=True,error_type=type(error).__name__))

    @filter.llm_tool(name='dafeiyu_cognition')
    async def cognition(self,event,action:str='review',owner_qq:str='',record_ids:list[str]|None=None)->str:
        """查阅或按已提供的编号删除语义学习资料。仅在本人明确要求管理时使用。

        Args:
            action(string): review查阅；forget删除。删除前先review取得当前编号和版本
            owner_qq(string): 留空为当前发言者；只有爸爸可管理别人的资料
            record_ids(array): forget时提供本轮review返回的记录编号，不支持猜测编号或通配符
        """
        try:
            turn=self.identity(event);owner=owner_qq or turn.actor
            if (not event.get_extra(ACTIVE_KEY,False) or not self.feature('learning_v2')
                    or owner!=turn.actor and turn.actor!=str(self.natural().actor_owner())):
                raise ValueError('Learning management is not authorized')
            if turn.is_group and owner!=turn.actor:raise ValueError('Group management is limited to the current speaker')
            origin=turn.origin if turn.is_group else turn.platform+':FriendMessage:'+owner
            if action=='review':
                rows=await asyncio.to_thread(self.learning_store().current,owner,origin)
                event.set_extra('dafeiyu_cognition_offered',dict(owner=owner,versions={r['id']:r['version'] for r in rows}))
                return json.dumps({'owner':owner,'records':[{k:r.get(k) for k in ('id','kind','summary','condition','status','event_day','version')} for r in rows]},ensure_ascii=False)
            offered=event.get_extra('dafeiyu_cognition_offered',{})
            if (action!='forget' or offered.get('owner')!=owner or not isinstance(record_ids,list)
                    or any(not isinstance(i,str) or i not in offered.get('versions',{}) for i in record_ids)):
                raise ValueError('Review current records before deleting')
            removed=await asyncio.to_thread(self.learning_store().forget_entities,owner,
                {i:offered['versions'][i] for i in record_ids})
            if self._growth is not None and not turn.is_group:await asyncio.to_thread(self._growth.forget,owner)
            await asyncio.to_thread(self.natural().forget_semantic,owner,origin,[])
            event.set_extra('dafeiyu_memory_mutated_in_turn',True)
            event.set_extra('dafeiyu_semantic_forgetting',True)
            event.set_extra('dafeiyu_privacy_action','forget_selected')
            event.set_extra('dafeiyu_context_epoch',self.learning_store().context_epoch())
            if self._index is not None:await asyncio.to_thread(self._index.invalidate,record_ids)
            return json.dumps({'owner':owner,'deleted':removed,'confirmed':True})
        except asyncio.CancelledError:raise
        except Exception as error:return json.dumps({'confirmed':False,'error_type':type(error).__name__})

    @filter.llm_tool(name='dafeiyu_personality')
    async def personality(self,event,action:str='status')->str:
        """本人问长期性格或变化原因时，只读查阅真实成长；不改变性格、身份或权限。

        Args:
            action(string): status当前倾向；history最近变化及原因。私聊只读当前人自己的关系倾向，群聊只读全局。
        """
        try:
            turn=self.identity(event)
            if not event.get_extra(ACTIVE_KEY,False) or action not in ('status','history'):raise ValueError('Personality read not authorized')
            result=dict(enabled=self.growth_enabled(),current=await self.growth_profile(turn),identity_changed=False)
            if self.growth_enabled() and action=='history':
                scopes=[GLOBAL]+([] if turn.is_group else [relation(turn.actor,turn.origin)])
                result['history']={}
                for scope in scopes:
                    rows=await asyncio.to_thread(self.growth_store().history,scope)
                    result['history']['global' if scope==GLOBAL else 'with_current_person']=[
                        dict(version=r['version'],at=r['at'],action=r['action'],reason=r['reason'],
                            tendencies=[{k:t[k] for k in ('tendency','when','counterweight')} for t in r['tendencies']]) for r in rows[:3]]
            return json.dumps(result,ensure_ascii=False)
        except asyncio.CancelledError:raise
        except Exception as error:return json.dumps(dict(enabled=False,error_type=type(error).__name__))

    async def status(self):
        return json_response({'version': '0.6.0', 'mode': self.config.get('mode', 'shadow'),
            'self_details_on_demand':True,
            'personality_growth_enabled':self.growth_enabled(),
            'personality_growth_loop_running':bool(self.growth_task and not self.growth_task.done()),
            'native_context_adapter_installed':self.provider_contracts.runner_adapter is not None,
            'legacy_adapter_ready': getattr(self.natural(), 'dialogue_context_protocol', 0) == 4,
            'active_private_user_count': len(self.config.get('active_private_users', [])),
            'all_allowed_private':bool(self.config.get('all_allowed_private',False)),
            'all_allowed_groups':bool(self.config.get('all_allowed_groups',False)),
            'active_group_count':len(self.config.get('active_group_ids',[])),
            'retained_samples': len(self.samples), 'samples': list(self.samples)[-20:], 'errors': self.errors,
            'response_review': self.config.get('response_review', 'enforce'), 'review_statistics': dict(self.review_stats),
            'contains_chat_content': False,
            'background_model_calls':sum(self.review_stats.get(k+'_calls',0) for k in ('activity','initiative','direction','learning_state','personality_growth')),
            'extra_qq_sends': 0,
            'source_databases_read_only': self.config.get('mode', 'shadow') != 'active',
            'learning_created': self._learning is not None,
            'index_created': self._index is not None,
            'feature_flags': {k: self.feature(k) for k in ('learning_v2', 'activity_policy_v2', 'initiative_policy_v2', 'voice_intent_v2')},
            'legacy_fact_guards_retained':self.config.get('mode')!='active' or not self.config.get('generation_contract',True),
            'deterministic_action_guards_retained':True})
