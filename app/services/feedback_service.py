from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.chat import ChatMessage, MessageRole
from app.models.feedback import MessageFeedback
from app.services.session_service import get_session


async def _message(db: AsyncSession, *, user_id: UUID, session_id: UUID, message_id: UUID) -> ChatMessage:
    await get_session(db, user_id=user_id, session_id=session_id)
    msg = await db.get(ChatMessage, message_id)
    if msg is None or msg.session_id != session_id or msg.deleted_at is not None:
        raise NotFoundError("Message not found")
    if msg.role != MessageRole.ASSISTANT:
        raise NotFoundError("Feedback applies to assistant messages only")
    return msg


async def set_feedback(
    db: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID | None,
    session_id: UUID,
    message_id: UUID,
    rating: int,
    comment: str | None,
) -> MessageFeedback:
    msg = await _message(db, user_id=user_id, session_id=session_id, message_id=message_id)
    stmt = select(MessageFeedback).where(
        MessageFeedback.message_id == message_id, MessageFeedback.user_id == user_id
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = MessageFeedback(
            message_id=message_id,
            session_id=session_id,
            user_id=user_id,
            org_id=org_id,
            rating=rating,
            comment=comment,
        )
        db.add(row)
    else:
        row.rating = rating
        row.comment = comment
    # Mirror on the message so history reads need no join.
    msg.meta = {**(msg.meta or {}), "feedback": {"rating": rating, "comment": comment}}
    await db.flush()
    return row


async def delete_feedback(db: AsyncSession, *, user_id: UUID, session_id: UUID, message_id: UUID) -> bool:
    msg = await _message(db, user_id=user_id, session_id=session_id, message_id=message_id)
    stmt = select(MessageFeedback).where(
        MessageFeedback.message_id == message_id, MessageFeedback.user_id == user_id
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    meta = dict(msg.meta or {})
    meta.pop("feedback", None)
    msg.meta = meta
    await db.flush()
    return True


async def list_feedback(
    db: AsyncSession, *, org_id: UUID | None, page: int, page_size: int
) -> tuple[list[MessageFeedback], int]:
    base = select(MessageFeedback)
    if org_id is not None:
        base = base.where(MessageFeedback.org_id == org_id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                base.order_by(MessageFeedback.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)
