"""Turn-local acoustic cues, kept separate from the speaker's words."""
from dataclasses import dataclass


EMOTIONS = {
    "happy": "愉快", "sad": "难过", "angry": "生气", "neutral": "平静",
    "surprised": "惊讶", "disgusted": "厌恶", "fearful": "害怕",
}
VOICE_TONES = "dafeiyu_current_voice_tones"


@dataclass
class VoiceTone:
    emotion: str | None = None


class VoiceInputPath(str):
    """A normal media path with a slot owned by this particular direct QQ recording.

    AstrBot passes this path to get_text(), and expects only a plain transcript back.
    Two messages referring to the same file still have separate slots.
    """

    def __new__(cls, path, tone):
        value = super().__new__(cls, path)
        value.tone = tone
        return value

    def __copy__(self):
        return str(self)

    def __deepcopy__(self, memo):
        return str(self)
