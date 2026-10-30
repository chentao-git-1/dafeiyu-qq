"""Private, opt-in dialogue context; shadow mode does not mutate native requests."""
import asyncio
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
from astrbot.api.web import json_response
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .adapters import MemoryCatalog
from .context_builder import compose
from .contracts import ACTIVE_KEY, TurnContext
from .retrieval import lexical_rank, recall_candidates, semantic_select
from .persona import replace_persona,attach_tool_contracts
from .review import review, verify_revision, packet_key, ReviewResult, UNAVAILABLE
from .self_state import self_state
from .generation import parse_generation, claim_sources, validate_claim_sources, request_contract
from .learning import LearningStore, proposals
from .indexes import SourceIndex
from .provider_contract import ProviderContracts,arm_contract
from .metering import MeteredProvider


@register('dafeiyu_dialogue', 'Local', '统一对话、来源学习与单次主生成', '0.3.0')
class Dialogue(Star):
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
        self.learning_ticks=Counter();self.learning_seen=deque(maxlen=512);self.learning_state_inflight=set()
        context.register_web_api('/dafeiyu_dialogue/status', self.status, ['GET'], '统一对话资料数值状态（无正文）')

    def natural(self):
        meta = self.context.get_registered_star('dafeiyu_natural_chat')
        return getattr(meta, 'star_cls', None) if meta and meta.activated else None

    async def terminate(self):self.provider_contracts.close()

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
        if (natural is None or not event.is_private_chat() or event.get_group_id()
                or event.get_extra('cron_job', None) or event.get_extra('cron_payload', None)):
            raise ValueError('Not a current private human turn')
        actor = natural.actor(event)
        return TurnContext(actor, event.unified_msg_origin, event.get_platform_id(),
            str(getattr(event.message_obj, 'message_id', '')), time.time())

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
            req.func_tool = tools

    @staticmethod
    def hide_automatic_legacy_writes(req):
        if getattr(req,'func_tool',None) is not None:
            tools=copy.copy(req.func_tool)
            for name in ('memorize_long_term_memory','jingjing_episodes'):tools.remove_tool(name)
            req.func_tool=tools

    @filter.on_llm_request(priority=sys.maxsize + 100)
    async def choose_path(self, event, req):
        # Fail closed and avoid changing tools shared with the framework registry.
        event.set_extra(ACTIVE_KEY, False)
        try:
            turn = self.identity(event)
            selected = (self.config.get('mode', 'shadow') == 'active'
                        and getattr(self.natural(), 'dialogue_context_protocol', 0) == 3
                        and turn.actor in {str(x) for x in self.config.get('active_private_users', [])})
        except ValueError:
            selected = False
        event.set_extra(ACTIVE_KEY, selected)
        event.set_extra('dafeiyu_generation_contract', selected and self.config.get('generation_contract', True))
        event.set_extra('dafeiyu_generation_capability',arm_contract(event.get_extra('dafeiyu_generation_contract',False)))
        if selected:
            personas = getattr(getattr(self.context, 'persona_manager', None), 'personas', [])
            matching = {p.system_prompt for p in personas if isinstance(getattr(p, 'system_prompt', None), str)
                        and p.system_prompt and req.system_prompt.count(p.system_prompt) == 1}
            event.set_extra('dafeiyu_dialogue_persona', next(iter(matching)) if len(matching) == 1 else '')
        if not selected:
            self.hide_recall(req)
        elif event.get_extra('dafeiyu_generation_contract',False) and self.feature('learning_v2'):
            self.hide_automatic_legacy_writes(req)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE,priority=sys.maxsize+99)
    async def prepare_transport(self,event):
        try:turn=self.identity(event)
        except ValueError:return
        if (self.config.get('mode')=='active' and self.config.get('generation_contract',True)
                and getattr(self.natural(),'dialogue_context_protocol',0)==3
                and turn.actor in {str(x) for x in self.config.get('active_private_users',[])}):
            event.set_extra('enable_streaming',False)
            event.set_extra('enable_reasoning',False)

    @filter.on_llm_request(priority=-sys.maxsize - 90)
    async def prepare(self, event, req):
        # The group plugin may rebuild tools after choose_path; enforce again at the final hook.
        if not event.get_extra(ACTIVE_KEY, False):
            self.hide_recall(req)
        elif event.get_extra('dafeiyu_generation_contract',False) and self.feature('learning_v2'):
            self.hide_automatic_legacy_writes(req)
        if self.config.get('mode', 'shadow') == 'off':
            return
        try:
            turn = self.identity(event)
            natural = self.natural()
            source = natural.input_snapshot(event)
            if event.get_extra('dafeiyu_generation_contract',False) and self.feature('learning_v2'):
                req.contexts,epoch=await asyncio.to_thread(self.learning_store().filter_context,turn.origin,req.contexts)
                event.set_extra('dafeiyu_context_epoch',epoch)
                event.set_extra('dafeiyu_native_request',req)
            snapshot = await asyncio.to_thread(self.catalog(automatic=True).read, turn, time.time())
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
            if event.get_extra('dafeiyu_generation_contract',False) and character and character.get('fictional_world'):
                from .retrieval import terms
                recent_users=[m.get('content','') for m in (req.contexts or [])[-4:] if m.get('role')=='user' and isinstance(m.get('content'),str)]
                if not terms(json.dumps(character['fictional_world'],ensure_ascii=False)) & terms(source['text']+' '.join(recent_users)):
                    character=dict(character,fictional_world=None)
            built = compose(prompt, turn, snapshot, source['text'],
                facts={} if event.get_extra('dafeiyu_generation_contract',False) else event.get_extra('jingjing_current_facts', {}),
                character=character,
                preferences=natural.interaction_store().preferences(turn.actor), life=record, progress=progress,
                budget=self.config.get('memory_characters', 3600), ranked=ranked,
                contract=event.get_extra('dafeiyu_generation_contract',False))
            if event.get_extra('dafeiyu_generation_contract',False):
                thoughts=await self.own_thoughts()
                built.evidence['self_thoughts']=thoughts
                built=replace(built,prompt=built.prompt+'\n<!-- BEGIN_DAFEIYU_SELF_THOUGHTS -->\n自己的持续兴趣与带来源反思；projects只是意向，反思中的主观看法不等于正在执行：'+json.dumps(thoughts,ensure_ascii=False).replace('<','\\u003c')+'\n<!-- END_DAFEIYU_SELF_THOUGHTS -->')
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
                        from data.plugins.dafeiyu_natural_chat.interaction import SENSITIVE
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

    async def own_activity(self):
        meta = self.context.get_registered_star('dafeiyu_life')
        life = getattr(meta, 'star_cls', None) if meta and meta.activated else None
        if not life:
            return None, None

        def read():
            completed = life.public_context()
            store = getattr(life, 'store', None)
            path = getattr(store, 'path', None)
            today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
            try:
                rows = MemoryCatalog._read(path, 'SELECT day,kind,status,updated FROM runs WHERE day=?', (today,)) if path else []
                progress = rows[0] if rows and rows[0]['status'] == 'started' else None
                # A stale/interrupted run is not proof of something still happening.
                if progress and not 0 <= time.time() - float(progress['updated']) <= 600:
                    progress = None
            except (OSError, ValueError, TypeError, sqlite3.Error):
                progress = None
            return completed, progress

        return await asyncio.to_thread(read)

    async def own_thoughts(self):
        meta=self.context.get_registered_star('dafeiyu_life')
        life=getattr(meta,'star_cls',None) if meta and meta.activated else None
        store=getattr(life,'store',None)
        if store is None or not hasattr(store,'root'):return {}
        def read():
            root=Path(store.root);path=root/'self_journal.db'
            if not path.is_file():return {}
            try:
                projects=MemoryCatalog._read(path,"SELECT id,title,question,version,updated FROM projects WHERE status='active' ORDER BY updated DESC LIMIT 3",())
                candidates=MemoryCatalog._read(path,'SELECT work_id,work_sha,insight,next_question,recorded FROM reflections ORDER BY recorded DESC LIMIT 12',())
                records={r['id']:r for r in store.records(60)};reflections=[]
                for row in candidates:
                    work=records.get(row['work_id'])
                    if not work or work.get('sha256')!=row['work_sha']:continue
                    artifact=(root/work['artifact']).resolve()
                    if (artifact.parent!=(root/'works').resolve() or not artifact.is_file() or artifact.stat().st_size>4*1024*1024
                            or hashlib.sha256(artifact.read_bytes()).hexdigest()!=row['work_sha']
                            or not 0<float(work['completed_at'])<=time.time()+5):continue
                    reflections.append(dict(row,source_work={k:work[k] for k in ('id','day','kind','note','completed_at')}))
                    if len(reflections)==3:break
                return dict(projects=[dict(p,status='intent') for p in projects],reflections=reflections)
            except (OSError,sqlite3.Error,KeyError,ValueError,TypeError):return {}
        return await asyncio.to_thread(read)

    async def provider_call(self, event, prompt, policy, purpose):
        provider = await self.context.get_using_provider_async(event.unified_msg_origin)
        if provider is None:
            raise ValueError('Current provider unavailable')
        response=await self.metered_provider(provider,purpose).text_chat(prompt=prompt,
            system_prompt=policy,contexts=[],func_tool=None,request_max_retries=0)
        return response.completion_text

    async def reviewed(self, event, segments, audience=''):
        mode = self.config.get('response_review', 'enforce')
        if mode == 'off':
            return ReviewResult(tuple(segments), 'off')
        evidence = copy.deepcopy(event.get_extra('dafeiyu_dialogue_evidence', {}))
        turn = self.identity(event)
        # Recheck current rows after generation: a forgotten/corrected source is no longer evidence.
        current = await asyncio.to_thread(self.catalog(automatic=True).read, turn, time.time())
        existing = {row.id: row.data() for row in current.items if row.visible_to(turn)}
        evidence['memory'] = [row for row in evidence.get('memory', []) if existing.get(row.get('id')) == row]
        completed, progress = await self.own_activity()
        evidence['verified_self'] = self_state(completed, progress)
        evidence['self_thoughts']=await self.own_thoughts()
        cross_session = audience and audience != event.unified_msg_origin
        if cross_session:
            evidence['memory'] = [row for row in evidence.get('memory', []) if row.get('visibility') == 'shared']
        history = event.get_extra('dafeiyu_dialogue_history', [])
        tools = event.get_extra('dafeiyu_dialogue_tool_evidence', [])
        packet = {'segments': segments, 'evidence': evidence, 'history': history, 'tools': tools, 'audience': audience}
        key = packet_key(packet)
        cache = event.get_extra('dafeiyu_dialogue_review_cache', {})
        if key in cache:
            self.review_stats['cache_hits'] += 1
            return cache[key]
        calls = event.get_extra('dafeiyu_dialogue_review_calls', 0)
        if calls >= 2:
            raise ValueError('Per-turn review budget exceeded')
        event.set_extra('dafeiyu_dialogue_review_calls', calls + 1)
        result = await review(segments, evidence,
            lambda prompt, policy: self.provider_call(event, prompt, policy, 'review'),
            human=self.natural().input_snapshot(event)['text'], history=history, tool_results=tools, audience=audience)
        if result.decision == 'revise':
            self.review_stats['revision_checks'] += 1
            supported = await verify_revision(result.segments, evidence,
                lambda prompt, policy: self.provider_call(event, prompt, policy, 'verification'),
                human=self.natural().input_snapshot(event)['text'], history=history, tool_results=tools)
            if not supported:
                self.review_stats['revision_rejected'] += 1
                raise ValueError('Rewritten facts were not supported')
        after_completed, after_progress = await self.own_activity()
        if self_state(after_completed, after_progress) != evidence['verified_self']:
            raise ValueError('Activity evidence changed during review')
        if await self.own_thoughts()!=evidence['self_thoughts']:raise ValueError('Reflection evidence changed during review')
        after = await asyncio.to_thread(self.catalog(automatic=True).read, turn, time.time())
        after_rows = {row.id: row.data() for row in after.items if row.visible_to(turn)}
        if any(after_rows.get(row['id']) != row for row in evidence['memory']):
            raise ValueError('Memory changed during review')
        self.review_stats[result.decision] += 1
        for kind in result.issue_kinds:
            self.review_stats[kind] += 1
        if mode == 'audit':
            result = ReviewResult(tuple(segments), 'audit')
        cache[key] = result
        event.set_extra('dafeiyu_dialogue_review_cache', cache)
        event.set_extra('dafeiyu_dialogue_last_review', result.decision)
        return result

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

    async def finalize_generation(self, event, response):
        if event.get_extra('dafeiyu_generation_final_text',None)==response.completion_text:return
        event.set_extra('dafeiyu_generation_valid',False)
        event.set_extra('dafeiyu_learning_valid',False)
        stage='format'
        try:
            generated = parse_generation(response.completion_text,recover_claims=True,recover_metadata=True)
            recover=not generated.claims_valid or not generated.metadata_valid
            event.set_extra('dafeiyu_generation_framing_recovered',generated.framing_recovered)
            if generated.framing_recovered:self.review_stats['generation_framing_recovered']+=1
            stage='source'
            if (self.feature('learning_v2') and event.get_extra('dafeiyu_context_epoch',None) is not None
                    and event.get_extra('dafeiyu_context_epoch')!=self.learning_store().context_epoch()):
                raise ValueError('Conversation evidence was withdrawn')
            materials = copy.deepcopy(event.get_extra('dafeiyu_dialogue_evidence', {}))
            turn = self.identity(event)
            current = await asyncio.to_thread(self.catalog(automatic=True).read, turn, time.time())
            current_rows = {r.id: r.data() for r in current.items if r.visible_to(turn)}
            materials['memory'] = [r for r in materials.get('memory', []) if current_rows.get(r['id']) == r]
            if (len(materials['memory']) != len(event.get_extra('dafeiyu_dialogue_evidence', {}).get('memory', []))
                    and not event.get_extra('dafeiyu_memory_mutated_in_turn',False)):
                raise ValueError('A generation source was withdrawn')
            if materials.get('learned_context'):
                latest=await asyncio.to_thread(self.learning_store().current,turn.actor,turn.origin)
                by_id={r['id']:r for r in latest}
                kept=[]
                for row in materials['learned_context']:
                    actual=by_id.get(row['id'])
                    if actual is None and event.get_extra('dafeiyu_memory_mutated_in_turn',False):continue
                    if actual is None or any(actual.get(k)!=v for k,v in row.items()):
                        raise ValueError('A learned source was withdrawn or corrected')
                    kept.append(row)
                materials['learned_context']=kept
            complete, running = await self.own_activity()
            materials['verified_self'] = self_state(complete, running)
            materials['self_thoughts']=await self.own_thoughts()
            tool_rows = event.get_extra('dafeiyu_dialogue_tool_evidence', [])
            offered = dict(event.get_extra('dafeiyu_claim_sources', {}))
            # Native tools run after the request hook. Their actual results become offered evidence.
            offered.update({k: v for k, v in claim_sources({}, tool_rows).items()})
            try:validate_claim_sources(generated, offered, claim_sources(materials, tool_rows))
            except ValueError:recover=True
            human=self.natural().input_snapshot(event)['text']
            stage='learning'
            candidates=generated.learning.get('items',[])
            if not isinstance(candidates,list) or len(candidates)>5:candidates=[]
            candidates=[i for i in candidates if isinstance(i,dict)
                and (i.get('subject'),i.get('modality'),i.get('retention'))==('speaker','direct','persistent')]
            try:privacy,items=proposals(generated.learning,human)
            except ValueError:
                privacy,items=proposals({'privacy':generated.learning['privacy'],'items':[]},human)
                recover=True
                self.review_stats['learning_proposals_rejected']+=1
                event.get_extra('dafeiyu_dialogue_evidence',{})['learning_proposals_rejected']=True
            if not self.feature('learning_v2') or event.get_extra('dafeiyu_learning_source',None) is None:
                if candidates or privacy['action']!='none':
                    recover=True;event.get_extra('dafeiyu_dialogue_evidence',{})['learning_proposals_rejected']=True
                privacy=dict(action='none',evidence='',ids=[],subject='speaker',modality='direct');items=[];candidates=[]
            if candidates or privacy['action']!='none':
                from .learning_check import check
                from data.plugins.dafeiyu_natural_chat.main import content_text
                old_privacy,old_items=privacy,candidates
                history=[content_text(m.get('content')) for m in event.get_extra('dafeiyu_dialogue_history',[])
                    if m.get('role')=='user'][-2:]
                try:
                    privacy,items=await check(privacy,candidates,human,history,materials.get('learned_context',[]),
                        lambda prompt,policy:self.provider_call(event,prompt,policy,'learning_check'))
                except Exception as error:
                    privacy=dict(action='none',evidence='',ids=[],subject='speaker',modality='direct');items=[]
                    self.review_stats['learning_check_failed']+=1
                    logger.warning('Learning source check unavailable: %s',type(error).__name__)
                if privacy!=old_privacy or len(items)!=len(old_items):
                    recover=True
                    event.get_extra('dafeiyu_dialogue_evidence',{})['learning_proposals_rejected']=True
                    if privacy!=old_privacy:event.get_extra('dafeiyu_dialogue_evidence',{})['learning_control_rejected']=True
                if event.get_extra('dafeiyu_context_epoch',None)!=await asyncio.to_thread(self.learning_store().context_epoch):
                    raise ValueError('Privacy changed during source verification')
            pending = event.get_extra('dafeiyu_learning_source', None)
            if pending:
                stage='privacy'
                versions = event.get_extra('dafeiyu_learning_versions', {})
                allowed=set(versions)|{r['id'] for r in materials['memory'] if r['owner']==turn.actor}
                removed = await asyncio.to_thread(self.learning_store().apply_privacy, pending, privacy, allowed)
                if privacy['action']!='none':
                    legacy=await self.forget_legacy(event,turn,privacy)
                    event.set_extra('dafeiyu_privacy_receipt',dict(legacy,learned=removed))
                    event.set_extra('dafeiyu_privacy_action',privacy['action'])
                event.set_extra('dafeiyu_learning_pending', items if privacy['action'] == 'none' else [])
                event.set_extra('dafeiyu_learning_valid',privacy['action']=='none' and bool(items))
                if privacy['action']!='none':event.set_extra('dafeiyu_semantic_forgetting',True)
                self.review_stats['privacy_removed'] += removed
            # High consequence factual claims retain semantic checking; ordinary turns use one generation.
            stage='review'
            if recover and self.config.get('response_review','enforce')!='enforce':
                raise ValueError('Metadata recovery requires enforced semantic review')
            if (recover or 'self' in generated.focus and 'fiction' not in generated.focus or 'action' in generated.focus
                    or any(c['kind'] in ('self_activity', 'external_action') for c in generated.claims)):
                checked = await self.reviewed(event, [generated.reply], event.unified_msg_origin)
                body = checked.segments[0]
                if recover:self.review_stats['metadata_recovered']+=1
            else:
                body = generated.reply
            confirmed=event.get_extra('dafeiyu_confirmed_delivery',[])
            sent=list(dict.fromkeys(r['text'] for r in confirmed if r.get('kind')=='reply' and r.get('text')))
            if sent:body='\n'.join(sent)
            response.completion_text = body
            event.set_extra('dafeiyu_generation_final_text',body)
            event.set_extra('dafeiyu_generation_valid', True)
            event.set_extra('dafeiyu_dialogue_last_review', 'contract')
            event.set_extra('dafeiyu_semantic_focus',list(generated.focus))
            event.set_extra('jingjing_expression_intent', {'intent': generated.expression,
                'text_hash': packet_key({'text': body})})
            self.review_stats['contract_turns'] += 1
            if sent:
                await self.natural().remember_delivered_reply(event)
                await self.commit_learning(event)
        except asyncio.CancelledError:
            response.completion_text = UNAVAILABLE
            raise
        except Exception as error:
            self.review_stats['contract_failed'] += 1
            self.review_stats['contract_failed_'+stage] += 1
            event.set_extra('dafeiyu_generation_failure_kind',stage)
            if stage=='format':event.set_extra('dafeiyu_learning_state_urgent',True)
            response.completion_text = UNAVAILABLE
            event.set_extra('dafeiyu_generation_final_text',UNAVAILABLE)
            if not event.get_extra('dafeiyu_learning_valid',False):event.set_extra('dafeiyu_learning_pending', [])
            event.set_extra('dafeiyu_dialogue_last_review', 'unavailable')
            logger.warning('Dialogue generation unavailable (%s): %s',stage,type(error).__name__)

    async def forget_legacy(self,event,turn,privacy):
        action=privacy['action'];ids=None if action=='forget_all' else privacy['ids']
        meta=self.context.get_registered_star('长期记忆系统')
        memory=getattr(meta,'star_cls',None) if meta and meta.activated else None
        long_term=0
        if memory and hasattr(memory,'forget_owned'):
            mids=None if ids is None else [int(i.split(':',1)[1]) for i in ids if i.startswith('long_term:')]
            long_term=await memory.forget_owned(event,mids)
        native={'episodes':0,'exchanges':0}
        if action in ('forget_selected','forget_all'):
            native=await asyncio.to_thread(self.natural().forget_semantic,turn.actor,turn.origin,ids)
            if self._index is not None:
                if ids is None:
                    fresh=await asyncio.to_thread(self.catalog().read,turn,time.time())
                    await asyncio.to_thread(self._index.sync,turn,fresh.items)
                else:await asyncio.to_thread(self._index.invalidate,ids)
            runtime=self.context.get_registered_star('dafeiyu_runtime')
            runtime=getattr(runtime,'star_cls',None) if runtime and runtime.activated else None
            outgoing=getattr(getattr(runtime,'outbound',None),'store',None)
            if outgoing is not None:await asyncio.to_thread(outgoing.forget,turn.actor,None)
        return dict(native,long_term=long_term)

    @filter.on_agent_done(priority=-sys.maxsize + 1)
    async def save_native_context(self,event,run_context,response):
        if not event.get_extra('dafeiyu_generation_contract',False):return
        event.set_extra('dafeiyu_native_context',run_context)
        event.set_extra('dafeiyu_native_expected_text',str(getattr(response,'completion_text','') or ''))
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
        from data.plugins.dafeiyu_natural_chat.main import content_text
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
                        from .event_state import updates
                        self.learning_state_inflight.add(source['owner'])
                        try:
                            from data.plugins.dafeiyu_natural_chat.main import content_text
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
            origin=turn.platform+':FriendMessage:'+owner
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
            await asyncio.to_thread(self.natural().forget_semantic,owner,origin,[])
            event.set_extra('dafeiyu_memory_mutated_in_turn',True)
            event.set_extra('dafeiyu_semantic_forgetting',True)
            event.set_extra('dafeiyu_privacy_action','forget_selected')
            event.set_extra('dafeiyu_context_epoch',self.learning_store().context_epoch())
            if self._index is not None:await asyncio.to_thread(self._index.invalidate,record_ids)
            return json.dumps({'owner':owner,'deleted':removed,'confirmed':True})
        except asyncio.CancelledError:raise
        except Exception as error:return json.dumps({'confirmed':False,'error_type':type(error).__name__})

    async def status(self):
        return json_response({'version': '0.3.0', 'mode': self.config.get('mode', 'shadow'),
            'legacy_adapter_ready': getattr(self.natural(), 'dialogue_context_protocol', 0) == 3,
            'active_private_user_count': len(self.config.get('active_private_users', [])),
            'retained_samples': len(self.samples), 'samples': list(self.samples)[-20:], 'errors': self.errors,
            'response_review': self.config.get('response_review', 'enforce'), 'review_statistics': dict(self.review_stats),
            'contains_chat_content': False,
            'background_model_calls':sum(self.review_stats.get(k+'_calls',0) for k in ('activity','initiative','learning_state')),
            'extra_qq_sends': 0,
            'source_databases_read_only': self.config.get('mode', 'shadow') != 'active',
            'learning_created': self._learning is not None,
            'index_created': self._index is not None,
            'feature_flags': {k: self.feature(k) for k in ('learning_v2', 'activity_policy_v2', 'initiative_policy_v2', 'voice_intent_v2')},
            'legacy_fact_guards_retained':self.config.get('mode')!='active' or not self.config.get('generation_contract',True),
            'deterministic_action_guards_retained':True})
