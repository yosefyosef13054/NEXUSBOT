from __future__ import annotations

from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.chat import ChatMessage, MessageRole


def _to_lc_message(row: ChatMessage) -> BaseMessage:
    if row.role == MessageRole.USER:
        return HumanMessage(content=row.content)
    if row.role == MessageRole.ASSISTANT:
        return AIMessage(content=row.content)
    return SystemMessage(content=row.content)


async def load_recent_history(
    session: AsyncSession,
    *,
    session_id: UUID,
    limit: int | None = None,
) -> list[BaseMessage]:
    """Load the trailing N messages of a session as LangChain message objects.

    Older context lives in the session's rolling `summary`; this function only
    pulls the recent window, keeping prompt size bounded.
    """
    cap = limit or settings.CHAT_MAX_HISTORY_MESSAGES
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.deleted_at.is_(None),
            ChatMessage.role != MessageRole.SYSTEM,
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(cap)
    )
    rows = (await session.execute(stmt)).scalars().all()
    rows = list(reversed(rows))
    return [_to_lc_message(r) for r in rows]
