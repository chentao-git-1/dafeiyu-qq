"""Aliyun Model Studio short-recording ASR for AstrBot's speech-to-text stage."""
import asyncio
import base64
import io
import re
import wave

import httpx
from astrbot.core import astrbot_config
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import STTProvider
from astrbot.core.provider.register import provider_cls_map, register_provider_adapter
from astrbot.core.utils.media_utils import MediaResolver
from dafeiyu.media.voice.transcription import EMOTIONS, VoiceInputPath

ADAPTER = "dafeiyu_bailian_asr"
MODEL = "qwen3-asr-flash"
LEGACY_MODEL = "qwen-audio-3.1-asr-flash"
MAX_SECONDS = 300
MAX_PCM_BYTES = 7 * 1024 * 1024  # The full base64 data URI must fit within 10 MB.


def audio_chunk(reader, frames):
    pcm = reader.readframes(frames)
    if not pcm:
        return None
    silence = b"\x80" if reader.getsampwidth() == 1 else b"\0"
    if not pcm.strip(silence):
        return ""
    data = io.BytesIO()
    with wave.open(data, "wb") as out:
        out.setnchannels(reader.getnchannels())
        out.setsampwidth(reader.getsampwidth())
        out.setframerate(reader.getframerate())
        out.writeframes(pcm)
    return "data:audio/wav;base64," + base64.b64encode(data.getvalue()).decode("ascii")


def response_parts(payload, legacy):
    try:
        if legacy:
            message, text = {}, payload["output"]["text"]
        else:
            message = payload["choices"][0]["message"]
            text = message["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Bailian ASR returned no transcription field") from None
    if not isinstance(text, str):
        raise RuntimeError("Invalid Bailian ASR transcription")
    annotations = message.get("annotations")
    if not isinstance(annotations, list):
        return text.strip(), None
    labels = [row.get("emotion") for row in annotations
              if isinstance(row, dict) and row.get("type") == "audio_info"]
    first = labels[0] if labels else None
    if isinstance(first, str) and first in EMOTIONS and all(label == first for label in labels):
        return text.strip(), first
    return text.strip(), None


class BailianASR(STTProvider):
    def __init__(self, provider_config, provider_settings):
        super().__init__(provider_config, provider_settings)
        self.set_model(provider_config.get("model", MODEL))
        if self.get_model() not in (MODEL, LEGACY_MODEL):
            raise ValueError("Unsupported Bailian ASR model")
        self.client = httpx.AsyncClient(timeout=float(provider_config.get("timeout", 45)), follow_redirects=False)

    def credentials(self):
        source = self.provider_config
        shared_id = source.get("credential_provider_id")
        if shared_id:
            source = next((p for p in astrbot_config.get("provider", [])
                           if p.get("id") == shared_id and p.get("type") == "dafeiyu_bailian_tts"), {})
        workspace = str(source.get("workspace_id") or "")
        key = source.get("api_key")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", workspace) or not isinstance(key, str) or not key.strip():
            raise ValueError("Missing Bailian Beijing workspace or API key")
        return workspace, key.strip()

    async def get_text(self, audio_url):
        tone = audio_url.tone if isinstance(audio_url, VoiceInputPath) else None
        if tone is not None:
            tone.emotion = None
        workspace, key = self.credentials()
        legacy = self.get_model() == LEGACY_MODEL
        route = "/api/v1/services/aigc/multimodal-generation/generation" if legacy else "/compatible-mode/v1/chat/completions"
        endpoint = f"https://{workspace}.cn-beijing.maas.aliyuncs.com{route}"
        texts, emotions = [], []
        async with MediaResolver(audio_url, media_type="audio", default_suffix=".wav").as_path(target_format="wav") as audio:
            with wave.open(str(audio.path), "rb") as reader:
                # Cap both duration and encoded size for high-rate or multi-channel WAVs.
                frames = min(reader.getframerate() * MAX_SECONDS,
                             MAX_PCM_BYTES // (reader.getnchannels() * reader.getsampwidth()))
                while True:
                    uri = await asyncio.to_thread(audio_chunk, reader, frames)
                    if uri is None:
                        break
                    if not uri:
                        continue
                    messages = [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": uri}}]}]
                    body = {"model": self.get_model(), "messages": messages, "stream": False, "asr_options": {"enable_itn": True}}
                    if legacy:
                        body = {"model": self.get_model(), "parameters": {"format": "wav"}, "input": {"messages": messages}}
                    try:
                        reply = await self.client.post(endpoint, json=body, headers={
                            "Authorization": "Bearer " + key, "X-DashScope-SSE": "disable",
                        })
                    except httpx.HTTPError as error:
                        raise RuntimeError("Bailian ASR connection failed: " + type(error).__name__) from None
                    if reply.status_code != 200:
                        raise RuntimeError(f"Bailian ASR HTTP {reply.status_code}")
                    try:
                        text, emotion = response_parts(reply.json(), legacy)
                    except ValueError:
                        raise RuntimeError("Invalid Bailian ASR response") from None
                    if text:
                        texts.append(text)
                        emotions.append(emotion)
        # A long clip only gets a cue if all of its transcribed chunks agree.
        if tone is not None and emotions and all(label == emotions[0] for label in emotions):
            tone.emotion = emotions[0]
        return " ".join(texts)

    async def terminate(self):
        await self.client.aclose()


def register_asr():
    if ADAPTER in provider_cls_map:
        provider_cls_map[ADAPTER].cls_type = BailianASR
        return
    register_provider_adapter(
        ADAPTER, "阿里云百炼（北京）语音识别", provider_type=ProviderType.SPEECH_TO_TEXT,
        default_config_tmpl={"provider_type": "speech_to_text", "model": MODEL, "timeout": 45,
                             "credential_provider_id": "dafeiyu-voice", "workspace_id": "", "api_key": ""},
    )(BailianASR)
