from __future__ import annotations

import enum
from uuid import UUID

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AttachmentKind(str, enum.Enum):
    IMAGE = "image"  # shown to vision models
    FILE = "file"  # text extracted and given to the model inline
    AUDIO = "audio"  # transcribed on upload (input) or synthesized replies (output)
    ARTIFACT = "artifact"  # produced by a tool during a turn


class Attachment(Base):
    """A binary bound to a chat message (or waiting to be bound), stored in object storage."""

    __tablename__ = "attachments"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[AttachmentKind] = mapped_column(
        Enum(AttachmentKind, name="attachment_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Extracted text (files) or transcript (audio), truncated to ATTACHMENT_TEXT_MAX_CHARS.
    text_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
