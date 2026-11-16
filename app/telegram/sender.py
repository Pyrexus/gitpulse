"""Rate-limited, per-chat ordered Telegram calls.

* every chat has its own FIFO lock → messages arrive in order
* per-chat token bucket (≈1 msg/s private, ≈20 msg/min groups/channels, with bursts)
* global token bucket (~25 msg/s, under Telegram's 30/s)
* ``RetryAfter`` is honoured exactly; network/5xx errors back off exponentially
* kicked/blocked/deleted chats are reported once and then skipped
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from app.log import get_logger

log = get_logger(__name__)
T = TypeVar("T")

_GONE_MARKERS = (
    "chat not found",
    "bot was kicked",
    "group chat was deactivated",
    "user is deactivated",
)


class ChatUnavailable(Exception):
    """The bot can no longer post in this chat."""


class TokenBucket:
    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class Sender:
    def __init__(
        self,
        on_gone: Callable[[int], Awaitable[None]],
        on_migrate: Callable[[int, int], Awaitable[None]] | None = None,
        global_rate: float = 25.0,
    ) -> None:
        self._on_gone = on_gone
        self._on_migrate = on_migrate
        self._global = TokenBucket(global_rate, global_rate)
        self._locks: dict[int, asyncio.Lock] = {}
        self._buckets: dict[int, TokenBucket] = {}
        self._gone: set[int] = set()

    def _bucket(self, chat_id: int) -> TokenBucket:
        b = self._buckets.get(chat_id)
        if b is None:
            b = TokenBucket(1.0, 4) if chat_id > 0 else TokenBucket(20 / 60, 6)
            self._buckets[chat_id] = b
        return b

    def forget(self, chat_id: int) -> None:
        self._gone.discard(chat_id)

    async def call(self, chat_id: int, factory: Callable[[], Awaitable[T]], retries: int = 5) -> T:
        """Run ``factory()`` (a Telegram API call) under the chat's rate limits."""
        if chat_id in self._gone:
            raise ChatUnavailable(chat_id)
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            delay = 1.0
            for attempt in range(retries + 1):
                await self._bucket(chat_id).acquire()
                await self._global.acquire()
                try:
                    return await factory()
                except TelegramRetryAfter as exc:
                    log.warning("flood_wait", chat_id=chat_id, retry_after=exc.retry_after)
                    await asyncio.sleep(exc.retry_after + 0.25)
                except TelegramMigrateToChat as exc:
                    if self._on_migrate:
                        await self._on_migrate(chat_id, exc.migrate_to_chat_id)
                    raise ChatUnavailable(chat_id) from exc
                except TelegramForbiddenError as exc:
                    await self._mark_gone(chat_id, str(exc))
                    raise ChatUnavailable(chat_id) from exc
                except TelegramBadRequest as exc:
                    if any(m in str(exc).lower() for m in _GONE_MARKERS):
                        await self._mark_gone(chat_id, str(exc))
                        raise ChatUnavailable(chat_id) from exc
                    raise
                except (TelegramNetworkError, TelegramServerError) as exc:
                    if attempt >= retries:
                        raise
                    log.warning("telegram_retry", chat_id=chat_id, error=type(exc).__name__)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
            raise ChatUnavailable(chat_id)  # pragma: no cover - retries exhausted on flood

    async def _mark_gone(self, chat_id: int, reason: str) -> None:
        if chat_id in self._gone:
            return
        self._gone.add(chat_id)
        log.info("chat_unavailable", chat_id=chat_id, reason=reason[:120])
        try:
            await self._on_gone(chat_id)
        except Exception:
            log.exception("on_gone_failed", chat_id=chat_id)
