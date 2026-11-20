"""QQ voice input reaches the native preprocessor and the normal dialogue text."""
import base64
import asyncio
import copy
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
from dafeiyu_runtime import qq_voice
from dafeiyu.media.voice.transcription import VOICE_TONES, VoiceInputPath, VoiceTone
from dafeiyu.runtime.flow import ChatFlow
from dafeiyu_natural_chat.main import NaturalChat
from dafeiyu_runtime.main import DafeiyuRuntime
from dafeiyu_runtime.qq_voice import finish_voice_text, prepare_voice_event


def wave_file():
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 4000)
    return data.getvalue()


def cloud_message(text, emotion=None):
    message = {"content": text}
    if emotion is not None:
        message["annotations"] = [{"type": "audio_info", "emotion": emotion}]
    return {"choices": [{"message": message}]}


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
        self.body = cloud_message("下午见。", "happy")
        self.responses = []

        async def respond(request):
            self.calls.append(request)
            return httpx.Response(self.status, json=self.responses.pop(0) if self.responses else self.body)

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
        self.assertEqual(call.url.path, "/compatible-mode/v1/chat/completions")
        self.assertEqual(call.headers["Authorization"], "Bearer example-credential")
        packet = json.loads(call.content)
        self.assertEqual(packet["model"], "qwen3-asr-flash")
        self.assertFalse(packet["stream"])
        content = packet["messages"]
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

    async def test_legacy_provider_remains_usable_for_existing_configurations(self):
        self.provider.set_model("qwen-audio-3.1-asr-flash")
        self.body = {"output": {"text": "下午见。"}}
        self.write()
        slot = VoiceTone()
        self.assertEqual(await self.provider.get_text(VoiceInputPath(self.wav, slot)), "下午见。")
        self.assertIsNone(slot.emotion)
        self.assertEqual(self.calls[0].url.path, "/api/v1/services/aigc/multimodal-generation/generation")

    async def test_uncertain_or_missing_cue_keeps_the_words_and_clears_earlier_cue(self):
        self.write()
        slot = VoiceTone()
        path = VoiceInputPath(self.wav, slot)
        for label in ("happy", None, "unrecognized", {"not": "a label"}):
            self.body = cloud_message("下午见。", label)
            self.assertEqual(await self.provider.get_text(path), "下午见。")
            self.assertEqual(slot.emotion, "happy" if label == "happy" else None)
        self.write(seconds=301)
        self.responses.extend([cloud_message("第一段", "sad"), cloud_message("第二段", "happy")])
        self.assertEqual(await self.provider.get_text(path), "第一段 第二段")
        self.assertIsNone(slot.emotion)

    async def test_duplicate_text_in_quote_does_not_change_owner_tone_or_transcript(self):
        self.write()
        encoded = base64.b64encode(self.wav.read_bytes()).decode()
        ev = await qq_event([{"type": "record", "data": {"file": "base64://" + encoded}}])
        ev.message_obj.message.insert(0, Reply(id="702", sender_id=12, chain=[Record.fromBase64(encoded)]))
        self.responses.extend([cloud_message("我没事。", "sad"), cloud_message("我没事。", "angry")])
        ctx, stage = environment(self.provider)
        try:
            with patch.object(qq_voice, "OWNER", str(ev.get_sender_id())):
                self.assertTrue(prepare_voice_event(ctx, ev))
            await stage.process(ev)
            finish_voice_text(ev)
            self.assertEqual(ev.get_extra(VOICE_TONES), ((1, 1, "sad"),))
            self.assertEqual(NaturalChat.input_snapshot(SimpleNamespace(), ev)["text"], "我没事。")
            self.assertEqual(ev.message_obj.message[0].chain[0].text, "我没事。")
            wire, stored = await chat_payload(ev)
            self.assertIn("难过", wire)
            self.assertNotIn("生气", wire)
            self.assertNotIn("语音语气线索", stored)
            self.assertEqual(len(self.calls), 2)
        finally:
            ev.cleanup_temporary_local_files()

    async def test_other_sender_transcribes_without_an_emotion_hint(self):
        self.write()
        encoded = base64.b64encode(self.wav.read_bytes()).decode()
        ev = await qq_event([{"type": "record", "data": {"file": "base64://" + encoded}}])
        ctx, stage = environment(self.provider)
        try:
            with patch.object(qq_voice, "OWNER", "another-account"):
                self.assertTrue(prepare_voice_event(ctx, ev))
            await stage.process(ev)
            finish_voice_text(ev)
            self.assertEqual(ev.get_message_str(), "下午见。")
            self.assertFalse(ev.get_extra(VOICE_TONES))
            wire, _ = await chat_payload(ev)
            self.assertNotIn("语音语气线索", wire)
        finally:
            ev.cleanup_temporary_local_files()

    async def test_concurrent_recordings_with_the_same_path_keep_separate_receipts(self):
        self.write()
        a, b = VoiceTone(), VoiceTone()
        async def respond(request):
            label = asyncio.current_task().get_name()
            await asyncio.sleep(0)
            return httpx.Response(200, json=cloud_message("一样的词", label))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(self.provider, "client", client):
                jobs = [asyncio.create_task(self.provider.get_text(VoiceInputPath(self.wav, slot)), name=label)
                        for slot, label in ((a, "sad"), (b, "happy"))]
                self.assertEqual(await asyncio.gather(*jobs), ["一样的词", "一样的词"])
        self.assertEqual((a.emotion, b.emotion), ("sad", "happy"))


async def chat_payload(event):
    """Use the native chat runner and capture the HTTP payload before sending."""
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.provider.entities import LLMResponse
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from dafeiyu.dialogue.generation import request_contract
    from dafeiyu_dialogue.provider_contract import ProviderContracts, arm_contract

    provider = ProviderOpenAIOfficial({"id": "fixture", "type": "openai_chat_completion",
        "model": "fixture", "key": ["fixture"], "modalities": ["text", "tool_use"]}, {})
    contracts = ProviderContracts()
    nonce = arm_contract(True)
    req = event.request_llm(prompt=event.get_message_str(), system_prompt=request_contract("fixture", {}, False, nonce))
    req.contexts = [{"role": "user", "content": "昨天的文字消息"}, {"role": "assistant", "content": "嗯。"}]
    original = copy.deepcopy(req.contexts)
    runtime = SimpleNamespace(allowed=lambda ev: True, replies=SimpleNamespace(expect=lambda ev: None))
    snapshots = []
    async def query(payload, *args, **kwargs):
        snapshots.append(copy.deepcopy(payload["messages"]))
        return LLMResponse(role="assistant", completion_text='{"reply":"嗯。"}')
    provider._query = AsyncMock(side_effect=query)
    try:
        await DafeiyuRuntime.reply_requested(runtime, event, req)
        contracts.install(provider)
        contracts.bind_request(req, nonce)
        runner = ToolLoopAgentRunner()
        await runner.reset(provider, req, ContextWrapper(context=None), None, BaseAgentRunHooks())
        _ = [part async for part in runner._iter_llm_responses()]
        assert req.contexts == original and req.prompt == event.get_message_str()
        assert len(snapshots) == 1
        wire = snapshots[0]
        last = max(i for i, m in enumerate(wire) if m["role"] == "user")
        assert all("语音语气线索" not in str(m["content"]) for i, m in enumerate(wire) if i != last)
        return str(wire[last]["content"]), str([m.content for m in runner.run_context.messages])
    finally:
        contracts.close()
        arm_contract(False)
        await provider.terminate()


class MergedVoiceTurns(unittest.IsolatedAsyncioTestCase):
    async def test_hints_follow_only_the_original_clip_in_a_burst(self):
        flow = ChatFlow(quiet=.03, maximum=.2, on_merge=qq_voice.merge_voice_tones)
        events = [await qq_event([{"type":"text","data":{"text":s}}]) for s in ("第一条", "第二条", "第三条")]
        events[0].set_extra(VOICE_TONES, ((1,1,"happy"),))
        events[2].set_extra(VOICE_TONES, ((1,2,"sad"),))
        results = await asyncio.gather(*(flow.collect("same-sender", ev, ev.message_str) for ev in events))
        self.assertEqual(results, [None, None, "第一条\n第二条\n第三条"])
        self.assertEqual(events[2].get_extra(VOICE_TONES), ((1,1,"happy"),(3,2,"sad")))
        later = await qq_event([{"type":"text","data":{"text":"新一轮"}}])
        await flow.collect("same-sender", later, later.message_str)
        self.assertFalse(later.get_extra(VOICE_TONES))


if __name__ == "__main__":
    sys.argv[1:] = [arg for arg in sys.argv[1:] if not Path(arg).is_dir()]
    unittest.main()
