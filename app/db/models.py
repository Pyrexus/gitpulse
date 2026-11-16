"""SQLAlchemy 2.0 typed ORM models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, datetime: DateTime(timezone=True)}


class User(Base):
    """A Telegram user, optionally linked to a GitHub identity."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    github_id: Mapped[int | None] = mapped_column(BigInteger)
    github_login: Mapped[str | None] = mapped_column(String(64))
    github_name: Mapped[str | None] = mapped_column(String(255))
    token_enc: Mapped[str | None] = mapped_column(Text)
    token_scopes: Mapped[str | None] = mapped_column(String(512))
    auth_method: Mapped[str | None] = mapped_column(String(16))  # pat | oauth
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    @property
    def logged_in(self) -> bool:
        return bool(self.token_enc and self.github_login)


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    integrations: Mapped[list[Integration]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )


class Integration(Base):
    """A repo or org connected to a chat through a GitHub webhook."""

    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("chat_id", "kind", "target", name="uq_integration_target"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(8))  # repo | org
    target: Mapped[str] = mapped_column(String(160))  # owner/repo or org login
    target_id: Mapped[int] = mapped_column(BigInteger)
    hook_id: Mapped[int | None] = mapped_column(BigInteger)
    hook_key: Mapped[str] = mapped_column(String(48), unique=True)
    secret_enc: Mapped[str] = mapped_column(Text)
    events: Mapped[int] = mapped_column(Integer)
    branch_filter: Mapped[str | None] = mapped_column(String(255))
    quiet: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    hook_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    last_event_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    chat: Mapped[Chat] = relationship(back_populates="integrations")
    owner: Mapped[User] = relationship()


class Delivery(Base):
    """Every GitHub webhook delivery, deduplicated by X-GitHub-Delivery."""

    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String(64), unique=True)
    integration_id: Mapped[int | None] = mapped_column(
        ForeignKey("integrations.id", ondelete="SET NULL"), index=True
    )
    event: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, Any]] = mapped_column()
    status: Mapped[str] = mapped_column(String(12), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    processed_at: Mapped[datetime | None] = mapped_column()


class MessageLink(Base):
    """Maps a Telegram message to the GitHub thing it represents (for replies & edits)."""

    __tablename__ = "message_links"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_message"),
        Index("ix_message_thread", "chat_id", "thread_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    integration_id: Mapped[int | None] = mapped_column(
        ForeignKey("integrations.id", ondelete="SET NULL")
    )
    repo: Mapped[str] = mapped_column(String(160))
    number: Mapped[int | None] = mapped_column(Integer)
    comment_id: Mapped[int | None] = mapped_column(BigInteger)  # review comment threads
    kind: Mapped[str] = mapped_column(String(16))
    thread_key: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str | None] = mapped_column(String(16))
    is_caption: Mapped[bool] = mapped_column(Boolean, default=False)
    html_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SentArtifact(Base):
    """Artifacts/assets already delivered, so nothing is sent twice."""

    __tablename__ = "sent_artifacts"
    __table_args__ = (UniqueConstraint("key", "chat_id", name="uq_artifact_chat"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(96), index=True)  # artifact:<id> | asset:<id>
    chat_id: Mapped[int] = mapped_column(BigInteger)
    file_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PendingInput(Base):
    """A button flow waiting for the user's next text message (survives restarts)."""

    __tablename__ = "pending_inputs"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    panel_message_id: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column()


class StatsCache(Base):
    """Cached GitHub statistics (contributor stats are slow to compute upstream)."""

    __tablename__ = "stats_cache"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column()
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)
