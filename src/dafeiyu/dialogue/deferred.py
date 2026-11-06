"""Bounded, process-local work after delivery; never persists or retries jobs."""
import asyncio
from collections import deque
import contextlib
import logging


class DeferredLearning:
    def __init__(self, capacity=32):
        self.capacity = capacity
        self.jobs = deque()
        self.pending = set()
        self.completed = deque(maxlen=512)
        self.worker = None
        self.closed = False

    def submit(self, key, run):
        if self.closed or key in self.pending or key in self.completed or len(self.pending) >= self.capacity:
            return False
        self.pending.add(key)
        self.jobs.append((key, run))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())
        return True

    async def _run(self):
        try:
            while self.jobs:
                key, run = self.jobs.popleft()
                try:
                    await run()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logging.getLogger(__name__).warning('Deferred learning stopped: %s', type(error).__name__)
                finally:
                    self.pending.discard(key)
                    self.completed.append(key)
        finally:
            self.worker = None

    async def wait_idle(self):
        while self.worker is not None and not self.worker.done():
            await asyncio.shield(self.worker)

    async def close(self):
        self.closed = True
        if self.worker is not None:
            worker = self.worker
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self.jobs.clear()
        self.pending.clear()

    def status(self):
        return {'pending': len(self.pending), 'capacity': self.capacity, 'closed': self.closed}
