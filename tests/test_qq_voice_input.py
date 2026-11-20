"""QQ voice input reaches the native preprocessor and the normal dialogue text."""
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "plugins"), str(ROOT / "framework")]

from aiocqhttp import Event
import httpx
from astrbot.core.message.components import Record, Reply
from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
from dafeiyu_mention_guard.main import DafeiyuMentionGuard
from dafeiyu_bailian_tts import bailian_asr
from dafeiyu_natural_chat.main import NaturalChat
from dafeiyu_runtime.main import DafeiyuRuntime
from dafeiyu_runtime.qq_voice import finish_voice_text, prepare_voice_event


def wave_file():
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 4000)
    return data.getvalue()


async def qq_event(segments, *, group=False, record_data=None):
    adapter = object.__new__(AiocqhttpAdapter)
    adapter.settings = {}
    adapter.bot = SimpleNamespace(call_action=AsyncMock(return_value={"base64": record_data}))
    raw = Event(dict(post_type="message", message_type="group" if group else "private",
                     time=0, message_id=191, self_id=22, user_id=11, group_id=33 if group else None,
                     sender={"user_id": 11, "nickname": "fixture"}, message=segments, raw_message=""))
    message = await adapter.convert_message(raw)
    return AiocqhttpMessageEvent(message.message_str, message, PlatformMetadata("aiocqhttp", "fixture", "dafeiyu-qq"),
                                 message.session_id, adapter.bot)


def environment(stt):
    config = dict(provider_stt_settings={"enable": True, "provider_id": "dafeiyu-bailian-asr"},
                  platform_settings={"enable_id_white_list": True, "id_whitelist": ["11", "33"]})
    guard = object.__new__(DafeiyuMentionGuard)
    ctx = SimpleNamespace(get_config=lambda origin: config, get_using_stt_provider_async=AsyncMock(return_value=stt),
                          get_registered_star=lambda name: SimpleNamespace(activated=True, star_cls=guard))
    guard.context = ctx
    stage = PreProcessStage()
    stage.config = config
    stage.stt_settings = config["provider_stt_settings"]
    stage.platform_settings = config["platform_settings"]
    stage.plugin_manager = SimpleNamespace(context=ctx)
    return ctx, stage


class QQVoiceInput(unittest.IsolatedAsyncioTestCase):
    async def test_private_audio_and_caption_keep_their_order_and_fetch_once(self):
        audio = base64.b64encode(wave_file()).decode()
        ev = await qq_event([{"type": "text", "data": {"text": "留言："}},
                             {"type": "record", "data": {"file": "fixture-napcat-media-id",
                                                        "path": "/not-shared/example.silk"}}], record_data=audio)
        stt = SimpleNamespace(get_text=AsyncMock(return_value="下午见"))
        ctx, stage = environment(stt)
        try:
            self.assertTrue(prepare_voice_event(ctx, ev))
            await stage.process(ev)
            self.assertEqual(NaturalChat.input_snapshot(SimpleNamespace(), ev)["text"], "留言：下午见")
            ev.bot.call_action.assert_awaited_once_with("get_record", self_id="22", file="fixture-napcat-media-id", out_format="wav")
            stt.get_text.assert_awaited_once()
        finally:
            ev.cleanup_temporary_local_files()

    async def test_directly_resolvable_audio_uses_no_onebot_roundtrip(self):
        audio = base64.b64encode(wave_file()).decode()
        ev = await qq_event([{"type": "record", "data": {"file": "base64://" + audio}}])
        stt = SimpleNamespace(get_text=AsyncMock(return_value="收到"))
        ctx, stage = environment(stt)
        try:
            self.assertTrue(prepare_voice_event(ctx, ev))
            await stage.process(ev)
            self.assertEqual(ev.get_message_str(), "收到")
            ev.bot.call_action.assert_not_awaited()
        finally:
            ev.cleanup_temporary_local_files()

    async def test_group_requires_the_existing_real_mention_before_audio_access(self):
        clip = {"type": "record", "data": {"file": "group-fixture"}}
        stt = SimpleNamespace(get_text=AsyncMock(return_value="下午见"))
        ctx, stage = environment(stt)
        ev = await qq_event([clip], group=True)
        self.assertFalse(prepare_voice_event(ctx, ev))
        self.assertTrue(ev.is_stopped())
        ev.bot.call_action.assert_not_awaited()
        stt.get_text.assert_not_awaited()
        ev = await qq_event([clip], group=True, record_data=base64.b64encode(wave_file()).decode())
        # The adapter normally looks up the @ display name in QQ. Stub just its raw segment here.
        ev.message_obj.raw_message["message"].insert(0, {"type": "at", "data": {"qq": "22"}})
        try:
            self.assertTrue(prepare_voice_event(ctx, ev))
            await stage.process(ev)
            self.assertEqual(NaturalChat.input_snapshot(SimpleNamespace(), ev)["text"], "下午见")
        finally:
            ev.cleanup_temporary_local_files()

    async def test_unreadable_audio_stops_before_a_chat_reply(self):
        ev = await qq_event([{"type": "record", "data": {"file": "expired-fixture"}}])
        ev.send = AsyncMock()
        stt = SimpleNamespace(get_text=AsyncMock())
        ctx, stage = environment(stt)
        self.assertTrue(prepare_voice_event(ctx, ev))
        await stage.process(ev)
        flow = SimpleNamespace(observe=lambda key, mid: 1)
        runtime = SimpleNamespace(allowed=lambda event: True, context=ctx, flow=flow,
                                  replies=SimpleNamespace(admit=Mock(), stats={"audio_not_understood": 0}))
        await DafeiyuRuntime.incoming(runtime, ev)
        stt.get_text.assert_not_awaited()
        ev.send.assert_awaited_once()
        self.assertTrue(ev.is_stopped())
        self.assertEqual(runtime.replies.stats["audio_not_understood"], 1)
        ev.bot.call_action.assert_awaited_once()

    async def test_quoted_voice_stays_attributed_to_the_original_speaker(self):
        ev = await qq_event([{"type": "text", "data": {"text": "这是谁说的？"}}])
        audio = Record.fromBase64(base64.b64encode(wave_file()).decode())
        ev.message_obj.message.insert(0, Reply(id="701", sender_id=12, chain=[audio]))
        ctx, stage = environment(SimpleNamespace(get_text=AsyncMock(return_value="周末见。")))
        try:
            self.assertTrue(prepare_voice_event(ctx, ev))
            await stage.process(ev)
            finish_voice_text(ev)
            self.assertEqual(ev.get_messages()[0].chain[0].text, "周末见。")
            self.assertEqual(ev.get_message_str(), "这是谁说的？")
            self.assertEqual(ev.message_obj.message_str, "这是谁说的？")
        finally:
            ev.cleanup_temporary_local_files()

    async def test_voice_before_typed_text_is_in_the_same_order_in_the_request(self):
        audio = base64.b64encode(wave_file()).decode()
        ev = await qq_event([{"type": "record", "data": {"file": "base64://" + audio}},
                             {"type": "text", "data": {"text": "还是下周？"}}])
        ctx, stage = environment(SimpleNamespace(get_text=AsyncMock(return_value="这周末，")))
        try:
            self.assertTrue(prepare_voice_event(ctx, ev))
            await stage.process(ev)
            finish_voice_text(ev)
            self.assertEqual(ev.request_llm(prompt=ev.get_message_str()).prompt, "这周末，还是下周？")
        finally:
            ev.cleanup_temporary_local_files()


class BailianCloud(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wav = Path(tmp.name) / "example.wav"
        self.calls = []
        self.status = 200
        self.body = {"output": {"text": "下午见。"}}

        async def respond(request):
            self.calls.append(request)
            return httpx.Response(self.status, json=self.body)

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(bailian_asr.httpx, "AsyncClient", return_value=client):
            self.provider = bailian_asr.BailianASR({"id": "test-asr", "credential_provider_id": "voice"}, {})
        self.addAsyncCleanup(self.provider.terminate)
        config = {"provider": [{"id": "voice", "type": "dafeiyu_bailian_tts",
                                "workspace_id": "example", "api_key": "example-credential"}]}
        mock_config = patch.object(bailian_asr, "astrbot_config", config)
        mock_config.start()
        self.addCleanup(mock_config.stop)

    def write(self, seconds=1, silence=False):
        with wave.open(str(self.wav), "wb") as f:
            f.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            f.writeframes((b"\x00\x00" if silence else b"\x05\x00") * 8000 * seconds)

    async def test_one_short_recording_makes_one_authenticated_bailian_request(self):
        self.write()
        self.assertEqual(await self.provider.get_text(str(self.wav)), "下午见。")
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call.url.host, "example.cn-beijing.maas.aliyuncs.com")
        self.assertEqual(call.url.path, "/api/v1/services/aigc/multimodal-generation/generation")
        self.assertEqual(call.headers["Authorization"], "Bearer example-credential")
        packet = json.loads(call.content)
        self.assertEqual(packet["model"], "qwen-audio-3.1-asr-flash")
        self.assertEqual(packet["parameters"], {"format": "wav"})
        content = packet["input"]["messages"]
        self.assertEqual(len(content), 1)
        self.assertEqual([p["type"] for p in content[0]["content"]], ["input_audio"])
        uri = content[0]["content"][0]["input_audio"]["data"]
        self.assertEqual(base64.b64decode(uri.split(",", 1)[1]), self.wav.read_bytes())

    async def test_long_recording_is_split_and_silence_is_free(self):
        self.write(silence=True)
        self.assertEqual(await self.provider.get_text(str(self.wav)), "")
        self.assertFalse(self.calls)
        self.write(seconds=301)
        self.assertEqual(await self.provider.get_text(str(self.wav)), "下午见。 下午见。")
        self.assertEqual(len(self.calls), 2)

    async def test_rejected_request_is_not_replayed_or_returned_as_speech(self):
        self.write()
        self.status, self.body = 403, {"message": "example-private-body"}
        with self.assertRaisesRegex(RuntimeError, "^Bailian ASR HTTP 403$"):
            await self.provider.get_text(str(self.wav))
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    sys.argv[1:] = [arg for arg in sys.argv[1:] if not Path(arg).is_dir()]
    unittest.main()
