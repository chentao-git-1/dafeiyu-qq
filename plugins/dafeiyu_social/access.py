"""Shared read access for notification producers and the existing voice plugin."""
from functools import lru_cache
from pathlib import Path
import time

from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .store import Store, can_notify_profile
from .activity import ActivityStore


@lru_cache(maxsize=4)
def database(path):
    return Store(Path(path) / 'plugin_data/dafeiyu_social/preferences.db')


def store():
    return database(str(get_astrbot_data_path()))


def profile(uid):
    return store().profile(str(uid))


def can_notify(uid, timestamp=None):
    return can_notify_profile(profile(uid), time.time() if timestamp is None else timestamp)


def activity():
    return ActivityStore(store().path)


def record_delivery(event):
    source = event.get_extra('dafeiyu_activity_input', None)
    return activity().record_confirmed(source, store().bots())
