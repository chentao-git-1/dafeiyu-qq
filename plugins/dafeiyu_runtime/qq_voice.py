"""Resolve NapCat voice IDs lazily when the media lives in a different container."""
import asyncio
import os

from astrbot.core.message.components import Plain, Record, Reply
from dafeiyu.media.voice.transcription import EMOTIONS, VOICE_TONES, VoiceInputPath, VoiceTone
from dafeiyu.memory.state import OWNER
if hasattr(Record, "model_fields"):
    from pydantic import PrivateAttr
else:
    from pydantic.v1 import PrivateAttr


class NapCatRecord(Record):
    _bot = PrivateAttr()
    _self_id: str = PrivateAttr()
    _media_id: str | None = PrivateAttr(default=None)
    _tone: VoiceTone | None = PrivateAttr(default=None)
    _failed: bool = PrivateAttr(default=False)

    async def convert_to_file_path(self):
        path = await self._resolve_qq_path()
        return VoiceInputPath(path, self._tone) if self._tone is not None else str(path)

    async def _resolve_qq_path(self):
        if self._failed:
            raise ValueError("QQ voice unavailable")
        try:
            path = await super().convert_to_file_path()
            if os.path.isfile(path):
                return path
        except Exception:
            # NapCat can report its private filesystem path in url/path when
            # the QQ download URL is unavailable. Only NapCat can read it.
            pass
        if not self._media_id:
            self._failed = True
            raise ValueError("QQ voice unavailable")
        try:
            data = await asyncio.wait_for(
                self._bot.call_action("get_record", self_id=self._self_id,
                                      file=self._media_id, out_format="wav"), timeout=20,
            )
            encoded = data.get("base64") if isinstance(data, dict) else None
            if not isinstance(encoded, str) or not encoded or len(encoded) > 45_000_000:
                raise ValueError("Missing or oversized QQ voice")
            self.file = await Record.fromBase64(encoded).convert_to_file_path()
            self.path = self.file
            return self.file
        except Exception:
            self._failed = True
            raise ValueError("QQ voice unavailable") from None


def prepare_voice_event(context, event):
    """Use the normal admission policy before any group voice is downloaded."""
    parts = event.get_messages()
    chains = [parts, *(part.chain for part in parts if isinstance(part, Reply) and part.chain)]
    if not any(isinstance(part, Record) for chain in chains for part in chain):
        return True
    if not context.get_config(event.unified_msg_origin).get("provider_stt_settings", {}).get("enable"):
        return True
    if not event.is_private_chat():
        plugin = context.get_registered_star("dafeiyu_mention_guard")
        guard = getattr(plugin, "star_cls", None)
        if getattr(plugin, "activated", False) and guard and guard.applies(event):
            if not guard.allowed(event) or not guard.explicitly_mentioned(event):
                guard.suppress(event)
                return False
    quoted = [(chain, idx) for chain in chains[1:] for idx, part in enumerate(chain) if isinstance(part, Record)]
    if quoted:
        event.set_extra("dafeiyu_quoted_voice_parts", quoted)
    if all(isinstance(part, (Plain, Record, Reply)) for part in parts) and any(isinstance(part, Plain) for part in parts):
        direct = [idx for idx, part in enumerate(parts) if isinstance(part, Record)]
        if direct:
            event.set_extra("dafeiyu_direct_voice_indices", direct)
    owner = str(event.get_sender_id()) == str(OWNER)
    sources = []
    for chain in chains:
        for idx, part in enumerate(chain):
            if type(part) is not Record:
                continue
            media_id = part.file or ""
            if (len(media_id) > 4096 or os.path.isabs(media_id) or
                    media_id.startswith(("file:", "http:", "https:", "base64:", "data:"))):
                media_id = ""
            direct_owner_clip = owner and chain is parts
            if not media_id and not direct_owner_clip:
                continue
            wrapped = NapCatRecord(file=part.file, url=part.url, path=part.path, text=part.text)
            wrapped._bot = event.bot
            wrapped._self_id = str(event.get_self_id())
            wrapped._media_id = media_id
            if direct_owner_clip:
                wrapped._tone = VoiceTone()
                sources.append((idx, wrapped._tone))
            chain[idx] = wrapped
    if sources:
        event.set_extra("dafeiyu_direct_voice_sources", sources)
    return True


def finish_voice_text(event):
    sources = event.get_extra("dafeiyu_direct_voice_sources", ())
    if sources:
        parts = event.get_messages()
        cues = tuple((1, number, tone.emotion) for number, (idx, tone) in enumerate(sources, 1)
                     if idx < len(parts) and isinstance(parts[idx], Plain) and tone.emotion in EMOTIONS)
        if cues:
            event.set_extra(VOICE_TONES, cues)
        event.set_extra("dafeiyu_direct_voice_sources", ())
    # AstrBot also appends transcripts from Reply.chain to the sender's message_str.
    # Keep those transcripts available on the quote without attributing them to the sender.
    quoted = event.get_extra("dafeiyu_quoted_voice_parts", ())
    suffix = "".join(chain[idx].text for chain, idx in quoted if idx < len(chain) and isinstance(chain[idx], Plain))
    if suffix:
        for message in (event, event.message_obj):
            if message.message_str.endswith(suffix):
                message.message_str = message.message_str[:-len(suffix)]
    if quoted:
        event.set_extra("dafeiyu_quoted_voice_parts", ())
    indices = event.get_extra("dafeiyu_direct_voice_indices", ())
    if indices:
        parts = event.get_messages()
        text = [(idx, p.text) for idx, p in enumerate(parts) if isinstance(p, Plain)]
        # Native preprocessing appends all transcripts after the typed text.
        # Replace exactly that representation, preserving edits made by other handlers.
        appended = "".join(t for idx, t in text if idx not in indices) + "".join(t for idx, t in text if idx in indices)
        ordered = "".join(t for _, t in text)
        for message in (event, event.message_obj):
            if message.message_str == appended:
                message.message_str = ordered
        event.set_extra("dafeiyu_direct_voice_indices", ())


def merge_voice_tones(latest, events):
    """Keep a cue tied to its original message when consecutive sends become one turn."""
    cues = tuple((message_no, clip_no, label) for message_no, event in enumerate(events, 1)
                 for _, clip_no, label in event.get_extra(VOICE_TONES, ()))
    if cues:
        latest.set_extra(VOICE_TONES, cues)
