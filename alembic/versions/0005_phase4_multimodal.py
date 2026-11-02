"""phase 4 multimodal: attachments, mcp servers, devices, session shares

Revision ID: 0005_phase4_multimodal
Revises: 0004_phase3_product
Create Date: 2026-11-02

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_phase4_multimodal"
down_revision: Union[str, None] = "0004_phase3_product"
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

    attachment_kind = postgresql.ENUM("image", "file", "audio", "artifact", name="attachment_kind", create_type=False)
    attachment_kind.create(bind, checkfirst=True)
    op.create_table(
        "attachments",
        *_timestamps(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", attachment_kind, nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("width", sa.Integer, nullable=True),
        sa.Column("height", sa.Integer, nullable=True),
        sa.Column("text_excerpt", sa.Text, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    for col in ("user_id", "org_id", "session_id", "message_id"):
        op.create_index(f"ix_attachments_{col}", "attachments", [col])

    mcp_transport = postgresql.ENUM("stdio", "http", "sse", name="mcp_transport", create_type=False)
    mcp_transport.create(bind, checkfirst=True)
    op.create_table(
        "mcp_servers",
        *_timestamps(),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("transport", mcp_transport, nullable=False),
        sa.Column("command", sa.String(512), nullable=True),
        sa.Column("args", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("env", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("url", sa.String(2048), nullable=True),
        sa.Column("headers", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("requires_approval", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("org_id", "name", name="uq_mcp_servers_org_name"),
    )
    op.create_index("ix_mcp_servers_org_id", "mcp_servers", ["org_id"])

    op.create_table(
        "devices",
        *_timestamps(),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("token", sa.String(1024), nullable=False),
        sa.Column("app_version", sa.String(64), nullable=True),
        sa.Column("locale", sa.String(16), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("platform", "token", name="uq_devices_platform_token"),
    )
    op.create_index("ix_devices_user_id", "devices", ["user_id"])

    op.create_table(
        "session_shares",
        *_timestamps(),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("views", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("token_hash", name="uq_session_shares_token_hash"),
    )
    op.create_index("ix_session_shares_session_id", "session_shares", ["session_id"])
    op.create_index("ix_session_shares_user_id", "session_shares", ["user_id"])


def downgrade() -> None:
    op.drop_table("session_shares")
    op.drop_table("devices")
    op.drop_table("mcp_servers")
    op.execute("DROP TYPE IF EXISTS mcp_transport")
    op.drop_table("attachments")
    op.execute("DROP TYPE IF EXISTS attachment_kind")
