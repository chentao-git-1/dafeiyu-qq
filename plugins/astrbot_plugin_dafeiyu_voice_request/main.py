"""Honor per-message speech requests without changing native TTS probability."""
import asyncio
import sys

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.star.session_llm_manager import SessionServiceManager

from .intent import output_intent



def social_profile(uid):
    try:
        from data.plugins.dafeiyu_social.access import profile
        return profile(str(uid))
    except ImportError:
        return {'voice':'auto','proactive':True}

def social_can_notify(uid):
    try:
        from data.plugins.dafeiyu_social.access import can_notify
        return can_notify(str(uid))
    except ImportError:
        return True


class DafeiyuVoiceRequest(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    def _intent(self, event):
        if event.get_platform_name() != 'aiocqhttp':
            return None
        config = self.context.get_config(event.unified_msg_origin)
        settings = config['platform_settings']
        allowed = {str(value) for value in settings.get('id_whitelist', [])}
        group = str(event.get_group_id() or '')
        if settings.get('enable_id_white_list', False):
            if group and group not in allowed:
                return None
            if not group and str(event.get_sender_id()) not in allowed and event.unified_msg_origin not in allowed:
                return None
        text = ''.join(part.text for part in event.get_messages() if isinstance(part, Plain))
        explicit = output_intent(text)
        if explicit:
            return explicit
        preferred = social_profile(event.get_sender_id())['voice']
        return preferred if preferred in ('text', 'voice') else None

    @filter.on_llm_request(priority=-1000000)
    async def explain_delivery(self, event: AstrMessageEvent, req: ProviderRequest):
        if event.get_extra('dafeiyu_cross_group_voice', False):
            return
        if self._intent(event) != 'voice':
            return
        config = self.context.get_config(event.unified_msg_origin)
        if not config['provider_tts_settings'].get('enable'):
            return
        req.system_prompt = (req.system_prompt or '') + (
            '\n本轮按用户要求或本人保存的偏好，系统会将你的回复自动合成为语音。'
            '直接按原人格用口语回答，或说出对方指定朗读的内容；'
            '不要声称不能发语音，不要输出语音占位符，不要解释生成过程。'
        )

    # Existing mention and meme decorators must finish before conversion.
    @filter.on_decorating_result(priority=-sys.maxsize - 1)
    async def honor_delivery(self, event: AstrMessageEvent):
        if event.get_extra('dafeiyu_cross_group_voice', False):
            result = event.get_result()
            if result and result.is_llm_result():
                result.result_content_type = ResultContentType.GENERAL_RESULT
            return
        intent = self._intent(event)
        result = event.get_result()
        if not intent or not result or not result.is_llm_result():
            return
        config = self.context.get_config(event.unified_msg_origin)
        settings = config['provider_tts_settings']
        if not settings.get('enable'):
            return
        if not await SessionServiceManager.should_process_tts_request(event):
            return
        # A custom result bypasses native random TTS and prevents retry/doubling.
        result.result_content_type = ResultContentType.GENERAL_RESULT
        if intent == 'text' or any(isinstance(part, Record) for part in result.chain):
            return
        text = ' '.join(part.text.strip() for part in result.chain if isinstance(part, Plain) and part.text.strip())
        if not text:
            return
        try:
            provider = await self.context.get_using_tts_provider_async(event.unified_msg_origin)
            if provider is None:
                raise RuntimeError('TTS provider unavailable')
            path = await asyncio.wait_for(provider.get_audio(text), timeout=45)
            if not path:
                raise RuntimeError('TTS returned no audio')
            record = Record(file=path, url=path, text=text)
            chain = []
            inserted = False
            for part in result.chain:
                if isinstance(part, Plain):
                    if not inserted:
                        chain.append(record)
                        inserted = True
                    if settings.get('dual_output'):
                        chain.append(part)
                else:
                    chain.append(part)
            result.chain = chain
            event.set_extra('_dafeiyu_explicit_voice_sent', True)
            logger.info('Explicit voice reply synthesized: umo=%s', event.unified_msg_origin)
        except Exception as error:
            logger.warning('Explicit voice reply failed; keeping text: %s', type(error).__name__)
