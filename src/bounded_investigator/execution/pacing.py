"""Share a completion-to-next-request cooldown across models using one service."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from time import monotonic
from weakref import WeakKeyDictionary

from pydantic_ai.models.wrapper import WrapperModel

_gates = WeakKeyDictionary()
log = logging.getLogger(__name__)


class _Gate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.finished = None

    @asynccontextmanager
    async def hold(self, interval):
        async with self.lock:
            remaining = 0 if self.finished is None else interval - (monotonic() - self.finished)
            if remaining > 0:
                log.info('model_request cooldown_seconds=%.3f', remaining)
                await asyncio.sleep(remaining)
            try:
                yield
            finally:
                self.finished = monotonic()


class PacedModel(WrapperModel):
    def __init__(self, wrapped, *, service_key: str, interval: float):
        super().__init__(wrapped)
        self.service_key, self.interval = service_key, interval

    @asynccontextmanager
    async def _slot(self):
        if not self.interval:
            yield
            return
        gates = _gates.setdefault(asyncio.get_running_loop(), {})
        gate = gates.setdefault(self.service_key, _Gate())
        async with gate.hold(self.interval):
            yield

    async def request(self, messages, model_settings, model_request_parameters):
        async with self._slot():
            return await self.wrapped.request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        async with self._slot():
            async with self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context) as response:
                yield response
