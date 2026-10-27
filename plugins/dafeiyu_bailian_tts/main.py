"""Bailian Beijing HTTP speech adapter for the existing native TTS pipeline."""
import asyncio
import base64
import io
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse, urlunparse
import uuid
import wave
import sys

import httpx

from astrbot.api.event import filter
from astrbot.api.star import Star
from astrbot.core.message.components import Plain
from astrbot.api.web import json_response
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import TTSProvider
from astrbot.core.provider.register import provider_cls_map, register_provider_adapter
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path, get_astrbot_data_path

from .expression import SPEECH_CONTEXT, relationship, relationship_prompt, speech_plan

ADAPTER = 'dafeiyu_bailian_tts'
MODEL = 'qwen-audio-3.1-tts-flash'
VOICE = 'xuxiaoqiao_v3.1'
LIMIT = 12 * 1024 * 1024


class BailianSpeech(TTSProvider):
    def __init__(self, provider_config, provider_settings):
        super().__init__(provider_config, provider_settings)
        self.set_model(provider_config['model'])
        workspace = str(provider_config.get('workspace_id', ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', workspace):
            raise ValueError('Invalid Beijing workspace ID')
        if self.get_model() != MODEL or provider_config.get('voice') != VOICE:
            raise ValueError('Unsupported speech model or voice')
        self.endpoint = 'https://' + workspace + '.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer'
        self.last_result = {}

    async def get_audio(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('Speech text must contain 1 to 4000 characters')
        plan = speech_plan(text, SPEECH_CONTEXT.get()) if self.provider_config.get('expressive_style') else None
        instruction = self.provider_config['instruction'] + (plan['instruction'] if plan else '')
        try:
            async with asyncio.timeout(40):
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as client:
                    async with client.stream('POST', self.endpoint, headers={
                        'Authorization': 'Bearer ' + self.provider_config['api_key'],
                    }, json={'model': self.get_model(), 'input': {
                        'text': text, 'voice': self.provider_config['voice'],
                        'instruction': instruction,
                        'format': 'wav', 'sample_rate': 24000,
                    }}) as response:
                        if response.status_code != 200:
                            raise RuntimeError('Bailian speech HTTP ' + str(response.status_code))
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 16 * 1024 * 1024:
                                raise RuntimeError('Speech response exceeds size limit')
                    result = json.loads(body)
                    audio = result.get('output', {}).get('audio', {})
                    if audio.get('data'):
                        raw = base64.b64decode(audio['data'], validate=True)
                    else:
                        parsed = urlparse(str(audio.get('url') or ''))
                        if (parsed.scheme not in ('http', 'https')
                                or not (parsed.hostname or '').endswith('.aliyuncs.com')
                                or parsed.username or parsed.password
                                or parsed.port not in (None, 80, 443)):
                            raise RuntimeError('Unexpected speech audio address')
                        url = urlunparse(parsed._replace(scheme='https'))
                        raw = bytearray()
                        # Authentication belongs only to the synthesis request.
                        async with client.stream('GET', url, timeout=10) as response:
                            if response.status_code != 200:
                                raise RuntimeError('Speech audio download failed')
                            async for chunk in response.aiter_bytes():
                                raw.extend(chunk)
                                if len(raw) > LIMIT:
                                    raise RuntimeError('Speech audio exceeds size limit')
                if not 1000 < len(raw) <= LIMIT or raw[:4] != b'RIFF' or raw[8:12] != b'WAVE':
                    raise RuntimeError('Invalid speech WAV audio')
                with wave.open(io.BytesIO(raw), 'rb') as audio_file:
                    params = audio_file.getparams()
                    if params.nchannels not in (1, 2) or params.sampwidth != 2 or params.framerate not in (16000, 22050, 24000, 44100, 48000):
                        raise RuntimeError('Invalid speech WAV format')
                    # Streaming WAV headers may declare an unfinished data length.
                    frames = audio_file.readframes(LIMIT // (params.nchannels * params.sampwidth))
                    duration = len(frames) / (params.nchannels * params.sampwidth * params.framerate)
                    if not 0.1 < duration <= 180:
                        raise RuntimeError('Unexpected speech audio duration')
                normalized = io.BytesIO()
                with wave.open(normalized, 'wb') as audio_file:
                    audio_file.setparams(params)
                    audio_file.setnframes(len(frames) // (params.nchannels * params.sampwidth))
                    audio_file.writeframes(frames)
                raw = normalized.getvalue()
                directory = Path(get_astrbot_temp_path())
                directory.mkdir(parents=True, exist_ok=True)
                # Bound this adapter's cache without touching other providers' files.
                old_files = sorted(directory.glob('jingjing_bailian_*.wav'), key=lambda p: p.stat().st_mtime, reverse=True)
                for index, old in enumerate(old_files):
                    if index >= 31 or time.time() - old.stat().st_mtime > 86400:
                        old.unlink(missing_ok=True)
                path = directory / ('jingjing_bailian_' + uuid.uuid4().hex + '.wav')
                path.write_bytes(raw)
                path.chmod(0o600)
                self.last_result = {'ok': True, 'path': str(path), 'duration_seconds': round(duration, 2), 'at': int(time.time())}
                if plan:
                    self.last_result.update({'style': plan['mood'], 'relationship': plan['relationship']})
                return str(path)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            safe_error = str(error) if isinstance(error, RuntimeError) and str(error).startswith((
                'Bailian speech HTTP ', 'Speech response exceeds ', 'Unexpected speech audio ',
                'Speech audio download failed', 'Speech audio exceeds ', 'Invalid speech WAV ',
            )) else type(error).__name__
            self.last_result = {'ok': False, 'error_type': type(error).__name__, 'reason': safe_error, 'at': int(time.time())}
            # Never propagate response bodies, authorization headers or signed URLs.
            raise RuntimeError('Bailian speech failed: ' + safe_error) from None

    async def test(self):
        await self.get_audio('爸爸，鲸鲸的温柔可爱语音已经准备好啦。你想聊天的时候，叫我一声就好。')


if ADAPTER in provider_cls_map:
    provider_cls_map[ADAPTER].cls_type = BailianSpeech
else:
    register_provider_adapter(
        ADAPTER, 'Jingjing Bailian Beijing speech',
        provider_type=ProviderType.TEXT_TO_SPEECH,
        default_config_tmpl={
            'provider_type': 'text_to_speech', 'model': MODEL,
            'workspace_id': '', 'api_key': '', 'voice': VOICE,
            'instruction': '', 'timeout': 40,
            'expressive_style': False,
        },
        provider_display_name='鲸鲸百炼聊天语音',
    )(BailianSpeech)


class JingjingBailianTTS(Star):
    def __init__(self, context):
        super().__init__(context)
        self.relations_path = Path(get_astrbot_data_path()) / 'plugin_data/dafeiyu_bailian_tts/relations.json'
        self.relations = json.loads(self.relations_path.read_text(encoding='utf-8')) if self.relations_path.exists() else {'people': {}}
        context.register_web_api('/dafeiyu_bailian_tts/status', self.status, ['GET'], '百炼聊天语音状态')

    @staticmethod
    def real_sender(event):
        if event.get_platform_name() != 'aiocqhttp' or event.get_platform_id() != 'dafeiyu-qq':
            return None
        raw = getattr(event.message_obj, 'raw_message', None)
        if not hasattr(raw, 'get') or raw.get('post_type') != 'message' or str(raw.get('self_id')) != '7000000002':
            return None
        uid = str(event.get_sender_id())
        return uid if re.fullmatch(r'[1-9]\d{4,11}', uid) and str(raw.get('user_id')) == uid else None

    @filter.on_llm_request(priority=1000)
    async def relationship_context(self, event, req):
        SPEECH_CONTEXT.set(None)
        uid = self.real_sender(event)
        if uid is None:
            return
        record = self.relations['people'].setdefault(uid, {'turns': 0, 'days': [], 'recent_ids': []})
        message_id = str(getattr(event.message_obj, 'message_id', '') or '')
        if message_id:
            key = hashlib.sha256((event.unified_msg_origin + ':' + message_id).encode()).hexdigest()[:24]
            if key not in record['recent_ids']:
                day = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
                record['turns'] += 1
                record['days'] = sorted(set(record['days'] + [day]))[-31:]
                record['recent_ids'] = (record['recent_ids'] + [key])[-32:]
                record['last_seen'] = int(time.time())
                self.relations_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.relations_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(self.relations, ensure_ascii=False), encoding='utf-8')
                temporary.chmod(0o600)
                os.replace(temporary, self.relations_path)
        profile = relationship(uid, record)
        event.set_extra('jingjing_relationship_profile', profile)
        req.system_prompt = (req.system_prompt or '') + relationship_prompt(profile)

    @filter.on_decorating_result(priority=sys.maxsize - 20)
    async def expression_context(self, event):
        uid = self.real_sender(event)
        if uid is None:
            SPEECH_CONTEXT.set(None)
            return
        profile = event.get_extra('jingjing_relationship_profile') or relationship(uid, self.relations['people'].get(uid))
        human_text = ' '.join(part.text for part in event.get_messages() if isinstance(part, Plain))
        if event.get_extra('jingjing_voice_continuity') == 'gentle':
            human_text += ' 心情不好，语气仍轻一些。'
        SPEECH_CONTEXT.set({'profile': profile, 'human_text': human_text})

    async def status(self):
        provider = await self.context.get_using_tts_provider_async('dafeiyu-qq:FriendMessage:7000000001')
        active = provider is not None and provider.meta().type == ADAPTER
        return json_response({
            'active': active,
            'provider_id': provider.meta().id if provider else None,
            'model': provider.get_model() if active else None,
            'voice': provider.provider_config.get('voice') if active else None,
            'expressive_style': bool(provider.provider_config.get('expressive_style')) if active else False,
            'relationship_profiles': len(self.relations['people']),
            'last_result': provider.last_result if active else {},
        })
