"""Recover the existing FishWorld MCP transport from the poller's idle ticks."""
import asyncio
import time
from contextlib import suppress

from astrbot.api import logger


def _registered_client(context):
    manager = getattr(context, "get_llm_tool_manager", None)
    runtimes = getattr(manager(), "mcp_server_runtime_view", {}) if manager else {}
    runtime = runtimes.get("fish-world")
    if runtime is None or runtime.shutdown_event.is_set() or runtime.lifecycle_task.done():
        return None
    client = runtime.client
    # A runtime is published before its first handshake completes. Only repair
    # clients that previously registered tools and are still enabled.
    if client.active and client.tools and callable(getattr(client, "_reconnect", None)):
        return client
    return None


def _connected(client):
    owner = client._connection_task
    # initialize() can fail after assigning session; its owner has then exited.
    return client.session is not None and owner is not None and not owner.done()


class FishWorldMCPRecovery:
    def __init__(self):
        self.client = None
        self.binding = None
        self.task = None
        self.closed = False
        self.failures = 0
        self.retry_at = 0.0
        self.failed_session = None

    def _ready(self, client):
        return _connected(client) and client.session is not self.failed_session

    def tick(self, context, binding):
        if self.closed:
            return
        client = _registered_client(context) if binding else None
        if client is not self.client or binding != self.binding:
            if self.task is not None and not self.task.done():
                self.task.cancel()
            self.client, self.binding = client, binding
            self.failures = 0
            self.retry_at = 0.0
            self.failed_session = None
        if client is None or (self.task is not None and not self.task.done()):
            return
        if client._reconnecting or client._reconnect_lock.locked():
            return
        if self._ready(client):
            self.failures = 0
            self.retry_at = 0.0
            self.failed_session = None
            return
        if time.monotonic() >= self.retry_at:
            self.task = asyncio.create_task(self._recover(context, client), name="fishworld-mcp-reconnect")

    async def _recover(self, context, client):
        # Recheck after scheduling, before entering the framework's own lock:
        # another caller may already have repaired or replaced the runtime.
        if (self.closed or _registered_client(context) is not client or client._reconnecting
                or client._reconnect_lock.locked()
                or self._ready(client)):
            return
        try:
            async with asyncio.timeout(30):
                await client._reconnect()
            if not _connected(client):
                raise ConnectionError("MCP handshake did not establish a live session")
        except Exception as error:
            # list_tools can fail after initialize succeeds. Keep retrying this
            # session, but yield to a new healthy one restored by AstrBot itself.
            self.failed_session = client.session
            self.failures += 1
            delay = min(30 * 2 ** min(self.failures - 1, 4), 300)
            self.retry_at = time.monotonic() + delay
            logger.warning("[鱼世界] MCP 重新连接失败（%s），%d 秒后再试", type(error).__name__, delay)
        else:
            self.failed_session = None
            self.failures = 0
            self.retry_at = 0.0
            logger.info("[鱼世界] MCP 工具连接已恢复")

    async def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
