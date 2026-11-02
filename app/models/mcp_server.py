from __future__ import annotations

import enum
from uuid import UUID

from sqlalchemy import Boolean, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MCPTransport(str, enum.Enum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


class MCPServer(Base):
    """An external Model Context Protocol server registered for an organization."""

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_mcp_servers_org_name"),)

    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    transport: Mapped[MCPTransport] = mapped_column(
        Enum(MCPTransport, name="mcp_transport", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    command: Mapped[str | None] = mapped_column(String(512), nullable=True)
    args: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Secrets live here and are never returned by the API (only their key names).
    env: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    headers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
