"""Resolve the configured primary provider for a single authorized notice."""
import asyncio
from dafeiyu.dialogue.notifications import compose_notification


async def notification_text(context, session, kind, facts, allow_model=True):
    provider = None
    lookup = getattr(context, 'get_using_provider_async', None)
    if allow_model and callable(lookup):
        try:
            provider = await asyncio.wait_for(lookup(session), timeout=3)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
    return await compose_notification(provider, kind, facts)
