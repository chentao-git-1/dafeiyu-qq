"""Local checks for the evening diary; no private conversation input."""
import datetime as dt

TZ = dt.timezone(dt.timedelta(hours=8))


class DiaryCancelled(ValueError):
    pass


class ImagePreparationFailed(RuntimeError):
    """The post write has not started, so one text-only fallback is safe."""


def window(day):
    date = dt.date.fromisoformat(day) if isinstance(day, str) else day
    return (dt.datetime.combine(date, dt.time(21, 30), TZ),
            dt.datetime.combine(date, dt.time(22, 30), TZ))


def diary_reason(text, plan):
    """Compatibility entry point; prose is no longer screened by word lists."""
    return ''
