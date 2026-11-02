from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db.base import Base


class MemoryKind(str, enum.Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    INSTRUCTION = "instruction"


class Memory(Base):
    """A durable fact / preference / standing instruction about a user.

    Extracted in the background after turns (or added by the user), embedded so the most
    relevant memories can be injected into the system prompt of later conversations.
    """

    __tablename__ = "memories"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    org_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[MemoryKind] = mapped_column(
        Enum(MemoryKind, name="memory_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=MemoryKind.FACT,
    )
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.OPENAI_EMBEDDING_DIM), nullable=True
    )
    source_message_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index(
            "ix_memories_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
