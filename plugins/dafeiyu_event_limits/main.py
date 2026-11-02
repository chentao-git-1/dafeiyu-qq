"""Apply DeepSeek peak-hour quotas to QQ replies, preserving notice handling."""

import asyncio
import contextvars
import hashlib
import math
import sys
import uuid
import weakref
from collections import OrderedDict
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.star import Star, register
from astrbot.api.web import json_response
from astrbot.core.message.components import Plain
from astrbot.core.pipeline.rate_limit_check.stage import RateLimitStage
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .quota import ReplyLedger, ReplyLimitExceeded

PLATFORM = "dafeiyu-qq"
NOTICE_TURN = contextvars.ContextVar("dafeiyu_quota_notice_turn", default=None)


def reply_scope(raw, is_group, session_id):
    """Identify one logical reply across text, voice and segmented sends.

    Args:
        raw: Original OneBot event, or None for a proactive send.
        is_group: Whether the destination is a QQ group.
        session_id: Destination QQ number or group number.

    Returns:
        A turn identifier, the replied-to user, and the destination group.
    """
    raw = raw if callable(getattr(raw, "get", None)) else {}
    target = str(session_id or raw.get("group_id" if is_group else "user_id") or "")
    user = str(raw.get("user_id") or "") if is_group else target
    if is_group and raw.get("post_type") != "message":
        user = ""
    group = target if is_group else ""
    message_id = raw.get("message_id")
    if message_id is None:
        # Missing IDs must not collapse unrelated messages into one quota entry.
        message_id = raw.setdefault("_dafeiyu_reply_turn", uuid.uuid4().hex)
    identity = (
        str(raw.get("self_id") or "7000000002"),
        "group" if is_group else "private",
        target,
        user,
        str(message_id),
    )
    return hashlib.sha256("|".join(identity).encode()).hexdigest(), user, group


@register("dafeiyu_event_limits", "Local", "DeepSeek峰谷回复限流与恢复提示", "0.2.2")
class EventLimits(Star):
    def __init__(self, context):
        super().__init__(context)
        self.skipped = {}
        self.denied = {"user": 0, "group": 0, "send": 0}
        self.send_locks = weakref.WeakValueDictionary()
        self.notice_until = OrderedDict()
        self.notice_count = 0
        self.ledger = ReplyLedger(
            Path(get_astrbot_data_path())
            / "plugin_data/dafeiyu_event_limits/replies.db",
            exempt_users=self.admin_users,
        )
        context.register_web_api(
            "/dafeiyu_event_limits/status",
            self.status,
            ["GET"],
            "DeepSeek峰谷回复限流状态",
        )

    def admin_users(self):
        """Use the same trusted administrator list as AstrBot permissions."""
        return {str(uid) for uid in self.context.get_config().get("admins_id", []) if uid}

    async def initialize(self):
        self.original = RateLimitStage.process
        self.original_send = AiocqhttpMessageEvent.__dict__["send_message"]
        self.original_ledger = getattr(
            AiocqhttpMessageEvent, "_dafeiyu_reply_ledger", None
        )
        self.original_notice = getattr(
            AiocqhttpMessageEvent, "_dafeiyu_reply_notice", None
        )
        original, ledger = self.original, self.ledger

        async def message_budget(stage, event):
            if event.get_platform_id() != PLATFORM:
                return await original(stage, event)
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if callable(getattr(raw, "get", None)):
                kind = raw.get("post_type")
                if kind in ("notice", "request", "meta_event"):
                    self.skipped[kind] = self.skipped.get(kind, 0) + 1
                    return
            # Quotas apply to replies, so merely receiving a message costs no slot.
            await self.check_event(event)

        original_send = self.original_send

        async def guarded_send(
            cls, bot, message_chain, event=None, is_group=False, session_id=None
        ):
            platform = self.context.get_platform("aiocqhttp")
            if (
                platform is None
                or platform.meta().id != PLATFORM
                or bot is not platform.bot
                or not message_chain.chain
            ):
                return await original_send.__get__(None, cls)(
                    bot, message_chain, event, is_group, session_id
                )
            turn, user, group = reply_scope(event, is_group, session_id)
            if NOTICE_TURN.get() == turn:
                return await original_send.__get__(None, cls)(
                    bot, message_chain, event, is_group, session_id
                )
            lock = self.send_locks.setdefault(turn, asyncio.Lock())
            async with lock:
                try:
                    reserved = ledger.reserve(turn, user, group)
                except ReplyLimitExceeded:
                    self.denied["send"] += 1
                    raise
                try:
                    result = await original_send.__get__(None, cls)(
                        bot, message_chain, event, is_group, session_id
                    )
                except BaseException:
                    if reserved:
                        ledger.finish(turn, False)
                    raise
                if reserved:
                    ledger.finish(turn, True)
                return result

        self.wrapper = message_budget
        self.send_wrapper = classmethod(guarded_send)
        RateLimitStage.process = self.wrapper
        AiocqhttpMessageEvent.send_message = self.send_wrapper
        # Shared read-only admission check used by the proactive group path.
        AiocqhttpMessageEvent._dafeiyu_reply_ledger = ledger
        self.notice_callback = self.check_event
        AiocqhttpMessageEvent._dafeiyu_reply_notice = self.notice_callback

    async def terminate(self):
        if RateLimitStage.process is getattr(self, "wrapper", None):
            RateLimitStage.process = self.original
        if AiocqhttpMessageEvent.__dict__["send_message"] is getattr(
            self, "send_wrapper", None
        ):
            AiocqhttpMessageEvent.send_message = self.original_send
        if getattr(AiocqhttpMessageEvent, "_dafeiyu_reply_ledger", None) is self.ledger:
            AiocqhttpMessageEvent._dafeiyu_reply_ledger = self.original_ledger
        if (
            getattr(AiocqhttpMessageEvent, "_dafeiyu_reply_notice", None)
            is self.notice_callback
        ):
            AiocqhttpMessageEvent._dafeiyu_reply_notice = self.original_notice

    @filter.on_llm_request(priority=sys.maxsize)
    async def check_event(self, event, request=None):
        if event.get_platform_id() != PLATFORM:
            return
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        is_group = bool(event.get_group_id())
        target = event.get_group_id() if is_group else event.get_sender_id()
        turn, user, group = reply_scope(raw, is_group, target)
        reason = self.ledger.blocked(user, group, turn)
        if reason:
            self.denied[reason] += 1
            event.stop_event()
            now = self.ledger.clock()
            notice_key = (
                (group or user, reason) if reason == "group" else (user, reason)
            )
            if self.notice_until.get(notice_key, 0) > now:
                return
            retry_at = self.ledger.retry_at(user, group)
            self.notice_until[notice_key] = max(now + 5, retry_at)
            self.notice_until.move_to_end(notice_key)
            while len(self.notice_until) > 256:
                self.notice_until.popitem(last=False)
            minutes = max(1, math.ceil((retry_at - now) / 60))
            text = (
                "这个群这半小时的回复次数用完啦，大约{}分钟后恢复。"
                if reason == "group"
                else "这半小时给你的回复次数用完啦，大约{}分钟后恢复。"
            ).format(minutes)
            token = NOTICE_TURN.set(turn)
            try:
                await asyncio.wait_for(
                    event.send(MessageChain(chain=[Plain(text)])), timeout=10
                )
                self.notice_count += 1
            except Exception as error:
                logger.warning("Quota notice delivery failed: %s", type(error).__name__)
            finally:
                NOTICE_TURN.reset(token)

    @filter.on_decorating_result(priority=sys.maxsize)
    async def check_result(self, event):
        if event.get_platform_id() != PLATFORM:
            return
        await self.check_event(event)
        if event.is_stopped():
            result = event.get_result()
            if result:
                result.chain = []

    async def status(self):
        return json_response(
            dict(
                self.ledger.summary(),
                active=RateLimitStage.process is self.wrapper,
                send_guard_active=AiocqhttpMessageEvent.__dict__["send_message"]
                is self.send_wrapper,
                skipped_non_message_events=self.skipped,
                denied=self.denied,
                quota_notice_count=self.notice_count,
                quota_notices_use_model=False,
                personal_scope="qq_user_across_private_and_groups",
                segments_count_as_one_reply=True,
                off_peak_unlimited=True,
                admin_exempt=True,
                admin_exempt_from_group_quota=True,
                admin_replies_consume_quota=False,
                admin_identity_source="AstrBot.admins_id",
                pricing_source="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            )
        )
