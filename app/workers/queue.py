"""Durable in-process work queue backed by the ``deliveries`` table.

The webhook endpoint inserts a row and pushes its id here; workers pick it up.
On startup every still-queued row is re-enqueued, so nothing is lost on restart.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from enum import StrEnum

from app.context import AppContext
from app.db import repo as db
from app.db.models import Delivery
from app.github.client import RateLimited
from app.log import get_logger

log = get_logger(__name__)

MAX_ATTEMPTS = 5


class Outcome(StrEnum):
    DONE = "done"
    SKIPPED = "skipped"
    DEFERRED = "deferred"  # handed to the debouncer; it will finish the row


class EventQueue:
    def __init__(
        self,
        ctx: AppContext,
        handler: Callable[[int], Awaitable[Outcome]],
        concurrency: int = 8,
    ) -> None:
        self.ctx = ctx
        self._handler = handler
        self._queue: asyncio.Queue[int] = asyncio.Queue(maxsize=10_000)
        self._concurrency = concurrency
        self._workers: list[asyncio.Task[None]] = []
        self._timers: set[asyncio.TimerHandle] = set()
        self.processed = 0
        self.failed = 0

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def enqueue(self, delivery_pk: int) -> None:
        try:
            self._queue.put_nowait(delivery_pk)
        except asyncio.QueueFull:  # row stays "queued" and is picked up on next start/sweep
            log.warning("queue_full", delivery=delivery_pk)

    def enqueue_later(self, delivery_pk: int, delay: float) -> None:
        loop = asyncio.get_running_loop()
        handle = loop.call_later(delay, self.enqueue, delivery_pk)
        self._timers.add(handle)

    async def start(self) -> None:
        async with self.ctx.db() as s:
            pending = await db.pending_deliveries(s)
        for pk in pending:
            self.enqueue(pk)
        if pending:
            log.info("requeued_pending", count=len(pending))
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"worker-{i}")
            for i in range(self._concurrency)
        ]
        self._workers.append(asyncio.create_task(self._janitor(), name="janitor"))

    async def stop(self) -> None:
        for h in self._timers:
            h.cancel()
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await w

    async def _worker(self, n: int) -> None:
        while True:
            pk = await self._queue.get()
            try:
                await self._run(pk)
            finally:
                self._queue.task_done()

    async def _run(self, pk: int) -> None:
        try:
            outcome = await self._handler(pk)
        except RateLimited as exc:
            log.warning("github_rate_limited", delivery=pk, retry_in=round(exc.retry_in))
            await self._retry(pk, str(exc), delay=min(exc.retry_in + 1, 3600))
            return
        except Exception as exc:
            log.exception("delivery_failed", delivery=pk)
            await self._retry(pk, f"{type(exc).__name__}: {exc}"[:500])
            return
        if outcome is Outcome.DEFERRED:
            return
        async with self.ctx.db() as s, s.begin():
            await db.finish_delivery(s, pk, outcome.value)
        self.processed += 1

    async def _retry(self, pk: int, error: str, delay: float | None = None) -> None:
        async with self.ctx.db() as s, s.begin():
            row = await s.get(Delivery, pk)
            if row is None:
                return
            row.attempts += 1
            row.error = error
            if row.attempts >= MAX_ATTEMPTS:
                row.status = "failed"
                self.failed += 1
                return
            attempts = row.attempts
        self.enqueue_later(pk, delay if delay is not None else min(2**attempts * 2, 120))

    async def _janitor(self) -> None:
        """Hourly: prune old deliveries and expired button-flow state."""
        while True:
            await asyncio.sleep(3600)
            try:
                async with self.ctx.db() as s, s.begin():
                    n = await db.prune_deliveries(s, timedelta(days=7))
                if n:
                    log.info("pruned_deliveries", count=n)
            except Exception:
                log.exception("janitor_failed")
