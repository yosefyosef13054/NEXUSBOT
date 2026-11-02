from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class Approval(Base):
    """A paused agent turn waiting for the user to allow or refuse tool calls.

    The graph state itself lives in the LangGraph checkpointer under `thread_id`; this row
    carries what the client needs (the pending calls) and how to rebuild the runner
    (`generation`: model, tools, system prompt) when the decision arrives.
    """

    __tablename__ = "approvals"

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The placeholder assistant message updated in place once the turn completes.
    message_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    thread_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="approval_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ApprovalStatus.PENDING,
    )
    tool_calls: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    generation: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
