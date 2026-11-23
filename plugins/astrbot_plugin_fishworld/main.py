from __future__ import annotations

import asyncio
import copy
import sys
from functools import wraps
import time
import uuid
from contextlib import suppress
from datetime import datetime
from math import ceil

import aiohttp
from aiocqhttp import Event
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.filter.command import GreedyStr

from .core import (
    BEIJING,
    DEFAULT_EVENTS,
    DEFAULT_WAKE,
    KINDS,
    MAX_PENDING,
    UNLIMITED_WAKES,
    StateStore,
    day_at,
    format_notice,
    fresh_state,
    has_wake_quota,
    in_quiet_hours,
    wake_prompt,
)
from .followthrough import Followthrough
from .outfit_history import outfit_at
from .plans import (
    MISSED_HOLD,
    SIGNAL_KINDS,
    apply_change,
    fallback_due,
    has_world_activity,
    missed_plans,
    plan_context,
    retain_signal,
    select_plans,
)

API = "https://api.yuyuworld.app/v1/notify"
MARKER = "fishworld_wake_id"
MIN_POLL_SECONDS = 15
MAX_POLL_SECONDS = 3600
MIN_ACTIVITY_SECONDS = 5 * 60
MAX_ACTIVITY_SECONDS = 24 * 60 * 60


def duration_label(seconds: int) -> str:
    return f"{seconds // 60} 分钟" if seconds % 60 == 0 else f"{seconds} 秒"


def settings(config) -> dict:
    def number(name, default, low, high):
        value = int(config.get(name, default))
        return max(low, min(high, value))

    def wake_limit(name, default):
        value = int(config.get(name, default))
        if value == UNLIMITED_WAKES:
            return UNLIMITED_WAKES
        return max(0, min(1000, value))

    mode = config.get("mode", "agent")
    if mode not in ("agent", "notify"):
        raise ValueError("mode 只能是 agent 或 notify")
    result = {
        "enabled": bool(config.get("enabled", True)),
        "watch_key": str(config.get("watch_key", "")).strip(),
        "mode": mode,
        "interval_seconds": number("interval_seconds", 30, MIN_POLL_SECONDS, MAX_POLL_SECONDS),
        "activity_interval_seconds": number("activity_interval_seconds", 0, 0, MAX_ACTIVITY_SECONDS),
        "max_wakes_per_day": wake_limit("max_wakes_per_day", 48),
        "talk_cooldown_seconds": number("talk_cooldown_seconds", 300, 0, 86400),
        "idle_minutes": number("idle_minutes", 0, 0, 1440),
        "plan_wakes": bool(config.get("plan_wakes", False)),
        "max_idle_wakes_per_day": wake_limit("max_idle_wakes_per_day", 12),
        "busy_timeout_seconds": number("busy_timeout_seconds", 600, 60, 3600),
        "quiet_start": str(config.get("quiet_start", "")).strip(),
        "quiet_end": str(config.get("quiet_end", "")).strip(),
    }
    if result["activity_interval_seconds"]:
        result["activity_interval_seconds"] = max(MIN_ACTIVITY_SECONDS, result["activity_interval_seconds"])
    for name, default in (("event_kinds", DEFAULT_EVENTS), ("wake_kinds", DEFAULT_WAKE)):
        value = config.get(name, default)
        if not isinstance(value, list) or any(x not in KINDS for x in value):
            raise ValueError(f"{name} 应为已知事件类型列表")
        result[name] = set(value)
    in_quiet_hours(result["quiet_start"], result["quiet_end"], time.time())
    return result


@register(
    "astrbot_plugin_fishworld", "Fishworld AstrBot Contributors", "鱼的世界轮询与自动唤醒", "0.1.0"
)
class FishWorldPlugin(Followthrough, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = None
        self.session = None
        self.task = None
        self.lock = asyncio.Lock()
        self.storage_error = False
        self.last_error = ""
        self.last_check = 0.0
        self.last_response = 0.0
        self.last_skipped = 0
        self.failures = 0
        self.next_fetch = 0.0
        self.next_delivery = 0.0
        self.delivery_failures = 0
        self._history_guard = None
        self._active_plan_offer = None

    async def public_outfit_at(self, day, at, *, scene_row=None):
        """Read the resident's confirmed clothing at a dated public event."""
        return await outfit_at(self.context, day, at, scene_row)

    def _install_history_guard(self):
        if self._history_guard is not None:
            return

        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
            InternalAgentSubStage,
        )

        save_history = InternalAgentSubStage._save_to_history

        @wraps(save_history)
        async def scoped_save(stage, event, *args, **kwargs):
            message_id = getattr(event.message_obj, "message_id", "")
            if event.get_extra(MARKER) and str(message_id).startswith("fishworld_"):
                return
            return await save_history(stage, event, *args, **kwargs)

        InternalAgentSubStage._save_to_history = scoped_save
        self._history_guard = (InternalAgentSubStage, save_history, scoped_save)

    async def initialize(self):
        self.path = StarTools.get_data_dir("astrbot_plugin_fishworld") / "state.json"
        try:
            self.store = StateStore(self.path, time.time())
        except (OSError, ValueError, TypeError):
            self.storage_error = True
            self.last_error = "状态文件无法读取，已停止轮询；备份后可用 /鱼世界 重置确认 重建"
            logger.error("[鱼世界] " + self.last_error)
        self._install_history_guard()
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.task = asyncio.create_task(self._loop(), name="fishworld-poller")

    async def terminate(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.session:
            await self.session.close()
        if self._history_guard is not None:
            stage, original, wrapper = self._history_guard
            if stage._save_to_history is wrapper:
                stage._save_to_history = original
            self._history_guard = None

    def _save(self):
        try:
            self.store.save()
        except (OSError, ValueError, TypeError):
            self.storage_error = True
            raise RuntimeError("状态写入失败，已停止投递，请检查磁盘权限并重载插件") from None

    async def _loop(self):
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not log exception bodies: remote bodies and configs may contain secrets.
                if not self.storage_error:
                    self.last_error = "轮询配置或本地处理异常，请检查配置和状态目录后重载"
                else:
                    self.last_error = "状态不可用，轮询已停止；检查文件和写入权限后重载插件"
                logger.error("[鱼世界] " + self.last_error)
            await asyncio.sleep(2)

    async def check(self):
        async with self.lock:
            if self.storage_error or self.store is None:
                return
            cfg = settings(self.config)
            state = self.store.data
            now = time.time()
            busy = state["busy"]
            if busy and now - busy["started"] >= cfg["busy_timeout_seconds"]:
                state["busy"] = None
                state["last_delivery"] = "上次唤醒等待超时，执行结果未知；不会自动重试该批事件"
                self._save()
            if not cfg["enabled"] or state["paused"] or not state["binding"]:
                return
            if error := self._prepare_watch_key(cfg, now):
                self.last_error = error
                return
            state = self.store.data
            now = time.time()
            self.store.roll_day(now)
            if now >= self.next_fetch and len(state["pending"]) <= MAX_PENDING - 50:
                await self._fetch(cfg, now)
            planned = self._uses_plans(cfg)
            now = time.time()
            disabled = [row for row in state["pending"]
                        if row["item"]["kind"] not in cfg["event_kinds"]
                        and not (planned and retain_signal(state["world_plans"], row, now))]
            plans = [row for row in state["world_plans"] if now < row["until"] + MISSED_HOLD]
            if disabled or plans != state["world_plans"]:
                self.store.consume(disabled)
                state["world_plans"] = plans
                self._save()
            if time.time() >= self.next_delivery:
                await self._deliver(cfg, time.time())

    def _prepare_watch_key(self, cfg, now):
        key = cfg["watch_key"]
        if not key.startswith("fwr_") or any(c.isspace() for c in key):
            return "请在插件配置中填写 fwr_ 开头的观看密钥。"
        try:
            self.store.use_key(key, now)
        except OSError:
            self.storage_error = True
            raise
        return ""

    async def _fetch(self, cfg, now):
        self.last_check = now
        self.next_fetch = now + cfg["interval_seconds"]
        try:
            params = {"cursor": self.store.data["cursor"]} if self.store.data["cursor"] else {}
            async with self.session.get(
                API,
                params=params,
                headers={"Authorization": "Bearer " + cfg["watch_key"]},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    retry = 0
                    if response.status == 429:
                        with suppress(ValueError, TypeError):
                            retry = min(
                                86400, max(0, int(response.headers.get("Retry-After", "0")))
                            )
                    self._fetch_failed(f"通知接口 HTTP {response.status}", cfg, now, retry)
                    return None
                # Bound response size without echoing response contents to logs.
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 512000:
                        raise ValueError("响应过大")
                import json

                payload = json.loads(body)
                kinds = cfg["event_kinds"]
                if self._uses_plans(cfg):
                    kinds = kinds | SIGNAL_KINDS
                added = self.store.ingest(payload, kinds, time.time())
                skipped = payload.get("skipped", 0)
                self.last_skipped = skipped if type(skipped) is int and skipped > 0 else 0
                self.last_response = time.time()
                self.failures = 0
                self.last_error = ""
                return added
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self._fetch_failed("通知接口网络错误或超时", cfg, now)
        except (ValueError, TypeError, UnicodeError):
            self._fetch_failed("通知接口响应格式无效，保留原游标", cfg, now)
        except OSError:
            self.storage_error = True
            raise

    def _fetch_failed(self, message, cfg, now, retry=0):
        self.failures += 1
        delay = max(retry, min(900, cfg["interval_seconds"] * 2 ** min(self.failures, 5)))
        self.next_fetch = now + delay
        self.last_error = f"{message}；约 {delay} 秒后重试"
        logger.warning("[鱼世界] " + self.last_error)

    def _adapter(self):
        binding = self.store.data["binding"]
        for adapter in self.context.platform_manager.get_insts():
            if adapter.meta().id == binding["platform_id"] and adapter.meta().name == "aiocqhttp":
                return adapter
        raise RuntimeError("绑定的 NapCat / OneBot v11 平台未加载")

    def _make_event(self, prompt):
        adapter = self._adapter()
        binding = self.store.data["binding"]
        message = AstrBotMessage()
        message.type = MessageType(binding["message_type"])
        message.self_id = binding["self_id"]
        message.session_id = binding["session_id"]
        message.message_id = "fishworld_" + uuid.uuid4().hex
        message.sender = MessageMember(user_id=binding["sender_id"], nickname="鱼的世界后台提醒")
        message.group_id = binding["group_id"]
        message.message = [Plain(prompt)]
        message.message_str = prompt
        raw = {
            "time": int(time.time()),
            "self_id": int(binding["self_id"]),
            "post_type": "message",
            "message_type": "group" if binding["group_id"] else "private",
            "sub_type": "normal" if binding["group_id"] else "friend",
            "message_id": 0,
            "user_id": int(binding["sender_id"]),
            "message": [{"type": "text", "data": {"text": prompt}}],
            "raw_message": prompt,
            "sender": {"user_id": int(binding["sender_id"]), "nickname": "鱼的世界后台提醒"},
        }
        if binding["group_id"]:
            raw["group_id"] = int(binding["group_id"])
        message.raw_message = Event(raw)
        # Constructor unchanged between AstrBot v4.5.7 and the checked current source.
        event = AiocqhttpMessageEvent(
            message_str=prompt,
            message_obj=message,
            platform_meta=adapter.meta(),
            session_id=binding["session_id"],
            bot=adapter.get_client(),
        )
        event.is_wake = True
        event.is_at_or_wake_command = True
        event.set_extra(MARKER, uuid.uuid4().hex)
        return adapter, event

    def _dispatch(self, cfg, now, rows, *, idle=False, test=False, manual=False, due=(), missed=()):
        prompt = wake_prompt(rows, idle=idle, test=test, scheduled=bool(due),
                             free_time=idle and (self._uses_plans(cfg) or bool(cfg["activity_interval_seconds"])),
                             manual=manual)
        adapter, event = self._make_event(prompt)
        dialogue=self.context.get_registered_star("dafeiyu_dialogue")
        if (not manual and not test and not self.store.data["binding"].get("group_id")
                and getattr(dialogue,"activated",False) and getattr(dialogue.star_cls,"optional_world_share",False)):
            event.set_extra("fishworld_optional_owner_reply",True)
        event.set_extra("fishworld_completion_signals", [
            {"kind":r["item"]["kind"],"text":str(r["item"].get("text",""))[:180],"t":r["item"].get("t")}
            for r in rows if r["item"]["kind"] in SIGNAL_KINDS
        ])
        if not manual and not test:
            event.set_extra("fishworld_mood_events", [
                {"id": row["id"], "received": row.get("received", now), "kind": row["item"]["kind"],
                 "text": row["item"]["text"][:70]}
                for row in rows[:6]
            ])
        event.set_extra("fishworld_prefetch_world_now", not test)
        event.set_extra("fishworld_manual_test", test)
        if manual:
            event.set_extra("fishworld_manual_wake", True)
        if due or missed:
            event.set_extra("fishworld_plan_reminders", (list(due), list(missed)))
        previous = copy.deepcopy(self.store.data)
        state = self.store.data
        self.store.consume(rows)
        reminded = {plan["id"] for plan in (*due, *missed)}
        if reminded:
            state["world_plans"] = [r for r in state["world_plans"] if r["id"] not in reminded]
        state["wake_count"] += 1
        state["idle_count"] += int(idle)
        state["last_wake"] = now
        for row in rows:
            if row["item"]["kind"] == "talk":
                state["talk_at"][str(row["item"].get("from", ""))] = now
        state["busy"] = {"id": event.get_extra(MARKER), "started": now}
        state["last_delivery"] = "已提交 AstrBot 会话队列，等待模型运行"
        # Reserve before queuing: a crash must not automatically repeat world actions.
        self._save()
        try:
            adapter.commit_event(event)
        except Exception:
            self.store.data = previous
            self._save()
            raise

    def _uses_plans(self, cfg):
        binding = self.store.data["binding"]
        return bool(cfg["plan_wakes"] and binding and not binding["group_id"])

    def _plan_can_wake(self, cfg, now):
        state = self.store.data
        return (
            cfg["mode"] == "agent"
            and bool(state["cursor"])
            and has_wake_quota(state["wake_count"], cfg["max_wakes_per_day"])
            and not in_quiet_hours(cfg["quiet_start"], cfg["quiet_end"], now)
            and (not self._active_plan_offer or time.monotonic() >= self._active_plan_offer["deadline"])
        )

    def _plan_idle_due(self, cfg, now):
        state = self.store.data
        return (
            not state["wake_count"]
            and state["world_active_day"] != state["day"]
            and has_wake_quota(state["idle_count"], cfg["max_idle_wakes_per_day"])
            and not any(row["item"]["kind"] in cfg["event_kinds"] for row in state["pending"])
            and not any(row["event"] is None and row["until"] > now and day_at(row["due"]) == state["day"]
                        for row in state["world_plans"])
            and fallback_due(now, state["key_hash"])
        )

    async def _deliver(self, cfg, now):
        state = self.store.data
        if state["busy"]:
            return
        agent, notify = self.store.choose(cfg, now)
        planned = self._uses_plans(cfg)
        due, signals, missed = [], [], []
        idle = False
        if planned and self._plan_can_wake(cfg, now):
            periodic = cfg["activity_interval_seconds"] > 0
            timer_due = periodic and self._idle_due(cfg, now)
            due, signals = select_plans(
                state["world_plans"], state["pending"], now, joining=bool(agent) or timer_due,
                wake_kinds=cfg["wake_kinds"] & cfg["event_kinds"], wake_rows=agent,
            )
            if not (agent or due):
                idle = timer_due if periodic else self._plan_idle_due(cfg, now)
            if agent or due or idle:
                missed = missed_plans(state["world_plans"], now)
            if signals:
                handled = {row["id"] for row in signals}
                notify = [row for row in notify if row["id"] not in handled]
        try:
            if notify:
                sent = await self.context.send_message(
                    state["binding"]["umo"], MessageChain().message(format_notice(notify))
                )
                if not sent:
                    raise RuntimeError("绑定的平台不存在")
                self.store.consume(notify)
                state["last_delivery"] = "普通 QQ 提醒已发送"
                self._save()
            if agent or due or idle:
                batch = list({row["id"]: row for row in (*agent, *signals)}.values())
                self._dispatch(cfg, now, batch, idle=idle, due=due, missed=missed)
            elif (not planned and (cfg["activity_interval_seconds"] or not state["pending"])
                  and self._idle_due(cfg, now)):
                self._dispatch(cfg, now, [], idle=True)
            self.delivery_failures = 0
        except Exception:
            if self.storage_error:
                raise
            self.delivery_failures += 1
            delay = min(900, 30 * 2 ** min(self.delivery_failures, 5))
            self.next_delivery = now + delay
            self.last_error = f"QQ 投递或唤醒提交失败，约 {delay} 秒后重试；检查 NapCat 连接"
            logger.warning("[鱼世界] " + self.last_error)

    def _idle_due(self, cfg, now):
        state = self.store.data
        interval = cfg["activity_interval_seconds"]
        if not interval and cfg["idle_minutes"]:
            interval = max(MIN_ACTIVITY_SECONDS, cfg["idle_minutes"] * 60)
        return (
            cfg["mode"] == "agent"
            and interval > 0
            and bool(state["cursor"])
            and has_wake_quota(state["wake_count"], cfg["max_wakes_per_day"])
            and has_wake_quota(state["idle_count"], cfg["max_idle_wakes_per_day"])
            and now - state["last_wake"] >= interval
            and not in_quiet_hours(cfg["quiet_start"], cfg["quiet_end"], now)
        )

    @filter.on_llm_request()
    async def on_request(self, event: AstrMessageEvent, request):
        if self.config.get("plan_wakes", False):
            event.set_extra("fishworld_request_started", time.time())
        if event.get_extra(MARKER):
            async with self.lock:
                if (
                    self.store
                    and self.store.data["busy"]
                    and self.store.data["busy"]["id"] == event.get_extra(MARKER)
                ):
                    self.store.data["last_delivery"] = (
                        "模型已开始处理，使用此会话原有的人格、历史和工具配置"
                    )
                    self._save()

    @filter.on_llm_response()
    async def on_response(self, event: AstrMessageEvent, response):
        if event.get_extra(MARKER) and not getattr(response, "tools_call_name", []):
            async with self.lock:
                if (
                    self.store
                    and self.store.data["busy"]
                    and self.store.data["busy"]["id"] == event.get_extra(MARKER)
                ):
                    self.store.data["busy"] = None
                    self.store.data["last_delivery"] = (
                        "模型已返回最终响应（世界操作是否成功以工具结果为准）"
                    )
                    self._save()

    @filter.on_decorating_result(priority=sys.maxsize)
    async def quiet_background_result(self,event):
        if not event.get_extra("fishworld_optional_owner_reply") or event.get_extra("dafeiyu_world_share_final"):
            return
        result=event.get_result()
        if result and getattr(result,"chain",None) and all(isinstance(p,Plain) for p in result.chain):
            result.chain.clear()

    @filter.on_llm_request(priority=-sys.maxsize - 95)
    async def offer_plans(self, event: AstrMessageEvent, request):
        if (
            not self.config.get("plan_wakes", False) or self.storage_error or self.store is None
            or event.get_extra("fishworld_manual_test")
        ):
            return
        state = self.store.data
        binding = state["binding"]
        if (
            not binding or binding["group_id"] or binding["umo"] != event.unified_msg_origin
            or binding["sender_id"] != str(event.get_sender_id())
            or (state["paused"] and not event.get_extra("fishworld_manual_wake"))
            or not self.config.get("enabled", True)
            or self.config.get("mode", "agent") != "agent"
            or not state["cursor"]
        ):
            return
        now = time.time()
        due, missed = event.get_extra("fishworld_plan_reminders", ([], []))
        if not (event.get_extra("dafeiyu_generation_contract") and event.get_extra("dafeiyu_provider_json_mode")):
            if due or missed:
                request.system_prompt = (request.system_prompt or "") + plan_context(now, [], due, missed, structured=False)
            return
        pending = [row for row in state["world_plans"] if row["until"] > now]
        offer = {
            "binding": binding,
            "key_hash": state["key_hash"],
            "observed_at": event.get_extra("fishworld_request_started", now),
            "plans": [*pending, *due, *missed],
            "deadline": time.monotonic() + settings(self.config)["busy_timeout_seconds"],
        }
        event.set_extra("fishworld_plan_scope", offer)
        self._active_plan_offer = offer
        request.system_prompt = (request.system_prompt or "") + plan_context(now, pending, due, missed)

    @filter.on_llm_response(priority=-sys.maxsize)
    async def store_plan_response(self, event: AstrMessageEvent, response):
        offer = event.get_extra("fishworld_plan_scope")
        if not offer or getattr(response, "tools_call_name", None):
            return
        event.set_extra("fishworld_plan_scope", None)
        try:
            if not event.get_extra("dafeiyu_generation_valid") or event.get_extra("dafeiyu_generation_failure_kind"):
                return
            async with self.lock:
                if self.storage_error or self.store is None or not self.config.get("plan_wakes", False):
                    return
                state = self.store.data
                if (state["binding"] is not offer["binding"] or state["key_hash"] != offer["key_hash"]
                        or (state["paused"] and not event.get_extra("fishworld_manual_wake"))):
                    return
                now = time.time()
                evidence = event.get_extra("dafeiyu_dialogue_tool_evidence", [])
                previous = state["world_plans"]
                change = event.get_extra("fishworld_plan_change")
                if isinstance(change, dict):
                    try:
                        state["world_plans"] = apply_change(previous, change, offer["plans"],
                                                            now, offer["observed_at"], evidence)
                    except (TypeError, ValueError, OverflowError):
                        pass
                activity_day = state["world_active_day"]
                if not event.get_extra(MARKER) and has_world_activity(evidence):
                    state["world_active_day"] = day_at(now)
                if state["world_plans"] != previous or state["world_active_day"] != activity_day:
                    self._save()
        finally:
            if self._active_plan_offer is offer:
                self._active_plan_offer = None

    async def _poll_once(self, cfg):
        if not cfg["enabled"]:
            return "轮询插件已禁用，请先在配置页启用。"
        if not self.store.data["binding"]:
            return "请先在目标 QQ 会话发送 /鱼世界 绑定。"
        now = time.time()
        if error := self._prepare_watch_key(cfg, now):
            return error
        state = self.store.data
        if self.failures and now < self.next_fetch:
            return f"通知接口正在退避，请在 {ceil(self.next_fetch - now)} 秒后再查。"
        pending = len(state["pending"])
        if pending > MAX_PENDING - 50:
            return f"已有 {pending} 条待处理通知，队列腾出空间后可以继续查。"
        if self.last_check and now < self.last_check + MIN_POLL_SECONDS:
            wait = ceil(self.last_check + MIN_POLL_SECONDS - now)
            return f"刚查过通知，当前待处理 {pending} 条。请在 {wait} 秒后再查。"
        first = not state["cursor"]
        added = await self._fetch(cfg, now)
        if added is None:
            return self.last_error
        if first:
            return "已建立通知基准，下次开始接收新事件。"
        return f"本次新增 {added} 条通知，当前待处理 {len(state['pending'])} 条。"

    def _wake_now(self, event, cfg, *, test=False):
        now = time.time()
        state = self.store.data
        binding = state["binding"]
        if not binding or binding["umo"] != event.unified_msg_origin:
            return "请在已绑定的 QQ 会话里发送唤醒指令。"
        if state["busy"]:
            return "听澜正在处理上一轮，等她回来再唤醒。"
        self.store.roll_day(now)
        if not has_wake_quota(state["wake_count"], cfg["max_wakes_per_day"]):
            return "今日唤醒额度已用完。"
        due, signals, missed, rows = [], [], [], []
        if not test:
            if not cfg["enabled"] or cfg["mode"] != "agent":
                return "手动唤醒需要在插件配置中启用轮询并选择 agent 模式。"
            if in_quiet_hours(cfg["quiet_start"], cfg["quiet_end"], now):
                return f"现在是免打扰时段，{cfg['quiet_end']} 后可以唤醒。"
            if error := self._prepare_watch_key(cfg, now):
                return error
            if not self.store.data["cursor"]:
                return "请先用 /轮询 查一次 建立通知基准，再唤醒听澜。"
            rows, _ = self.store.choose(cfg, now)
            if self._uses_plans(cfg):
                due, signals = select_plans(
                    state["world_plans"], state["pending"], now, joining=True,
                    wake_kinds=cfg["wake_kinds"] & cfg["event_kinds"], wake_rows=rows,
                )
                missed = missed_plans(state["world_plans"], now)
        batch = list({row["id"]: row for row in (*rows, *signals)}.values())
        self._dispatch(cfg, now, batch, test=test, manual=not test, due=due, missed=missed)
        if test:
            return "已提交只读测试，听澜将调用 world_now 查看现状。本次计入模型唤醒。"
        return "已唤醒听澜一次，她会去鱼世界看看，自己决定做什么。"

    def _set_interval(self, value, *, notifications=False):
        if value.endswith("分钟"):
            digits, multiplier = value[:-2].strip(), 60
        else:
            digits, multiplier = value.removesuffix("秒").strip(), 1
        if notifications:
            key, minimum, maximum = "interval_seconds", MIN_POLL_SECONDS, MAX_POLL_SECONDS
            usage = "通知间隔须在 15 秒至 60 分钟之间，例如 /轮询 通知间隔 30秒。"
        else:
            key, minimum, maximum = "activity_interval_seconds", MIN_ACTIVITY_SECONDS, MAX_ACTIVITY_SECONDS
            usage = "自主活动间隔须在 5 分钟至 24 小时之间，例如 /轮询 间隔 10分钟。"
        if len(digits) > 5 or not digits.isascii() or not digits.isdecimal():
            return usage
        seconds = int(digits) * multiplier
        if not minimum <= seconds <= maximum:
            return usage
        previous = self.config.get(key)
        self.config[key] = seconds
        try:
            self.config.save_config()
        except OSError:
            if previous is None:
                self.config.pop(key, None)
            else:
                self.config[key] = previous
            return "间隔保存失败，请检查插件配置文件的写入权限。"
        if not notifications:
            return f"已保存，听澜的自主活动间隔为 {duration_label(seconds)}。每轮会调用模型。"
        if not self.failures:
            self.next_fetch = self.last_check + seconds
        return f"已保存通知轮询间隔，每 {seconds} 秒查一次。"

    def _status(self):
        def stamp(value):
            return (
                datetime.fromtimestamp(value, BEIJING).strftime("%m-%d %H:%M:%S")
                if value
                else "暂无"
            )

        def count_label(count, limit):
            if limit == UNLIMITED_WAKES:
                return f"{count} 次（不限）"
            return f"{count}/{limit}"

        cfg = settings(self.config)
        state = self.store.data
        self.store.roll_day(time.time())
        total = count_label(state["wake_count"], cfg["max_wakes_per_day"])
        idle = count_label(state["idle_count"], cfg["max_idle_wakes_per_day"])
        if cfg["mode"] == "notify":
            schedule = "仅通知模式"
        elif cfg["activity_interval_seconds"]:
            schedule = f"每 {duration_label(cfg['activity_interval_seconds'])}自主活动一轮"
        elif self._uses_plans(cfg):
            pending = sum(row["until"] > time.time() for row in state["world_plans"])
            schedule = f"按自己的安排和世界事件运行，待办 {pending} 件"
        else:
            schedule = f"每 {max(5, cfg['idle_minutes'])} 分钟活动一轮" if cfg["idle_minutes"] else "随世界事件唤醒"
        triggers = [label for kind, label in (("done", "活动结束"), ("arrive", "抵达目的地"))
                    if cfg["mode"] == "agent" and kind in cfg["wake_kinds"] and kind in cfg["event_kinds"]]
        if triggers:
            schedule += "\n" + "、".join(triggers) + "时，每条完成通知单独唤醒一轮"
        return (
            f"鱼世界状态（北京时间）\n启用：{cfg['enabled']}；暂停：{state['paused']}；模式：{cfg['mode']}\n"
            f"观看密钥已填写：{bool(cfg['watch_key'])}（不代表鉴权成功）\n"
            f"绑定：{state['binding']['umo'] if state['binding'] else '尚未绑定'}\n"
            f"通知查询间隔：{cfg['interval_seconds']} 秒；基准已建立：{bool(state['cursor'])}\n"
            f"最近请求：{stamp(self.last_check)}；最近有效响应：{stamp(self.last_response)}\n"
            f"待处理：{len(state['pending'])}；服务端 skipped：{self.last_skipped}\n"
            f"今日唤醒：{total}，其中闲时：{idle}\n"
            f"自主活动：{schedule}\n"
            f"免打扰时段：{cfg['quiet_start'] + '–' + cfg['quiet_end'] if cfg['quiet_start'] else '未设置'}\n"
            f"当前免打扰：{in_quiet_hours(cfg['quiet_start'], cfg['quiet_end'], time.time())}；等待模型：{bool(state['busy'])}\n"
            f"投递：{state['last_delivery']}\n最近错误：{self.last_error or '无'}"
        )

    @filter.command("心情", alias={"情绪值"})
    async def mood_command(self,event: AstrMessageEvent):
        """查看绑定私聊里听澜自己的当前心情。"""
        if event.get_extra(MARKER):
            return
        mood=self.read_self_mood(event,self.follow_scope(event))
        if mood is not None:
            summary = "、".join(f"{f['tone']} {f['strength']}/10" for f in mood.get("feelings", []))
            yield event.plain_result(f"听澜的心情 {mood['score']}/100 · {summary or mood['tone']}")
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("鱼世界", alias={"轮询"})
    async def command(self, event: AstrMessageEvent, arguments: GreedyStr):
        if event.get_extra(MARKER):
            return
        action, _, value = arguments.strip().partition(" ")
        if action not in ("间隔", "活动间隔", "通知间隔") and value:
            yield event.plain_result("指令参数有误，发送 /轮询 帮助 查看用法。")
            event.stop_event()
            return
        if action == "检查":
            await self.check()  # Does not bypass interval, Retry-After, pause or quotas.
        async with self.lock:
            if action == "重置确认":
                if self.path.exists():
                    backup = self.path.with_name(f"state.backup-{time.time_ns()}.json")
                    backup.write_bytes(self.path.read_bytes())
                old = self.store.data if self.store else {}
                self.store = StateStore.__new__(StateStore)
                self.store.path = self.path
                self.store.data = fresh_state(time.time())
                for key in (
                    "binding",
                    "paused",
                    "day",
                    "wake_count",
                    "idle_count",
                    "world_plans",
                    "outfit_photo",
                    "self_mood",
                    "world_shares",
                    "world_active_day",
                    "talk_at",
                    "last_wake",
                ):
                    if key in old:
                        self.store.data[key] = old[key]
                self.store.data["key_hash"] = old.get("key_hash", "")
                self._save()
                self.storage_error = False
                self.last_error = ""
                self.next_fetch = 0
                reply = (
                    "已备份并重建状态，清除待处理事件；下一次只建立新基准，不补历史。今日额度保持。"
                )
            elif self.storage_error or not self.store:
                reply = "状态文件不可用。检查状态目录权限后重载；若需丢弃旧队列并重新建立基准，发送 /鱼世界 重置确认。"
            elif action == "绑定":
                if event.get_platform_name() != "aiocqhttp":
                    reply = "请在已连接 NapCat 的 OneBot v11 QQ 会话里绑定。"
                else:
                    old = self.store.data["binding"]
                    if old and old["umo"] != event.unified_msg_origin:
                        reply = "已有其他会话绑定。请先发送 /鱼世界 解绑，再在目标会话绑定。"
                    else:
                        self.store.data["binding"] = {
                            "platform_id": event.get_platform_id(),
                            "umo": event.unified_msg_origin,
                            "session_id": event.message_obj.session_id,
                            "message_type": event.message_obj.type.value,
                            "sender_id": event.get_sender_id(),
                            "self_id": event.get_self_id(),
                            "group_id": event.get_group_id() or "",
                        }
                        self._save()
                        reply = "已绑定当前会话。配置 fwr_ 观看密钥后自动轮询；首次仅建立基准。"
            elif action == "解绑":
                if self.store.data["busy"]:
                    reply = (
                        "当前仍有已提交的唤醒，请等本轮结束或等待超时后解绑。暂停只能阻止后续唤醒。"
                    )
                else:
                    self.store.data.update(binding=None, world_plans=[], world_active_day="", outfit_photo=None, world_shares=[], self_mood=None)
                    self._save()
                    reply = "已解绑，停止轮询。已有待处理事件保留，重新绑定后可能投递到新会话。"
            elif action in ("暂停", "继续", "恢复"):
                self.store.data["paused"] = action == "暂停"
                self._save()
                reply = (
                    "已暂停自动轮询和唤醒。已经开始的一轮会照常结束。"
                    if action == "暂停"
                    else "已恢复轮询。"
                )
            elif action in ("状态", "检查"):
                reply = self._status()
            elif action in ("间隔", "活动间隔", "通知间隔"):
                reply = self._set_interval(value, notifications=action == "通知间隔")
            elif action == "查一次":
                reply = await self._poll_once(settings(self.config))
            elif action in ("试唤醒", "唤醒一次"):
                reply = self._wake_now(event, settings(self.config), test=action == "试唤醒")
            else:
                reply = (
                    "鱼世界轮询指令\n/轮询 状态　查看运行情况\n/轮询 暂停　暂停自动轮询与唤醒\n"
                    "/轮询 恢复　恢复自动运行\n/轮询 间隔 10分钟　设置听澜自主活动的频率（每轮调用模型）\n"
                    "/轮询 通知间隔 30秒　设置后台查通知的频率\n"
                    "/轮询 查一次　只查通知，可在暂停时使用\n"
                    "/轮询 唤醒一次　让听澜自主活动一轮，计入模型用量，遵守免打扰\n"
                    "/鱼世界 检查　按当前规则处理一轮\n/鱼世界 试唤醒　只读模型测试\n"
                    "/鱼世界 绑定、/鱼世界 解绑　管理目标会话\n"
                    "/心情　查看听澜的当前心情\n"
                    "/鱼世界 重置确认　备份并清空队列，重建基准"
                )
        # Stop other handlers after the reply has passed through QQ delivery.
        yield event.plain_result(reply)
        event.stop_event()
