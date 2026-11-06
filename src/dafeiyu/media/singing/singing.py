"""Bounded client for MiMo's documented singing mode; no local speech model."""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import math
import time
import wave
from collections import OrderedDict
from contextlib import asynccontextmanager

import aiohttp

ENDPOINT = 'https://api.xiaomimimo.com/v1/chat/completions'
MODEL = 'mimo-v2.5-tts'
MAX_RESPONSE_BYTES = 12 * 1024 * 1024


class SingingError(Exception):
    """Contains a safe, user-facing explanation, never remote response bodies."""


def build_payload(lyrics: str, voice: str, style: str = '') -> dict:
    if voice not in ('冰糖', '茉莉'):
        raise SingingError('唱歌音色必须选择冰糖或茉莉。')
    instruction = '像一首真正的抒情民谣一样演唱：音符清晰、音高起伏明显，保持四四拍，句末唱出延长音，旋律连贯。人声清唱，不要用讲话或朗诵的语调。'
    if style.strip():
        instruction += '演唱风格参考：' + style.strip()[:120]
    return {
        'model': MODEL,
        'messages': [
            {'role': 'user', 'content': instruction},
            {'role': 'assistant', 'content': '(唱歌)' + lyrics},
        ],
        'audio': {'format': 'wav', 'voice': voice},
        'stream': False,
    }


def decode_wav(encoded: str) -> tuple[bytes, float]:
    if not isinstance(encoded, str) or len(encoded) > MAX_RESPONSE_BYTES:
        raise SingingError('唱歌接口返回的音频大小异常。')
    try:
        audio = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(audio), 'rb') as source:
            duration = source.getnframes() / source.getframerate()
            if (not math.isfinite(duration) or not 0.1 <= duration <= 180
                    or source.getnchannels() not in (1, 2)
                    or source.getsampwidth() not in (1, 2, 3, 4)):
                raise ValueError('unsupported audio dimensions')
    except (ValueError, TypeError, wave.Error, EOFError, ZeroDivisionError, binascii.Error):
        raise SingingError('唱歌接口没有返回有效的 WAV 音频。') from None
    return audio, duration


async def synthesize(key: str, lyrics: str, voice: str, style: str = '', timeout: int = 75) -> tuple[bytes, float]:
    payload = build_payload(lyrics, voice, style)
    limits = aiohttp.ClientTimeout(total=max(15, min(timeout, 120)), connect=10)
    try:
        async with aiohttp.ClientSession(timeout=limits, trust_env=False) as session:
            async with session.post(ENDPOINT, json=payload,
                                    headers={'Authorization': 'Bearer ' + key},
                                    allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise SingingError('MiMo 唱歌密钥无效或没有模型权限，请管理员检查。')
                if response.status == 429:
                    raise SingingError('唱歌服务繁忙，稍后再试。')
                if response.status != 200:
                    raise SingingError('唱歌接口暂时失败（HTTP {}）。'.format(response.status))
                if response.content_length and response.content_length > MAX_RESPONSE_BYTES:
                    raise SingingError('唱歌返回内容过大，这次停止接收。')
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise SingingError('唱歌返回内容过大，这次停止接收。')
                import json
                try:
                    result = json.loads(body)
                    encoded = result['choices'][0]['message']['audio']['data']
                except (ValueError, KeyError, TypeError, IndexError):
                    raise SingingError('唱歌接口没有返回音频，请管理员检查 MiMo 模型权限。') from None
                return decode_wav(encoded)
    except asyncio.TimeoutError:
        raise SingingError('这次唱歌生成超时了，稍后再试。') from None
    except aiohttp.ClientError:
        raise SingingError('暂时连不上唱歌服务，稍后再试。') from None


class SingingGate:
    """One in-flight generation globally; bounded per-session cooldown state."""
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last = OrderedDict()

    @asynccontextmanager
    async def enter(self, session_id: str, cooldown: int):
        if self.lock.locked():
            raise SingingError('正在唱另一段，等唱完再来。')
        now = time.monotonic()
        remaining = max(0, cooldown) - (now - self.last.get(session_id, -1e12))
        if remaining > 0:
            raise SingingError('唱歌冷却中，约 {} 秒后再试。'.format(math.ceil(remaining)))
        async with self.lock:
            self.last[session_id] = now
            self.last.move_to_end(session_id)
            while len(self.last) > 128:
                self.last.popitem(last=False)
            yield
