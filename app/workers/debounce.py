"""Group rapid events (pushes to one branch, a review and its comments)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from app.log import get_logger

log = get_logger(__name__)
K = TypeVar("K", bound=Hashable)


@dataclass
class Batch:
    items: list[Any] = field(default_factory=list)
    delivery_ids: list[int] = field(default_factory=list)
    first_seen: float = field(default_factory=time.monotonic)
    timer: asyncio.TimerHandle | None = None


class Debouncer(Generic[K]):
    """Flushes a key ``delay`` s after its last item, but at most ``max_wait`` s after the first."""

    def __init__(
        self,
        delay: float,
        max_wait: float,
        on_flush: Callable[[K, Batch], Awaitable[None]],
    ) -> None:
        self.delay = delay
        self.max_wait = max_wait
        self._on_flush = on_flush
        self._batches: dict[K, Batch] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def add(self, key: K, item: Any, delivery_id: int | None = None) -> None:
        batch = self._batches.get(key)
        if batch is None:
            batch = self._batches[key] = Batch()
        batch.items.append(item)
        if delivery_id is not None:
            batch.delivery_ids.append(delivery_id)
        if batch.timer:
            batch.timer.cancel()
        elapsed = time.monotonic() - batch.first_seen
        wait = max(0.0, min(self.delay, self.max_wait - elapsed))
        loop = asyncio.get_running_loop()
        batch.timer = loop.call_later(wait, self._schedule, key)

    def _schedule(self, key: K) -> None:
        task = asyncio.create_task(self._flush(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _flush(self, key: K) -> None:
        batch = self._batches.pop(key, None)
        if batch is None:
            return
        try:
            await self._on_flush(key, batch)
        except Exception:
            log.exception("debounce_flush_failed", key=str(key))

    async def flush_all(self) -> None:
        for key, batch in list(self._batches.items()):
            if batch.timer:
                batch.timer.cancel()
            await self._flush(key)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def __len__(self) -> int:
        return len(self._batches)
