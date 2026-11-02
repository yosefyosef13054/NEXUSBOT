"""phase 3 product layer: organizations, assistants, feedback, memories, message tree

Revision ID: 0004_phase3_product
Revises: 0003_phase2_ai_core
Create Date: 2026-11-02

"""

from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import settings

revision: str = "0004_phase3_product"
down_revision: Union[str, None] = "0003_phase2_ai_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    bind = op.get_bind()

    # ---- organizations & memberships ----
    op.create_table(
        "organizations",
        *_timestamps(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(140), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_personal", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("plan", sa.String(32), nullable=False, server_default="free"),
        sa.Column("settings", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)
    op.create_index("ix_organizations_owner_id", "organizations", ["owner_id"])

    org_role = postgresql.ENUM("owner", "admin", "member", name="org_role", create_type=False)
    org_role.create(bind, checkfirst=True)
    op.create_table(
        "memberships",
        *_timestamps(),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", org_role, nullable=False, server_default="member"),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "user_id", name="uq_memberships_org_user"),
    )
    op.create_index("ix_memberships_org_id", "memberships", ["org_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])

    # ---- assistants ----
    op.create_table(
        "assistants",
        *_timestamps(),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("avatar", sa.String(16), nullable=True),
        sa.Column("system_prompt", sa.Text, nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("tool_names", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("collection_ids", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("use_rag", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("use_tools", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("temperature", sa.Float, nullable=True),
        sa.Column("memory_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_assistants_org_id", "assistants", ["org_id"])

    # ---- org scoping columns ----
    for table, ondelete in (("chat_sessions", "CASCADE"), ("documents", "CASCADE"), ("collections", "CASCADE"), ("ai_usage_logs", "SET NULL")):
        op.add_column(
            table,
            sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete=ondelete), nullable=True),
        )
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])

    op.add_column(
        "chat_sessions",
        sa.Column("assistant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("assistants.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column("users", sa.Column("memory_enabled", sa.Boolean, nullable=False, server_default=sa.true()))

    # ---- message tree ----
    op.add_column(
        "chat_messages",
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_chat_messages_parent_id", "chat_messages", ["parent_id"])
    op.add_column("chat_sessions", sa.Column("active_leaf_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_chat_sessions_active_leaf", "chat_sessions", "chat_messages", ["active_leaf_id"], ["id"], ondelete="SET NULL"
    )

    # ---- feedback ----
    op.create_table(
        "message_feedback",
        *_timestamps(),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("message_id", "user_id", name="uq_message_feedback_message_user"),
    )
    for col in ("message_id", "session_id", "user_id", "org_id"):
        op.create_index(f"ix_message_feedback_{col}", "message_feedback", [col])

    # ---- memories ----
    memory_kind = postgresql.ENUM("fact", "preference", "instruction", name="memory_kind", create_type=False)
    memory_kind.create(bind, checkfirst=True)
    from pgvector.sqlalchemy import Vector

    op.create_table(
        "memories",
        *_timestamps(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("kind", memory_kind, nullable=False, server_default="fact"),
        sa.Column("importance", sa.Integer, nullable=False, server_default="3"),
        sa.Column("embedding", Vector(settings.OPENAI_EMBEDDING_DIM), nullable=True),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_memories_user_id", "memories", ["user_id"])
    op.create_index("ix_memories_org_id", "memories", ["org_id"])
    op.execute(
        "CREATE INDEX ix_memories_embedding_hnsw ON memories USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # ---- data backfill: personal orgs + org scoping + message parents ----
    users = bind.execute(sa.text("SELECT id FROM users")).fetchall()
    for (user_id,) in users:
        org_id = uuid.uuid4()
        bind.execute(
            sa.text(
                "INSERT INTO organizations (id, name, slug, owner_id, is_personal, plan, settings) "
                "VALUES (:id, 'Personal', :slug, :owner, true, 'free', '{}'::jsonb)"
            ),
            {"id": org_id, "slug": f"personal-{uuid.UUID(str(user_id)).hex[:12]}", "owner": user_id},
        )
        bind.execute(
            sa.text("INSERT INTO memberships (id, org_id, user_id, role) VALUES (:id, :org, :user, 'owner')"),
            {"id": uuid.uuid4(), "org": org_id, "user": user_id},
        )
        for table in ("chat_sessions", "documents", "collections", "ai_usage_logs"):
            bind.execute(
                sa.text(f"UPDATE {table} SET org_id = :org WHERE user_id = :user AND org_id IS NULL"),
                {"org": org_id, "user": user_id},
            )

    sessions = bind.execute(sa.text("SELECT id FROM chat_sessions")).fetchall()
    for (session_id,) in sessions:
        rows = bind.execute(
            sa.text("SELECT id, role FROM chat_messages WHERE session_id = :sid ORDER BY created_at ASC, id ASC"),
            {"sid": session_id},
        ).fetchall()
        last_turn = None
        pending_tools: list = []
        for message_id, role in rows:
            if role == "tool":
                pending_tools.append(message_id)
                continue
            if last_turn is not None:
                bind.execute(
                    sa.text("UPDATE chat_messages SET parent_id = :parent WHERE id = :id"),
                    {"parent": last_turn, "id": message_id},
                )
            if role == "assistant":
                for tool_id in pending_tools:
                    bind.execute(
                        sa.text("UPDATE chat_messages SET parent_id = :parent WHERE id = :id"),
                        {"parent": message_id, "id": tool_id},
                    )
                pending_tools = []
            last_turn = message_id


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    op.drop_table("memories")
    op.execute("DROP TYPE IF EXISTS memory_kind")
    op.drop_table("message_feedback")
    op.drop_constraint("fk_chat_sessions_active_leaf", "chat_sessions", type_="foreignkey")
    op.drop_column("chat_sessions", "active_leaf_id")
    op.drop_index("ix_chat_messages_parent_id", table_name="chat_messages")
    op.drop_column("chat_messages", "parent_id")
    op.drop_column("users", "memory_enabled")
    op.drop_column("chat_sessions", "assistant_id")
    for table in ("chat_sessions", "documents", "collections", "ai_usage_logs"):
        op.drop_index(f"ix_{table}_org_id", table_name=table)
        op.drop_column(table, "org_id")
    op.drop_index("ix_assistants_org_id", table_name="assistants")
    op.drop_table("assistants")
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_index("ix_memberships_org_id", table_name="memberships")
    op.drop_table("memberships")
    op.execute("DROP TYPE IF EXISTS org_role")
    op.drop_index("ix_organizations_owner_id", table_name="organizations")
    op.drop_index("ix_organizations_slug", table_name="organizations")
    op.drop_table("organizations")
