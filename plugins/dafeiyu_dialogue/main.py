"""Private, opt-in dialogue context; shadow mode does not mutate native requests."""
import asyncio
from collections import deque
import copy
from pathlib import Path
import sys
import time
import json

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import json_response
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .adapters import MemoryCatalog
from .context_builder import compose
from .contracts import ACTIVE_KEY, TurnContext
from .retrieval import lexical_rank, recall_candidates, semantic_select


@register('dafeiyu_dialogue', 'Local', '统一私聊资料与有来源的语义召回，支持只读影子模式', '0.1.1')
class Dialogue(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.root = Path(get_astrbot_data_path())
        self.samples = deque(maxlen=200)
        self.errors = 0
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
                        and getattr(self.natural(), 'dialogue_context_protocol', 0) == 1
                        and turn.actor in {str(x) for x in self.config.get('active_private_users', [])})
        except ValueError:
            selected = False
        event.set_extra(ACTIVE_KEY, selected)
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
            meta = self.context.get_registered_star('dafeiyu_life')
            life = getattr(meta, 'star_cls', None) if meta and meta.activated else None
            record = life.public_context() if life else None
            built = compose(req.system_prompt, turn, snapshot, source['text'],
                facts=event.get_extra('jingjing_current_facts', {}),
                preferences=natural.interaction_store().preferences(turn.actor), life=record,
                budget=self.config.get('memory_characters', 3600))
            selected = bool(event.get_extra(ACTIVE_KEY, False))
            self.samples.append(dict(built.metrics, active=selected))
            if selected:
                req.system_prompt = built.prompt
                event.set_extra('jingjing_style_prepared', True)
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
            provider = await self.context.get_using_provider_async(event.unified_msg_origin)
            if provider is None:
                raise ValueError('Current provider unavailable')

            async def call(prompt, policy):
                response = await provider.text_chat(prompt=prompt, system_prompt=policy, contexts=[],
                    func_tool=None, request_max_retries=0)
                return response.completion_text

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
        return json_response({'version': '0.1.1', 'mode': self.config.get('mode', 'shadow'),
            'legacy_adapter_ready': getattr(self.natural(), 'dialogue_context_protocol', 0) == 1,
            'active_private_user_count': len(self.config.get('active_private_users', [])),
            'retained_samples': len(self.samples), 'samples': list(self.samples)[-20:], 'errors': self.errors,
            'contains_chat_content': False, 'background_model_calls': 0, 'extra_qq_sends': 0,
            'source_databases_read_only': True, 'legacy_fact_guards_retained': True})
