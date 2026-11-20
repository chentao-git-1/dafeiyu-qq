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

ADAPTER = "dafeiyu_bailian_asr"
MODEL = "qwen-audio-3.1-asr-flash"
MAX_SECONDS = 300


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


class BailianASR(STTProvider):
    def __init__(self, provider_config, provider_settings):
        super().__init__(provider_config, provider_settings)
        self.set_model(provider_config.get("model", MODEL))
        if self.get_model() != MODEL:
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
        workspace, key = self.credentials()
        endpoint = f"https://{workspace}.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
        texts = []
        async with MediaResolver(audio_url, media_type="audio", default_suffix=".wav").as_path(target_format="wav") as audio:
            with wave.open(str(audio.path), "rb") as reader:
                # Keep individual requests within the provider's five-minute duration limit
                # and keep high-rate or multi-channel WAV files within a modest upload size.
                frames = min(reader.getframerate() * MAX_SECONDS,
                             8 * 1024 * 1024 // (reader.getnchannels() * reader.getsampwidth()))
                while True:
                    uri = await asyncio.to_thread(audio_chunk, reader, frames)
                    if uri is None:
                        break
                    if not uri:
                        continue
                    body = {"model": self.get_model(), "parameters": {"format": "wav"},
                            "input": {"messages": [{"role": "user", "content": [
                                {"type": "input_audio", "input_audio": {"data": uri}}
                            ]}]}}
                    try:
                        reply = await self.client.post(endpoint, json=body, headers={
                            "Authorization": "Bearer " + key, "X-DashScope-SSE": "disable",
                        })
                    except httpx.HTTPError as error:
                        raise RuntimeError("Bailian ASR connection failed: " + type(error).__name__) from None
                    if reply.status_code != 200:
                        raise RuntimeError(f"Bailian ASR HTTP {reply.status_code}")
                    try:
                        text = reply.json()["output"]["text"]
                    except (ValueError, KeyError, TypeError):
                        raise RuntimeError("Bailian ASR returned no transcription field") from None
                    if not isinstance(text, str):
                        raise RuntimeError("Invalid Bailian ASR transcription")
                    if text.strip():
                        texts.append(text.strip())
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
