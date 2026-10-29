"""Observe successful private API sends without changing or repeating delivery."""
import asyncio
import hashlib
import json
import logging
import math
import re
import time

from astrbot.core.agent.message import TextPart
from .diagnostics import active
from .outbound import BOT, PREFIX, MARKER, OutboundStore, outgoing_text, message_id, bridge, reply_note

LOG = logging.getLogger(__name__)


class PrivateOutbound:
    def __init__(self, context, root):
        self.context = context
        self.store = OutboundStore(root / 'plugin_data/dafeiyu_runtime/outgoing.db')
        self.wrappers = []
        self.stats = {'recorded': 0, 'record_failures': 0, 'history_added': 0}

    def allowed(self, qq):
        if not re.fullmatch(r'[1-9]\d{4,11}', str(qq)) or str(qq) == BOT:
            return False
        cfg = self.context.get_config(PREFIX + str(qq)).get('platform_settings', {})
        allowed = {str(x) for x in cfg.get('id_whitelist', [])}
        return not cfg.get('enable_id_white_list', True) or str(qq) in allowed or PREFIX + str(qq) in allowed

    async def cid(self, qq):
        manager = getattr(self.context, 'conversation_manager', None)
        getter = getattr(manager, 'get_curr_conversation_id', None)
        if not callable(getter):
            return '?'
        try:
            return str(await asyncio.wait_for(getter(PREFIX + str(qq)), timeout=.5) or '')
        except Exception:
            return '?'

    async def observe_bot(self):
        getter = getattr(self.context, 'get_platform', None)
        platform = getter('aiocqhttp') if callable(getter) else None
        bot = getattr(platform, 'bot', None)
        original = getattr(bot, 'call_action', None)
        if (platform is None or platform.meta().id != 'dafeiyu-qq' or not callable(original)
                or getattr(original, '_dafeiyu_outbound_owner', None) is self):
            return

        async def call_action(action, **params):
            target = str(params.get('user_id', ''))
            private = action == 'send_private_msg' or action == 'send_msg' and params.get('message_type') == 'private'
            capture = False
            try:
                turn = active()
                same_agent = (turn is not None and turn.calls.get('agent_runs')
                              and turn.session == hashlib.sha256((PREFIX + target).encode()).hexdigest()[:16])
                capture = (private and not same_agent and self.allowed(target)
                           and str(params.get('self_id', BOT)) == BOT)
            except Exception:
                self.stats['record_failures'] += 1
            conversation = await self.cid(target) if capture else ''
            # An exception or unknown result never becomes a confirmed entry.
            receipt = await original(action, **params)
            if capture:
                try:
                    if self.store.record(target, outgoing_text(params.get('message')), receipt, conversation):
                        self.stats['recorded'] += 1
                except Exception as error:
                    self.stats['record_failures'] += 1
                    LOG.warning('Confirmed private history not saved: %s', type(error).__name__)
            return receipt

        call_action._dafeiyu_outbound_owner = self
        bot.call_action = call_action
        self.wrappers.append((bot, original, call_action))

    async def seed_balance(self, root):
        path = root / 'plugin_data/astrbot_plugin_dafeiyu_balance/state.json'
        if not path.exists():
            return
        try:
            from data.plugins.astrbot_plugin_dafeiyu_balance.reply_context import notice_records, latest_notice
            state = json.loads(path.read_text(encoding='utf-8-sig'))
            now = time.time()
            for qq in {r['qq'] for r in notice_records(state, None, now)}:
                if not self.allowed(qq):
                    continue
                row = latest_notice(state, qq, now)
                if row:
                    self.store.record(qq, row['text'], {'message_id': row['id']}, await self.cid(qq), 'balance_legacy', row['at'])
        except Exception as error:
            self.stats['record_failures'] += 1
            LOG.warning('Legacy private history not imported: %s', type(error).__name__)

    async def prepare(self, event, req):
        raw = getattr(getattr(event, 'message_obj', None), 'raw_message', None)
        uid = str(event.get_sender_id())
        if (event.get_platform_name() != 'aiocqhttp' or event.get_platform_id() != 'dafeiyu-qq'
                or not event.is_private_chat() or event.unified_msg_origin != PREFIX + uid
                or not isinstance(raw, dict) or raw.get('post_type') != 'message'
                or str(raw.get('self_id')) != BOT or str(raw.get('user_id')) != uid
                or event.get_extra('cron_job', None) or event.get_extra('cron_payload', None)
                or not self.allowed(uid)):
            return
        stamp = raw.get('time')
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not 0 <= time.time() - stamp <= 300:
            return
        conversation = str(getattr(getattr(req, 'conversation', None), 'cid', '') or '')
        if not conversation:
            conversation = await self.cid(uid)
        if conversation == '?':
            return
        source = event.get_extra('jingjing_original_input', {}) or {}
        human = str(source.get('text', event.message_str) or '')
        parts = getattr(req, 'extra_user_content_parts', None)
        if parts is None:
            parts = [];req.extra_user_content_parts = parts
        # The old balance hook remains a fallback when this shared entry is unavailable.
        parts[:] = [p for p in parts if not isinstance(p, TextPart) or not p.text.startswith((MARKER, '【本会话已经发出的饭钱消息】'))]
        from data.plugins.dafeiyu_natural_chat.exchanges import forget_spec
        forgetting = forget_spec(human)
        if forgetting and not event.get_extra('jingjing_outbound_forgotten', False):
            self.store.forget(uid, None if forgetting['across'] else conversation,
                              forgetting.get('query', ''), forgetting['mode'] == 'latest')
            event.set_extra('jingjing_outbound_forgotten', True)
        if forgetting:
            event.set_extra('jingjing_outbound_managed', True)
            return
        rows = self.store.recent(uid, conversation, stamp)
        event.set_extra('jingjing_outbound_managed', self.store.manages_peer(uid))
        history = event.get_extra('jingjing_actual_chat', []) or []
        for row in rows:
            contexts, added = bridge(getattr(req, 'contexts', None), row, history)
            if added:
                req.contexts = contexts
                self.stats['history_added'] += 1
            self.store.bind(uid, row['id'], conversation)
        if rows:
            parts.append(TextPart(text=reply_note(rows, human, history)))

    def close(self):
        for bot, original, wrapper in reversed(self.wrappers):
            if getattr(bot, 'call_action', None) is wrapper:
                bot.call_action = original
        self.wrappers.clear()

    def status(self):
        return dict(self.store.status(), **self.stats, observer_active=any(getattr(bot, 'call_action', None) is wrapper for bot, _, wrapper in self.wrappers))
