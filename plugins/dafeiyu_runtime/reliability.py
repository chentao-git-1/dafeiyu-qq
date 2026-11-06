"""Bound QQ pipeline waits and retain text when optional media cannot be sent."""

import asyncio
import sys
import threading
import time
import traceback
from collections import OrderedDict

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.message.components import At, Image, Plain, Record
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from .delivery import RECEIPTS_KEY, append_receipt
from dafeiyu.runtime.diagnostics import phase
from dafeiyu.dialogue.action_facts import system_notice

STATE_KEY = "dafeiyu_reply_delivery"


class MediaDeliveryFailed(RuntimeError):
    """A text fallback was sent, but media delivery was not confirmed."""


class ReplyReliability:
    """Observe a reply's outcome without replaying external operations."""

    def __init__(
        self,
        original_send,
        timeout=180,
        progress_after=25,
        send_timeout=40,
        progress_cooldown=60,
    ):
        self.original_send = original_send
        self.timeout = timeout
        self.progress_after = progress_after
        self.send_timeout = send_timeout
        self.progress_cooldown = progress_cooldown
        self.notice_times = OrderedDict()
        self.tasks = set()
        self.task_states = {}
        self.stats = {
            "started": 0,
            "completed": 0,
            "filtered": 0,
            "timeouts": 0,
            "empty_fallbacks": 0,
            "progress_notices": 0,
            "send_failures": 0,
            "media_text_fallbacks": 0,
            "fallback_failures": 0,
            "late_sends_blocked": 0,
            "active": 0,
            "audio_not_understood": 0,
            "progress_suppressed": 0,
            "quota_suppressed": 0,
            "partial_deliveries": 0,
        }

    @staticmethod
    def state(event):
        return event.get_extra(STATE_KEY, None)

    def admit(self, event):
        state = self.state(event)
        if state:
            state["admitted"] = True

    def expect(self, event):
        state = self.state(event)
        if state and not state["expired"]:
            state["admitted"] = True
            state["reply_expected"] = True
            state["processing_started"].set()

    def tool_started(self, event, mutating=False):
        self.expect(event)
        state = self.state(event)
        if state:
            state["completed"] = False
            state["write_started"] = state["write_started"] or mutating

    @staticmethod
    def confirm_delivery(event, components, purpose='reply'):
        text=' '.join(str(getattr(p,'text','') or '').strip() for p in components if isinstance(p,(Plain,Record))).strip()
        media=['image' if isinstance(p,Image) else 'audio' for p in components if isinstance(p,(Image,Record))]
        append_receipt(event,text,purpose,media)

    async def _send_direct(self, event, message, purpose='reply'):
        policy=getattr(AiocqhttpMessageEvent,'_dafeiyu_group_reply_policy',None)
        if policy is not None and not policy(event,message):return False
        components=list(getattr(message,'chain',[]) or [])
        with phase('send', event):
            result=await asyncio.wait_for(
                self.original_send(event, message), timeout=self.send_timeout
            )
        if result is not False:self.confirm_delivery(event,components,purpose)
        return result

    async def _notice(self, event, text, notify_quota=True):
        try:
            chain = []
            if event.get_group_id():
                chain.append(At(qq=str(event.get_sender_id())))
            chain.append(Plain(text))
            result=await self._send_direct(event, MessageChain(chain=chain),purpose='notice')
            return result is not False
        except Exception as error:
            if type(error).__name__ == "ReplyLimitExceeded":
                self.stats["quota_suppressed"] += 1
                event.stop_event()
                notify = getattr(AiocqhttpMessageEvent, "_dafeiyu_reply_notice", None)
                if notify_quota and notify is not None:
                    await notify(event)
                return False
            self.stats["fallback_failures"] += 1
            logger.warning("Reply notice delivery failed: %s", type(error).__name__)
            return False

    async def _progress(self, event, state):
        """Retired compatibility hook; do not replace or preface model speech."""
        return

    async def send(self, event, message):
        state = self.state(event)
        if state and state["expired"]:
            self.stats["late_sends_blocked"] += 1
            return
        components = list(getattr(message, "chain", []) or [])
        components = [
            part
            for part in components
            if not isinstance(part, Plain) or str(part.text or "").strip()
        ]
        if not components:
            return
        has_media = any(isinstance(part, (Record, Image)) for part in components)
        plain = " ".join(
            part.text for part in components if isinstance(part, Plain)
        ).strip()
        if state:
            state["send_started"] = True
        try:
            async with state["send_lock"] if state else asyncio.Lock():
                if state and state["expired"]:
                    self.stats["late_sends_blocked"] += 1
                    return
                result = await self._send_direct(event, message,purpose='reply')
                if result is False:return
                if state:
                    state["sends"] += 1
                    state["final_sends"] += 1
                    state["completed"] = True
        except Exception as error:
            if type(error).__name__ == "ReplyLimitExceeded":
                event.stop_event()
                notify = getattr(AiocqhttpMessageEvent, "_dafeiyu_reply_notice", None)
                if notify is not None:
                    await notify(event)
                raise
            self.stats["send_failures"] += 1
            logger.warning(
                "QQ delivery failed: type=%s media=%s", type(error).__name__, has_media
            )
            if not has_media:
                if state:
                    state["delivery_failed"] = True
                raise
            # Do not resend the media or repeat a generation. Voice text is retained
            # by both native and explicit TTS even when dual output is disabled.
            text = (
                plain
                or " ".join(
                    str(getattr(part, "text", "") or "")
                    for part in components
                    if isinstance(part, Record)
                ).strip()
            )
            fallback_purpose = 'reply' if text else 'notice'
            if not text:
                text = system_notice('media_delivery', dict(status='unconfirmed',
                    media='audio' if any(isinstance(part, Record) for part in components) else 'image', retry=False))
            fallback = [part for part in components if isinstance(part, At)][:1]
            fallback.append(Plain(text))
            try:
                result = await self._send_direct(event, MessageChain(chain=fallback), purpose=fallback_purpose)
                if result is False:return
            except Exception:
                self.stats["fallback_failures"] += 1
                raise
            self.stats["media_text_fallbacks"] += 1
            if state:
                state["sends"] += 1
                state["final_sends"] += 1
                state["completed"] = True
            # Tool callers must not mistake a fallback notice for an image/song
            # delivery and announce that the requested media was sent.
            raise MediaDeliveryFailed(
                "Media delivery unconfirmed; text fallback sent"
            ) from error
        return result

    async def execute(self, original, scheduler, event):
        state = {
            "admitted": False,
            "completed": False,
            "expired": False,
            "write_started": False,
            "sends": 0,
            "final_sends": 0,
            "reply_expected": False,
            "processing_started": asyncio.Event(),
            "send_lock": asyncio.Lock(),
            "send_started": False,
            "delivery_failed": False,
        }
        event.set_extra(STATE_KEY, state)
        self.stats["started"] += 1
        self.stats["active"] += 1
        task = asyncio.create_task(original(scheduler, event))
        self.tasks.add(task)
        self.task_states[task] = (event, state)

        def finished(completed):
            self.tasks.discard(completed)
            self.task_states.pop(completed, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.timeout)
            if not done:
                state["expired"] = True
                event.stop_event()
                task.cancel()
                self.stats["timeouts"] += 1
                if state["admitted"] and not state["completed"]:
                    text = system_notice('reply', dict(status='timeout', operation_unconfirmed=state['write_started'], retry=False))
                    await self._notice(event, text)
                await asyncio.wait({task}, timeout=2)
                return
            await task
            if (
                state["delivery_failed"]
                and state["final_sends"]
                and not event.is_stopped()
            ):
                self.stats["partial_deliveries"] += 1
                await self._notice(
                    event, system_notice('reply', dict(status='partial_delivery', retry=False))
                )
            elif state["completed"]:
                self.stats["completed"] += 1
            elif (
                event.is_stopped()
                or not state["admitted"]
                or not state["reply_expected"]
            ):
                self.stats["filtered"] += 1
            else:
                self.stats["empty_fallbacks"] += 1
                await self._notice(event, system_notice('reply', dict(status='empty_result')))
        except asyncio.CancelledError:
            state["expired"] = True
            task.cancel()
            raise
        except Exception as error:
            logger.warning("QQ pipeline failed: %s", type(error).__name__)
            if (
                state["delivery_failed"]
                and state["final_sends"]
                and not event.is_stopped()
            ):
                self.stats["partial_deliveries"] += 1
                await self._notice(
                    event, system_notice('reply', dict(status='partial_delivery', retry=False))
                )
            elif state["admitted"] and not state["completed"] and not event.is_stopped():
                self.stats["empty_fallbacks"] += 1
                await self._notice(event, system_notice('reply', dict(status='failed', retry=False)))
        finally:
            self.stats["active"] -= 1

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            tracked = self.task_states.get(task)
            if tracked:
                event, state = tracked
                state["expired"] = True
                event.stop_event()
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=2)


class LoopWatchdog:
    """Sample only stack locations when the event loop stops making progress."""

    def __init__(self, threshold=8):
        self.threshold = threshold
        self.heartbeat = time.monotonic()
        self.thread_id = threading.get_ident()
        self.stop = threading.Event()
        self.stats = {"samples": 0, "last_lag_seconds": 0, "last_stack": []}
        self.thread = threading.Thread(
            target=self._watch, daemon=True, name="qq-loop-watchdog"
        )

    def _watch(self):
        last_sample = 0
        while not self.stop.wait(2):
            now = time.monotonic()
            lag = now - self.heartbeat
            if lag < self.threshold or now - last_sample < 30:
                continue
            frame = sys._current_frames().get(self.thread_id)
            stack = traceback.extract_stack(frame)[-24:] if frame else []
            del frame
            # No local values, message text, credentials, or source expressions.
            locations = [f"{row.filename}:{row.lineno}:{row.name}" for row in stack]
            self.stats.update(
                samples=self.stats["samples"] + 1,
                last_lag_seconds=round(lag, 2),
                last_stack=locations,
            )
            logger.warning(
                "QQ event loop blocked for %.1fs; locations=%s", lag, locations
            )
            last_sample = now

    async def pulse(self):
        while not self.stop.is_set():
            self.heartbeat = time.monotonic()
            await asyncio.sleep(1)

    async def close(self):
        self.stop.set()
        if self.thread.is_alive():
            await asyncio.to_thread(self.thread.join, 3)
