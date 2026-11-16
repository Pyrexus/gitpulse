"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("github_id", sa.BigInteger),
        sa.Column("github_login", sa.String(64)),
        sa.Column("github_name", sa.String(255)),
        sa.Column("token_enc", sa.Text),
        sa.Column("token_scopes", sa.String(512)),
        sa.Column("auth_method", sa.String(16)),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "chats",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255)),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "integrations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "chat_id", sa.BigInteger, sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "owner_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("target", sa.String(160), nullable=False),
        sa.Column("target_id", sa.BigInteger, nullable=False),
        sa.Column("hook_id", sa.BigInteger),
        sa.Column("hook_key", sa.String(48), nullable=False, unique=True),
        sa.Column("secret_enc", sa.Text, nullable=False),
        sa.Column("events", sa.Integer, nullable=False),
        sa.Column("branch_filter", sa.String(255)),
        sa.Column("quiet", sa.Boolean, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("hook_ok", sa.Boolean, nullable=False),
        sa.Column("last_event_at", TS),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("chat_id", "kind", "target", name="uq_integration_target"),
    )
    op.create_index("ix_integrations_chat_id", "integrations", ["chat_id"])
    op.create_table(
        "deliveries",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("delivery_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "integration_id",
            sa.Integer,
            sa.ForeignKey("integrations.id", ondelete="SET NULL"),
        ),
        sa.Column("event", sa.String(48), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("processed_at", TS),
    )
    op.create_index("ix_deliveries_integration_id", "deliveries", ["integration_id"])
    op.create_index("ix_deliveries_status", "deliveries", ["status"])
    op.create_index("ix_deliveries_created_at", "deliveries", ["created_at"])
    op.create_table(
        "message_links",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("message_id", sa.BigInteger, nullable=False),
        sa.Column(
            "integration_id",
            sa.Integer,
            sa.ForeignKey("integrations.id", ondelete="SET NULL"),
        ),
        sa.Column("repo", sa.String(160), nullable=False),
        sa.Column("number", sa.Integer),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("thread_key", sa.String(200)),
        sa.Column("state", sa.String(16)),
        sa.Column("is_caption", sa.Boolean, nullable=False),
        sa.Column("html_url", sa.Text),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("chat_id", "message_id", name="uq_message"),
    )
    op.create_index("ix_message_thread", "message_links", ["chat_id", "thread_key"])
    op.create_table(
        "sent_artifacts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("key", sa.String(96), nullable=False),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("file_id", sa.Text),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("key", "chat_id", name="uq_artifact_chat"),
    )
    op.create_index("ix_sent_artifacts_key", "sent_artifacts", ["key"])
    op.create_table(
        "pending_inputs",
        sa.Column("user_id", sa.BigInteger, primary_key=True),
        sa.Column("chat_id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("data", postgresql.JSONB, nullable=False),
        sa.Column("panel_message_id", sa.BigInteger),
        sa.Column("expires_at", TS, nullable=False),
    )
    op.create_table(
        "stats_cache",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("data", postgresql.JSONB, nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
    )


def downgrade() -> None:
    for table in (
        "stats_cache",
        "pending_inputs",
        "sent_artifacts",
        "message_links",
        "deliveries",
        "integrations",
        "chats",
        "users",
    ):
        op.drop_table(table)
