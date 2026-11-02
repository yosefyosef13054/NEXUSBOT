"""phase 2 ai core: collections, approvals, full-text columns, model preferences, agent checkpoints

Revision ID: 0003_phase2_ai_core
Revises: 0002_phase1_foundation
Create Date: 2026-11-02

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_phase2_ai_core"
down_revision: Union[str, None] = "0002_phase1_foundation"
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
    # ---- collections ----
    op.create_table(
        "collections",
        *_timestamps(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_collections_user_id", "collections", ["user_id"])

    op.add_column(
        "documents",
        sa.Column(
            "collection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_documents_collection_id", "documents", ["collection_id"])
    op.add_column("documents", sa.Column("source_url", sa.String(2048), nullable=True))

    # ---- full-text search columns (generated) ----
    op.execute(
        "ALTER TABLE document_chunks ADD COLUMN content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"
    )
    op.execute("CREATE INDEX ix_document_chunks_content_tsv ON document_chunks USING gin (content_tsv)")
    op.execute(
        "ALTER TABLE chat_messages ADD COLUMN content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"
    )
    op.execute("CREATE INDEX ix_chat_messages_content_tsv ON chat_messages USING gin (content_tsv)")

    # ---- model preference ----
    op.add_column("users", sa.Column("default_model", sa.String(64), nullable=True))

    # ---- approvals ----
    approval_status = postgresql.ENUM(
        "pending", "approved", "denied", "expired", name="approval_status", create_type=False
    )
    approval_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "approvals",
        *_timestamps(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("status", approval_status, nullable=False, server_default="pending"),
        sa.Column("tool_calls", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("generation", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_approvals_user_id", "approvals", ["user_id"])
    op.create_index("ix_approvals_session_id", "approvals", ["session_id"])
    op.create_index("ix_approvals_thread_id", "approvals", ["thread_id"])

    # ---- LangGraph checkpoint tables (same SQL the saver's setup() runs) ----
    from langgraph.checkpoint.postgres.base import MIGRATIONS

    for version, statement in enumerate(MIGRATIONS):
        if statement.strip():  # the list contains no-op placeholders for retired steps
            # Alembic runs inside a transaction and the tables are empty: a plain index
            # build is equivalent to the saver's CONCURRENTLY variant here.
            op.execute(statement.replace("CONCURRENTLY ", ""))
        if version > 0:
            op.execute(f"INSERT INTO checkpoint_migrations (v) VALUES ({version}) ON CONFLICT DO NOTHING")


def downgrade() -> None:
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.drop_index("ix_approvals_thread_id", table_name="approvals")
    op.drop_index("ix_approvals_session_id", table_name="approvals")
    op.drop_index("ix_approvals_user_id", table_name="approvals")
    op.drop_table("approvals")
    op.execute("DROP TYPE IF EXISTS approval_status")
    op.drop_column("users", "default_model")
    op.execute("DROP INDEX IF EXISTS ix_chat_messages_content_tsv")
    op.execute("ALTER TABLE chat_messages DROP COLUMN IF EXISTS content_tsv")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_content_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS content_tsv")
    op.drop_column("documents", "source_url")
    op.drop_index("ix_documents_collection_id", table_name="documents")
    op.drop_column("documents", "collection_id")
    op.drop_index("ix_collections_user_id", table_name="collections")
    op.drop_table("collections")
