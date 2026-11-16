"""Data-access helpers. Each function takes an explicit session."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Chat,
    Delivery,
    Integration,
    MessageLink,
    PendingInput,
    SentArtifact,
    StatsCache,
    User,
)

# ── Users & chats ─────────────────────────────────────────────────────────────


async def get_or_create_user(s: AsyncSession, user_id: int) -> User:
    user = await s.get(User, user_id)
    if user is None:
        await s.execute(insert(User).values(id=user_id).on_conflict_do_nothing())
        user = await s.get(User, user_id)
        assert user is not None
    return user


async def upsert_chat(s: AsyncSession, chat_id: int, type_: str, title: str | None) -> None:
    stmt = insert(Chat).values(id=chat_id, type=type_, title=title, active=True)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Chat.id], set_={"type": type_, "title": title, "active": True}
    )
    await s.execute(stmt)


async def deactivate_chat(s: AsyncSession, chat_id: int) -> None:
    await s.execute(update(Chat).where(Chat.id == chat_id).values(active=False))
    await s.execute(
        update(Integration).where(Integration.chat_id == chat_id).values(active=False)
    )


# ── Integrations ──────────────────────────────────────────────────────────────


async def chat_integrations(s: AsyncSession, chat_id: int) -> Sequence[Integration]:
    res = await s.execute(
        select(Integration)
        .where(Integration.chat_id == chat_id)
        .options(selectinload(Integration.owner))
        .order_by(Integration.created_at)
    )
    return res.scalars().all()


async def count_chat_integrations(s: AsyncSession, chat_id: int) -> int:
    res = await s.execute(
        select(func.count()).select_from(Integration).where(Integration.chat_id == chat_id)
    )
    return int(res.scalar_one())


async def get_integration(s: AsyncSession, integration_id: int) -> Integration | None:
    res = await s.execute(
        select(Integration)
        .where(Integration.id == integration_id)
        .options(selectinload(Integration.owner), selectinload(Integration.chat))
    )
    return res.scalar_one_or_none()


async def integration_by_hook_key(s: AsyncSession, hook_key: str) -> Integration | None:
    res = await s.execute(select(Integration).where(Integration.hook_key == hook_key))
    return res.scalar_one_or_none()


async def find_integration(
    s: AsyncSession, chat_id: int, kind: str, target: str
) -> Integration | None:
    res = await s.execute(
        select(Integration).where(
            Integration.chat_id == chat_id,
            Integration.kind == kind,
            func.lower(Integration.target) == target.lower(),
        )
    )
    return res.scalar_one_or_none()


async def touch_integration(s: AsyncSession, integration_id: int) -> None:
    await s.execute(
        update(Integration)
        .where(Integration.id == integration_id)
        .values(last_event_at=datetime.now(UTC), hook_ok=True)
    )


# ── Deliveries (idempotent queue) ─────────────────────────────────────────────


async def insert_delivery(
    s: AsyncSession, delivery_id: str, integration_id: int, event: str, payload: dict[str, Any]
) -> int | None:
    """Insert a delivery; returns its id, or ``None`` if it was already seen."""
    stmt = (
        insert(Delivery)
        .values(
            delivery_id=delivery_id,
            integration_id=integration_id,
            event=event,
            payload=payload,
            status="queued",
        )
        .on_conflict_do_nothing(index_elements=[Delivery.delivery_id])
        .returning(Delivery.id)
    )
    res = await s.execute(stmt)
    row = res.scalar_one_or_none()
    return int(row) if row is not None else None


async def pending_deliveries(s: AsyncSession, limit: int = 500) -> Sequence[int]:
    res = await s.execute(
        select(Delivery.id)
        .where(Delivery.status == "queued", Delivery.attempts < 5)
        .order_by(Delivery.id)
        .limit(limit)
    )
    return res.scalars().all()


async def finish_delivery(
    s: AsyncSession, delivery_pk: int, status: str, error: str | None = None
) -> None:
    await s.execute(
        update(Delivery)
        .where(Delivery.id == delivery_pk)
        .values(
            status=status,
            error=error,
            processed_at=datetime.now(UTC),
            attempts=Delivery.attempts + 1,
        )
    )


async def prune_deliveries(s: AsyncSession, older_than: timedelta) -> int:
    res = await s.execute(
        delete(Delivery).where(
            Delivery.created_at < datetime.now(UTC) - older_than, Delivery.status != "queued"
        )
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


# ── Message map ───────────────────────────────────────────────────────────────


async def save_message_link(s: AsyncSession, **values: Any) -> None:
    stmt = insert(MessageLink).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_message",
        set_={k: v for k, v in values.items() if k not in {"chat_id", "message_id"}},
    )
    await s.execute(stmt)


async def get_message_link(s: AsyncSession, chat_id: int, message_id: int) -> MessageLink | None:
    res = await s.execute(
        select(MessageLink).where(
            MessageLink.chat_id == chat_id, MessageLink.message_id == message_id
        )
    )
    return res.scalar_one_or_none()


async def thread_messages(s: AsyncSession, chat_id: int, thread_key: str) -> Sequence[MessageLink]:
    res = await s.execute(
        select(MessageLink)
        .where(MessageLink.chat_id == chat_id, MessageLink.thread_key == thread_key)
        .order_by(MessageLink.id)
    )
    return res.scalars().all()


# ── Artifacts ─────────────────────────────────────────────────────────────────


async def claim_artifact(s: AsyncSession, key: str, chat_id: int) -> bool:
    """Atomically reserve an artifact for a chat. False if it was already sent."""
    res = await s.execute(
        insert(SentArtifact)
        .values(key=key, chat_id=chat_id)
        .on_conflict_do_nothing(constraint="uq_artifact_chat")
        .returning(SentArtifact.id)
    )
    return res.scalar_one_or_none() is not None


async def release_artifact_claim(s: AsyncSession, key: str, chat_id: int) -> None:
    await s.execute(
        delete(SentArtifact).where(SentArtifact.key == key, SentArtifact.chat_id == chat_id)
    )


async def set_artifact_file_id(s: AsyncSession, key: str, chat_id: int, file_id: str) -> None:
    await s.execute(
        update(SentArtifact)
        .where(SentArtifact.key == key, SentArtifact.chat_id == chat_id)
        .values(file_id=file_id)
    )


async def known_file_id(s: AsyncSession, key: str) -> str | None:
    res = await s.execute(
        select(SentArtifact.file_id)
        .where(SentArtifact.key == key, SentArtifact.file_id.is_not(None))
        .limit(1)
    )
    return res.scalar_one_or_none()


# ── Pending input (button flows) ──────────────────────────────────────────────


async def set_pending(
    s: AsyncSession,
    user_id: int,
    chat_id: int,
    kind: str,
    data: dict[str, Any],
    panel_message_id: int | None,
    ttl: timedelta,
) -> None:
    values = {
        "user_id": user_id,
        "chat_id": chat_id,
        "kind": kind,
        "data": data,
        "panel_message_id": panel_message_id,
        "expires_at": datetime.now(UTC) + ttl,
    }
    stmt = insert(PendingInput).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[PendingInput.user_id, PendingInput.chat_id],
        set_={k: v for k, v in values.items() if k not in {"user_id", "chat_id"}},
    )
    await s.execute(stmt)


async def get_pending(s: AsyncSession, user_id: int, chat_id: int) -> PendingInput | None:
    row = await s.get(PendingInput, (user_id, chat_id))
    if row is None:
        return None
    if row.expires_at < datetime.now(UTC):
        await s.delete(row)
        return None
    return row


async def clear_pending(s: AsyncSession, user_id: int, chat_id: int) -> None:
    await s.execute(
        delete(PendingInput).where(
            PendingInput.user_id == user_id, PendingInput.chat_id == chat_id
        )
    )


# ── Stats cache ───────────────────────────────────────────────────────────────


async def cache_get(s: AsyncSession, key: str, max_age: timedelta) -> dict[str, Any] | None:
    row = await s.get(StatsCache, key)
    if row is None or row.fetched_at < datetime.now(UTC) - max_age:
        return None
    return row.data


async def cache_put(s: AsyncSession, key: str, data: dict[str, Any]) -> None:
    stmt = insert(StatsCache).values(key=key, data=data, fetched_at=datetime.now(UTC))
    stmt = stmt.on_conflict_do_update(
        index_elements=[StatsCache.key], set_={"data": data, "fetched_at": datetime.now(UTC)}
    )
    await s.execute(stmt)
