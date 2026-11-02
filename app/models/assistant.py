from __future__ import annotations

from uuid import UUID

from sqlalchemy import Boolean, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Assistant(Base):
    """A saved persona: system prompt + model + tools + knowledge scope + memory policy.

    Shared across the organization. A session that references an assistant takes its
    settings as defaults; per-turn request fields still override.
    """

    __tablename__ = "assistants"

    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar: Mapped[str | None] = mapped_column(String(16), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_names: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    collection_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    use_rag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    use_tools: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
