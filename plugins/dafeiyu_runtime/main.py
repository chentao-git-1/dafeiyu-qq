import asyncio
import contextlib
import datetime
import hashlib
import re
import sqlite3
import sys
import time
from pathlib import Path

from astrbot.api.event import filter
from astrbot.api import logger
from astrbot.api.star import Context, Star
from astrbot.api.web import json_response
from astrbot.core.message.components import (
    At,
    Face,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.session_lock import session_lock_manager

from .flow import ChatFlow
from .ops import load_state, problem, result
from .reliability import LoopWatchdog, ReplyReliability
from .diagnostics import CURRENT, KEY, Journal, Turn, active, phase, request_metrics
from .private_outbound import PrivateOutbound

WRITES = {
    "dafeiyu_draw",
    "dafeiyu_sing",
    "dafeiyu_qzone_publish",
    "dafeiyu_qzone_draft",
    "dafeiyu_tasks",
    "dafeiyu_friend_allowlist",
    "memorize_long_term_memory",
    "forget_long_term_memory",
    "send_message_to_user",
}


class DafeiyuRuntime(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.flow = ChatFlow()
        self.health_task = None
        self.pulse_task = None
        self.original_execute = PipelineScheduler.execute
        self.original_send = AiocqhttpMessageEvent.send
        self.replies = ReplyReliability(self.original_send)
        self.loop_watchdog = LoopWatchdog()
        self.diagnostics = Journal(Path(get_astrbot_data_path()) / 'plugin_data/dafeiyu_runtime/diagnostics.json')
        self.tts_wrappers = []
        self.outbound = PrivateOutbound(context, Path(get_astrbot_data_path()))
        self.original_session_lock = session_lock_manager.acquire_lock
        self.context.register_web_api(
            "/dafeiyu_runtime/status",
            self.status,
            ["GET"],
            "小鲸鱼运行状态（不含密钥及聊天正文）",
        )
        self.context.register_web_api('/dafeiyu_runtime/diagnostics', self.diagnostic_status, ['GET'], '回复耗时与服务商实际报告的用量（无正文）')

    async def initialize(self):
        original_execute = self.original_execute
        await self.outbound.observe_bot()
        await self.outbound.seed_balance(Path(get_astrbot_data_path()))

        @contextlib.asynccontextmanager
        async def measured_lock(session_id):
            started = time.monotonic()
            turn = active()
            matching = turn and turn.session == hashlib.sha256(str(session_id).encode()).hexdigest()[:16]
            entered = False
            try:
                async with self.original_session_lock(session_id):
                    entered = True
                    if matching:
                        turn.phases['session_wait'] = turn.phases.get('session_wait', 0) + (time.monotonic() - started) * 1000
                    yield
            finally:
                if matching and not entered:
                    turn.phases['session_wait'] = turn.phases.get('session_wait', 0) + (time.monotonic() - started) * 1000

        self.session_lock_wrapper = measured_lock
        session_lock_manager.acquire_lock = measured_lock

        async def execute(scheduler, event):
            if event.get_platform_id() != "dafeiyu-qq":
                return await original_execute(scheduler, event)
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if callable(getattr(raw, "get", None)) and raw.get("post_type") not in (
                None,
                "message",
            ):
                return await original_execute(scheduler, event)
            turn = None
            token = None
            cancelled = False
            try:
                turn = Turn(event)
                event.set_extra(KEY, turn)
                turn.attach_trace(event)
                token = CURRENT.set(turn)
            except Exception:
                pass
            try:
                with contextlib.suppress(Exception):
                    await self.outbound.observe_bot()
                    await self.observe_tts_provider(event)
                return await self.replies.execute(original_execute, scheduler, event)
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                try:
                    if turn is not None:
                        self.diagnostics.append(turn.finish(event, cancelled))
                except Exception:
                    pass
                finally:
                    if token is not None:
                        CURRENT.reset(token)

        async def send(event, message):
            if event.get_platform_id() != "dafeiyu-qq":
                return await self.original_send(event, message)
            return await self.replies.send(event, message)

        self.execute_wrapper = execute
        self.send_wrapper = send
        PipelineScheduler.execute = execute
        AiocqhttpMessageEvent.send = send
        self.loop_watchdog.thread.start()
        self.pulse_task = asyncio.create_task(self.loop_watchdog.pulse())
        self.health_task = asyncio.create_task(self.health_loop())

    async def terminate(self):
        await self.replies.close()
        self.outbound.close()
        if session_lock_manager.acquire_lock is getattr(self, 'session_lock_wrapper', None):
            session_lock_manager.acquire_lock = self.original_session_lock
        for provider, original, wrapper in self.tts_wrappers:
            if getattr(provider, 'get_audio', None) is wrapper:
                provider.get_audio = original
        self.tts_wrappers.clear()
        if PipelineScheduler.execute is getattr(self, "execute_wrapper", None):
            PipelineScheduler.execute = self.original_execute
        if AiocqhttpMessageEvent.send is getattr(self, "send_wrapper", None):
            AiocqhttpMessageEvent.send = self.original_send
        if self.pulse_task:
            self.pulse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.pulse_task
        await self.loop_watchdog.close()
        if self.health_task:
            self.health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.health_task
        await self.diagnostics.flush()

    async def diagnostic_status(self):
        return json_response(self.diagnostics.status())

    async def observe_tts_provider(self, event):
        """Cover already-instantiated providers too; plugin reload need not rebuild TTS."""
        getter = getattr(self.context, 'get_using_tts_provider_async', None)
        if not callable(getter):
            return
        provider = await asyncio.wait_for(getter(event.unified_msg_origin), timeout=.3)
        original = getattr(provider, 'get_audio', None)
        if not callable(original) or getattr(original, '_dafeiyu_diagnostic_owner', None) is self:
            return
        # Installed providers are few; bound references if configurations keep changing.
        if len(self.tts_wrappers) >= 16:
            return
        async def get_audio(*args, **kwargs):
            text = args[0] if args else kwargs.get('text', '')
            with phase('tts', characters=len(text) if isinstance(text, str) else 0):
                return await original(*args, **kwargs)
        get_audio._dafeiyu_diagnostic_owner = self
        provider.get_audio = get_audio
        self.tts_wrappers.append((provider, original, get_audio))

    def space_state(self):
        path = (
            Path(get_astrbot_data_path())
            / "plugin_data/astrbot_plugin_qzone/posts_5.db"
        )
        summary = {"unconfirmed_actions": 0, "next_runs": []}
        if not path.exists():
            return summary
        with sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "social_actions" in tables:
                summary["unconfirmed_actions"] = connection.execute(
                    "SELECT count(*) FROM social_actions WHERE status IN ('started','unknown')"
                ).fetchone()[0]
            if "daily_publish_runs" in tables:
                rows = connection.execute(
                    "SELECT day,run_at,status FROM daily_publish_runs ORDER BY day DESC LIMIT 2"
                ).fetchall()
                summary["next_runs"] = [
                    {"day": day, "run_at": run_at, "status": status}
                    for day, run_at, status in rows
                ]
        return summary

    async def status(self):
        return json_response(
            {
                "operations": load_state(),
                "chat": self.flow.stats,
                "space": self.space_state(),
                "delivery": dict(self.replies.stats),
                "loop_watchdog": dict(self.loop_watchdog.stats),
                "pipeline_guard_active": PipelineScheduler.execute
                is getattr(self, "execute_wrapper", None),
                "send_guard_active": AiocqhttpMessageEvent.send
                is getattr(self, "send_wrapper", None),
                "loop_watchdog_active": self.loop_watchdog.thread.is_alive()
                and self.pulse_task is not None
                and not self.pulse_task.done(),
                "reply_timeout_seconds": self.replies.timeout,
                "progress_after_seconds": self.replies.progress_after,
                "send_timeout_seconds": self.replies.send_timeout,
                "progress_notice_cooldown_seconds": self.replies.progress_cooldown,
                "version": "0.3.2",
                "outbound_history": self.outbound.status(),
                "session_wait_diagnostics": session_lock_manager.acquire_lock is getattr(self, 'session_lock_wrapper', None),
                "reply_diagnostics": {'enabled': True, 'retained_turns': self.diagnostics.status()['retained_turns'], 'contains_chat_content': False},
                "confirmed_delivery_receipts": True,
                "group_reply_policy_active": callable(getattr(AiocqhttpMessageEvent, "_dafeiyu_group_reply_policy", None)),
            }
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("小鲸鱼状态", alias={"机器人状态"})
    async def status_command(self, event):
        if str(event.get_sender_id()) != "7000000001":
            return
        operations = load_state()
        space = self.space_state()
        qq = {
            "online": "在线",
            "checking_failed": "检查未通过",
            "error": "需要检查",
        }.get(operations.get("qq", {}).get("status"), "等待检查")
        interaction = operations.get("qzone_interactions", {})
        labels = {
            "completed": "已完成",
            "no_updates": "没有可互动的新动态",
            "error": "需要检查",
        }
        lines = [
            "QQ：" + qq,
            "空间互动：" + labels.get(interaction.get("status"), "等待本轮检查"),
        ]
        if interaction.get("status") == "completed":
            lines.append(
                "最近一轮：评论{}条、点赞{}条".format(
                    interaction.get("commented", 0), interaction.get("liked", 0)
                )
            )
        timezone = datetime.timezone(datetime.timedelta(hours=8))
        for plan in space["next_runs"][:1]:
            when = datetime.datetime.fromtimestamp(plan["run_at"], timezone).strftime(
                "%m-%d %H:%M"
            )
            lines.append(
                "空间发布："
                + (
                    "当日已发"
                    if plan["status"] == "published"
                    else "计划 " + when + "（北京时间）"
                )
            )
        if space["unconfirmed_actions"]:
            lines.append(
                "有{}项公开操作结果待核对，已避免自动重发。".format(
                    space["unconfirmed_actions"]
                )
            )
        yield event.plain_result("\n".join(lines))

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command('回复诊断')
    async def diagnostic_command(self, event):
        if str(event.get_sender_id()) != '7000000001' or event.get_platform_id() != 'dafeiyu-qq':
            return
        rows = [row for row in self.diagnostics.status()['turns'] if any(row['calls'].get(key) for key in ('agent_runs','vision','tts'))]
        if not rows:
            yield event.plain_result('还没有回复诊断记录。')
            return
        row = rows[0]
        when = datetime.datetime.fromtimestamp(row['at'],datetime.timezone(datetime.timedelta(hours=8))).strftime('%m-%d %H:%M')
        lines = ['最近一轮（北京时间{}，{}）：{:.2f}秒，结果：{}'.format(when,'群聊' if row['scope']=='group' else '私聊',row['total_ms'] / 1000,row['outcome'])]
        labels = {'preparation':'准备', 'session_wait':'会话排队', 'first_response':'模型首个完整响应', 'history':'聊天记录', 'memory':'片段回忆', 'schedule':'日程', 'agent':'主模型与工具', 'tools':'工具', 'vision':'图片识别', 'tts':'语音合成', 'send':'发送'}
        lines.append('；'.join('{}{:.2f}秒'.format(labels[k], v / 1000) for k, v in row['phases_ms'].items()))
        for source, label in (('main','主模型'), ('vision','图片识别')):
            usage = row['reported_tokens'].get(source)
            lines.append(label + ('已报告 Token：输入{}（缓存命中{}），输出{}'.format(usage['input_other'] + usage['input_cached'], usage['input_cached'], usage['output']) if usage else '用量：未报告'))
        lines.append('部分耗时互相包含；Token只累计实际报告部分，未估算金额。')
        yield event.plain_result('\n'.join(lines))

    def allowed(self, event):
        if event.get_platform_name() != "aiocqhttp":
            return False
        config = self.context.get_config(event.unified_msg_origin)["platform_settings"]
        if not config.get("enable_id_white_list"):
            return True
        allowed = {str(value) for value in config.get("id_whitelist", [])}
        group = str(event.get_group_id() or "")
        return (
            (group in allowed)
            if group
            else (
                str(event.get_sender_id()) in allowed
                or event.unified_msg_origin in allowed
            )
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize - 50)
    async def incoming(self, event):
        if not self.allowed(event):
            return
        # OneBot notices/requests are also converted to private/group events.
        # They must not advance the chat epoch and invalidate a pending reply.
        raw = getattr(event.message_obj, "raw_message", None)
        if callable(getattr(raw, "get", None)) and raw.get("post_type") not in (
            None,
            "message",
        ):
            return
        parts = event.get_messages()
        if not str(event.message_str or "").strip():
            faces = [part for part in parts if isinstance(part, Face)]
            if faces:
                # Native QQ faces have no text prompt and are otherwise skipped
                # by the agent. Retain the original components for other plugins.
                face_names = {3: "发呆", 13: "呲牙", 14: "微笑"}
                face_text = " ".join(
                    "[QQ表情：{}]".format(
                        face_names.get(part.id, "编号{}".format(part.id))
                    )
                    for part in faces
                )
                event.message_str = face_text
                event.message_obj.message = parts + [Plain(face_text)]
            elif not any(
                isinstance(part, (File, Image, Record, Reply, Video)) for part in parts
            ):
                # Unsupported or empty input is a normal skip, not a failed reply.
                return
        self.replies.admit(event)
        key = event.unified_msg_origin + "|" + str(event.get_sender_id())
        epoch = self.flow.observe(key, getattr(event.message_obj, "message_id", None))
        if epoch is None:
            event.stop_event()
            return
        event.set_extra("dafeiyu_flow_key", key)
        event.set_extra("dafeiyu_flow_epoch", epoch)
        text = str(event.message_str or "").strip()
        # Explicit commands keep their current immediate handling.
        if re.match(r"^[/!！]", text) or re.match(
            r"^(?:查余额|查询余额|余额|记忆|发说说|写说说|过稿|拒稿)(?:\s|$)", text
        ):
            return
        has_plain = any(
            isinstance(part, Plain) and part.text.strip()
            for part in event.get_messages()
        )
        if not has_plain and any(
            isinstance(part, Record) for part in event.get_messages()
        ):
            config = self.context.get_config(event.unified_msg_origin)
            model = (
                config.get("agent_runner", {})
                .get("config", {})
                .get("model", {})
                .get("provider_id")
            )
            provider = next(
                (row for row in config.get("provider", []) if row.get("id") == model),
                {},
            )
            modalities = provider.get("modalities")
            if (
                not config.get("provider_stt_settings", {}).get("enable")
                and modalities
                and "audio" not in modalities
            ):
                await event.send(
                    event.plain_result("这条语音我还没听清，发文字给我好不好？")
                )
                self.replies.stats["audio_not_understood"] += 1
                event.stop_event()
                return
        if not text or any(
            not isinstance(part, (Plain, At)) for part in event.get_messages()
        ):
            return
        combined = await self.flow.collect(key, event, text)
        if combined is None:
            event.stop_event()
            return
        if combined != text:
            event.message_str = combined
            mentions = [part for part in event.get_messages() if isinstance(part, At)]
            event.message_obj.message = mentions + [Plain(combined)]

    def fresh(self, event):
        key = event.get_extra("dafeiyu_flow_key")
        return (
            not key
            or event.get_extra("dafeiyu_flow_write", False)
            or self.flow.fresh(key, event.get_extra("dafeiyu_flow_epoch"))
        )

    @filter.on_llm_request(priority=-sys.maxsize - 100)
    async def reply_requested(self, event, request):
        if self.allowed(event) and not event.is_stopped():
            self.replies.expect(event)
            request_metrics(request, event)

    @filter.on_llm_request(priority=-1000035)
    async def outgoing_history(self, event, request):
        try:
            await self.outbound.prepare(event, request)
        except Exception as error:
            self.outbound.stats['record_failures'] += 1
            logger.warning('Private outgoing context unavailable: %s', type(error).__name__)

    @filter.on_llm_response(priority=sys.maxsize)
    async def first_model_response(self, event, response):
        turn = active(event)
        if turn and turn.agent_started is not None and 'first_response' not in turn.phases:
            turn.phases['first_response'] = max(0, time.monotonic() - turn.agent_started) * 1000

    @filter.on_llm_tool_respond()
    async def confirm_tool_delivery(self, event, tool, arguments, result):
        if (
            getattr(tool, "name", "") != "send_message_to_user"
            or not self.allowed(event)
            or result is None
            or getattr(result, "isError", False)
            or not getattr(event, "_has_send_oper", False)
            or not isinstance(arguments, dict)
        ):
            return
        messages=arguments.get('messages')
        if not isinstance(messages,list):return
        if not any(
            isinstance(message, dict)
            and (
                message.get("type") != "plain"
                or str(message.get("text", "")).strip()
            )
            for message in messages
        ):
            return
        confirmation = "Message sent to session " + event.unified_msg_origin
        if not any(
            str(getattr(part, "text", "")).strip() == confirmation
            for part in getattr(result, "content", [])
        ):
            return
        texts=[]
        for message in messages:
            # The native tool only renders text on plain components. A record's
            # optional text argument is ignored and is not a verified transcript.
            if isinstance(message,dict) and str(message.get('type','')).lower()=='plain' and str(message.get('text','')).strip():
                texts.append(Plain(str(message['text']).strip()))
        self.replies.confirm_delivery(event,texts,'reply')
        state = self.replies.state(event)
        if state and not state["expired"]:
            state["sends"] += 1
            state["final_sends"] += 1
            state["send_started"] = True
            state["completed"] = True

    @filter.on_using_llm_tool(priority=sys.maxsize)
    async def protect_writes(self, event, tool, tool_args):
        turn = active(event)
        if turn:
            turn.tool_begin()
        name = getattr(tool, "name", "")
        mutating = name in WRITES or (
            name == "jingjing_episodes"
            and (tool_args or {}).get("action") in ("save", "forget")
        )
        self.replies.tool_started(event, mutating)
        if name == "send_message_to_user":
            state = self.replies.state(event)
            if state:
                # The built-in tool bypasses event.send(). Finish any pending
                # progress notice before the tool can send its own message.
                async with state["send_lock"]:
                    state["send_started"] = True
        if mutating:
            event.set_extra("dafeiyu_flow_write", True)

    @filter.on_llm_tool_respond(priority=sys.maxsize)
    async def diagnostic_tool_done(self, event, tool, arguments, result):
        turn = active(event)
        if turn:
            turn.tool_end()

    @filter.on_decorating_result(priority=sys.maxsize - 10)
    async def suppress_stale_reply(self, event):
        response = event.get_result()
        if (
            not response
            or not response.is_llm_result()
            or not event.get_extra("dafeiyu_flow_key")
        ):
            return
        if event.get_extra("dafeiyu_flow_write", False):
            return
        # A later message is not proof that its replacement answer was delivered.
        # Keep completed answers; only the short, pre-generation burst is merged.
        event.set_extra("dafeiyu_flow_ordinary", True)

    async def health_loop(self):
        await asyncio.sleep(8)
        failures = 0
        while True:
            client = None
            try:
                await self.outbound.observe_bot()
                platform = self.context.get_platform("aiocqhttp")
                client = getattr(platform, "bot", None)
                if client is None:
                    raise RuntimeError("QQ client unavailable")
                status = await asyncio.wait_for(client.get_status(), timeout=10)
                if status.get("online") is not True:
                    raise RuntimeError("QQ offline")
                failures = 0
                result("qq", "online", error_code=None, consecutive_failures=0)
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                result("qq", "checking_failed", consecutive_failures=failures)
                if failures >= 2:
                    await problem(client, "qq", "QQ_OFFLINE")
            await self.diagnostics.flush()
            await asyncio.sleep(120)
