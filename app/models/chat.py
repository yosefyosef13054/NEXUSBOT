from __future__ import annotations

import enum
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Computed, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class MessageRole(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Workspace the chat was started in (sessions stay private to their user).
    org_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # Persona whose settings (prompt, model, tools, scope) are the defaults for every turn.
    assistant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("assistants.id", ondelete="SET NULL"), nullable=True
    )
    # Leaf of the active branch of the message tree (None = latest message).
    active_leaf_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "chat_messages.id", ondelete="SET NULL", use_alter=True, name="fk_chat_sessions_active_leaf"
        ),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New chat")
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Rolling summary of older context — kept short by the summarizer.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form settings, e.g. {"collection_ids": [...]} for the knowledge scope.
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    user: Mapped[User] = relationship(back_populates="sessions")
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at.asc()",
        foreign_keys="ChatMessage.session_id",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, name="message_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Generated full-text column for chat search.
    content_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True), nullable=True
    )
    # Optional structured info: tool calls, citations, model, latency, status, approval.
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Message tree: user messages branch on edits, assistant messages on regenerations.
    # Tool messages point at the user message of their turn.
    parent_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True, index=True
    )

    session: Mapped[ChatSession] = relationship(back_populates="messages", foreign_keys=[session_id])

    __table_args__ = (Index("ix_chat_messages_content_tsv", "content_tsv", postgresql_using="gin"),)
