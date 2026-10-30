"""Saved BreezeBlue voices in the existing AstrBot TTS pipeline."""
import array
import asyncio
import io
from pathlib import Path
import re
import time
import uuid
import wave

import httpx

from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import TTSProvider
from astrbot.core.provider.register import provider_cls_map, register_provider_adapter
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from .expression import SPEECH_CONTEXT, speech_plan

ADAPTER='dafeiyu_breeze_tts'
MODEL='breeze-tts-2'
API='https://api.breeze.blue/v1/text-to-speech/'
LIMIT=12*1024*1024
INSTRUCTION='保持已保存音色，用标准普通话自然、轻柔地说话，只说给定文本。'
MOODS={'chat':'自然放松，语速适中。','comfort':'安慰时轻柔平稳，语速稍慢。','happy':'开心时轻快明亮，带自然笑意。','explain':'解释时温和认真，吐字清楚。'}
MOODS['neutral']='平稳自然地朗读，不额外添加情绪或话语。'
RELATIONS={'dad':'对爸爸有自然亲近感。','family':'与家人熟络自然。','friend':'保持朋友分寸，不刻意撒娇。'}


class SpeechError(RuntimeError):pass


def normalize_wav(raw):
    if not 1000<len(raw)<=LIMIT or raw[:4]!=b'RIFF' or raw[8:12]!=b'WAVE':
        raise SpeechError('Invalid Breeze WAV audio')
    with wave.open(io.BytesIO(raw),'rb') as audio:
        if (audio.getnchannels(),audio.getsampwidth(),audio.getframerate())!=(1,2,24000):
            raise SpeechError('Unexpected Breeze audio format')
        frames=audio.readframes(LIMIT//2)
    if len(frames)%2:raise SpeechError('Incomplete Breeze audio frames')
    duration=len(frames)/48000
    samples=array.array('h',frames)
    if not 0.1<duration<=180 or not samples or sum(x*x for x in samples)/len(samples)<1:
        raise SpeechError('Empty or silent Breeze audio')
    output=io.BytesIO()
    with wave.open(output,'wb') as audio:
        audio.setparams((1,2,24000,0,'NONE','not compressed'));audio.writeframes(frames)
    return output.getvalue(),duration


class BreezeSpeech(TTSProvider):
    synthesis_timeout=85
    def __init__(self,provider_config,provider_settings):
        super().__init__(provider_config,provider_settings)
        self.set_model(provider_config.get('model',MODEL))
        voice=provider_config.get('voice','')
        if self.get_model()!=MODEL or not re.fullmatch(r'voc_[A-Za-z0-9_-]{6,96}',str(voice)):
            raise ValueError('A saved Breeze voice and breeze-tts-2 model are required')
        if not isinstance(provider_config.get('api_key'),str) or not provider_config['api_key'].strip():
            raise ValueError('Breeze API key is required')
        guidance=float(provider_config.get('guidance_scale',2))
        if not 1<=guidance<=8:raise ValueError('Invalid Breeze guidance scale')
        self.guidance=guidance;self.endpoint=API+voice+'/stream';self.last_result={}
        self.generation_lock=asyncio.Lock()

    async def get_audio(self,text):
        if not isinstance(text,str) or not text.strip() or len(text)>2000:
            raise ValueError('Speech text must contain 1 to 2000 characters')
        context=SPEECH_CONTEXT.get()
        plan=speech_plan(text,context) if context and self.provider_config.get('expressive_style',True) else None
        instructions=INSTRUCTION+(RELATIONS[plan['relationship']]+MOODS[plan['mood']] if plan else '')
        phase='connect';received=0;started=time.monotonic()
        try:
            async with asyncio.timeout(self.synthesis_timeout),self.generation_lock:
                # Retry TCP/TLS setup only, before any synthesis request is sent.
                transport=httpx.AsyncHTTPTransport(local_address='0.0.0.0',retries=2,trust_env=False)
                async with httpx.AsyncClient(trust_env=False,follow_redirects=False,transport=transport,
                        timeout=httpx.Timeout(75,connect=5,pool=5)) as client:
                    async with client.stream('POST',self.endpoint,
                            headers={'xi-api-key':self.provider_config['api_key'],'Content-Type':'application/json',
                                'Accept-Encoding':'identity','User-Agent':'BreezeLocalAudition/1.0'},
                            params={'output_format':'wav'},json={'text':text,'model_id':MODEL,'language_code':'zh',
                                'instructions':instructions,'voice_settings':{'guidance_scale':self.guidance}}) as response:
                        if response.status_code!=200:raise SpeechError('Breeze speech HTTP '+str(response.status_code))
                        phase='receive'
                        body=bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            received=len(body)
                            if len(body)>LIMIT:raise SpeechError('Breeze speech exceeds size limit')
                phase='validate';raw,duration=await asyncio.to_thread(normalize_wav,body)
                directory=Path(get_astrbot_temp_path());directory.mkdir(parents=True,exist_ok=True)
                old=sorted(directory.glob('jingjing_breeze_*.wav'),key=lambda p:p.stat().st_mtime,reverse=True)
                for index,path in enumerate(old):
                    if index>=31 or time.time()-path.stat().st_mtime>86400:path.unlink(missing_ok=True)
                path=directory/('jingjing_breeze_'+uuid.uuid4().hex+'.wav')
                path.write_bytes(raw);path.chmod(0o600)
                self.last_result={'ok':True,'path':str(path),'duration_seconds':round(duration,3),
                    'generation_seconds':round(time.monotonic()-started,3),'at':int(time.time())}
                if plan:self.last_result.update(style=plan['mood'],relationship=plan['relationship'])
                return str(path)
        except asyncio.CancelledError:raise
        except Exception as error:
            reason=str(error) if isinstance(error,SpeechError) else type(error).__name__
            self.last_result={'ok':False,'error_type':type(error).__name__,'reason':reason,'phase':phase,
                'received_bytes':received,'elapsed_seconds':round(time.monotonic()-started,3),'at':int(time.time())}
            raise RuntimeError('Breeze speech failed: '+reason+' during '+phase) from None

    async def test(self):
        await self.get_audio('你好，鲸鲸的新声音准备好啦。')


if ADAPTER in provider_cls_map:
    provider_cls_map[ADAPTER].cls_type=BreezeSpeech
else:
    register_provider_adapter(ADAPTER,'BreezeBlue saved voice speech',provider_type=ProviderType.TEXT_TO_SPEECH,
        default_config_tmpl={'provider_type':'text_to_speech','model':MODEL,'voice':'','api_key':'',
            'guidance_scale':2,'expressive_style':True},provider_display_name='鲸鲸 Breeze TTS 2')(BreezeSpeech)
