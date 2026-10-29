"""Private, opt-in dialogue context; shadow mode does not mutate native requests."""
import asyncio
from collections import deque, Counter
import copy
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
from .persona import replace_persona
from .review import review, verify_revision, packet_key, ReviewResult, UNAVAILABLE
from .self_state import self_state


@register('dafeiyu_dialogue', 'Local', '有来源的对话资料与发送前语义核对', '0.2.0')
class Dialogue(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.root = Path(get_astrbot_data_path())
        self.samples = deque(maxlen=200)
        self.errors = 0
        self.review_stats = Counter()
        context.register_web_api('/dafeiyu_dialogue/status', self.status, ['GET'], '统一对话资料数值状态（无正文）')

    def natural(self):
        meta = self.context.get_registered_star('dafeiyu_natural_chat')
        return getattr(meta, 'star_cls', None) if meta and meta.activated else None

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
            req.func_tool = tools

    @filter.on_llm_request(priority=sys.maxsize + 100)
    async def choose_path(self, event, req):
        # Fail closed and avoid changing tools shared with the framework registry.
        event.set_extra(ACTIVE_KEY, False)
        try:
            turn = self.identity(event)
            selected = (self.config.get('mode', 'shadow') == 'active'
                        and getattr(self.natural(), 'dialogue_context_protocol', 0) == 2
                        and turn.actor in {str(x) for x in self.config.get('active_private_users', [])})
        except ValueError:
            selected = False
        event.set_extra(ACTIVE_KEY, selected)
        if selected:
            personas = getattr(getattr(self.context, 'persona_manager', None), 'personas', [])
            matching = {p.system_prompt for p in personas if isinstance(getattr(p, 'system_prompt', None), str)
                        and p.system_prompt and req.system_prompt.count(p.system_prompt) == 1}
            event.set_extra('dafeiyu_dialogue_persona', next(iter(matching)) if len(matching) == 1 else '')
        if not selected:
            self.hide_recall(req)

    @filter.on_llm_request(priority=-sys.maxsize - 90)
    async def prepare(self, event, req):
        # The group plugin may rebuild tools after choose_path; enforce again at the final hook.
        if not event.get_extra(ACTIVE_KEY, False):
            self.hide_recall(req)
        if self.config.get('mode', 'shadow') == 'off':
            return
        try:
            turn = self.identity(event)
            natural = self.natural()
            source = natural.input_snapshot(event)
            snapshot = await asyncio.to_thread(self.catalog(automatic=True).read, turn, time.time())
            # Only a verified artifact may supply the bot's own recent activity.
            record, progress = await self.own_activity()
            prompt, compacted = replace_persona(req.system_prompt, event.get_extra('dafeiyu_dialogue_persona', ''))
            built = compose(prompt, turn, snapshot, source['text'],
                facts=event.get_extra('jingjing_current_facts', {}),
                character=natural.dialogue_character() if hasattr(natural, 'dialogue_character') else None,
                preferences=natural.interaction_store().preferences(turn.actor), life=record, progress=progress,
                budget=self.config.get('memory_characters', 3600))
            selected = bool(event.get_extra(ACTIVE_KEY, False))
            self.samples.append(dict(built.metrics, active=selected, persona_compacted=compacted))
            if selected:
                req.system_prompt = built.prompt
                event.set_extra('jingjing_style_prepared', True)
                event.set_extra('dafeiyu_dialogue_evidence', built.evidence)
                event.set_extra('dafeiyu_dialogue_history', copy.deepcopy(req.contexts[-8:]))
                # Final fact/receipt guards and confirmed-delivery recording remain active.
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors += 1
            # Do not silently send an unguarded v2 response if context building fails.
            event.set_extra(ACTIVE_KEY, False)
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

    async def provider_call(self, event, prompt, policy, purpose):
        provider = await self.context.get_using_provider_async(event.unified_msg_origin)
        if provider is None:
            raise ValueError('Current provider unavailable')
        self.review_stats[purpose + '_calls'] += 1
        started = time.perf_counter()
        try:
            response = await provider.text_chat(prompt=prompt, system_prompt=policy, contexts=[],
                func_tool=None, request_max_retries=0)
            usage = getattr(response, 'usage', None)
            for name in ('input_other', 'input_cached', 'output'):
                value = getattr(usage, name, 0)
                if type(value) is int and value >= 0:
                    self.review_stats[purpose + '_' + name] += value
            return response.completion_text
        finally:
            self.review_stats[purpose + '_elapsed_ms'] += round((time.perf_counter() - started) * 1000)

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
        if (not event.get_extra(ACTIVE_KEY, False) or getattr(response, 'tools_call_name', [])
                or not response.completion_text.strip()):
            return
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
        if not event.get_extra(ACTIVE_KEY, False):
            return
        text = result if isinstance(result, str) else None
        rows = event.get_extra('dafeiyu_dialogue_tool_evidence', [])
        if len(rows) < 8:
            rows.append({'tool': getattr(tool, 'name', ''), 'result': text if text and len(text) <= 5000 else None,
                         'complete': bool(text and len(text) <= 5000)})
            event.set_extra('dafeiyu_dialogue_tool_evidence', rows)

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
            return json.dumps({'records': [item.data() for item in rows], 'method': method,
                'unavailable_sources': current.unavailable, 'complete_history': False}, ensure_ascii=False)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors += 1
            return json.dumps({'records': [], 'unavailable': True, 'error_type': type(error).__name__})

    async def status(self):
        return json_response({'version': '0.2.0', 'mode': self.config.get('mode', 'shadow'),
            'legacy_adapter_ready': getattr(self.natural(), 'dialogue_context_protocol', 0) == 2,
            'active_private_user_count': len(self.config.get('active_private_users', [])),
            'retained_samples': len(self.samples), 'samples': list(self.samples)[-20:], 'errors': self.errors,
            'response_review': self.config.get('response_review', 'enforce'), 'review_statistics': dict(self.review_stats),
            'contains_chat_content': False, 'background_model_calls': 0, 'extra_qq_sends': 0,
            'source_databases_read_only': True, 'legacy_fact_guards_retained': True})
