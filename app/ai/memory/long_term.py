from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatSession


async def load_session_summary(session: AsyncSession, *, session_id: UUID) -> str:
    """The rolling summary acts as long-term memory for a single chat session.

    For cross-session / cross-user memory, write a thin adapter that pulls from
    a dedicated `memories` table and concatenate it with the per-session summary.
    """
    row = await session.get(ChatSession, session_id)
    return (row.summary or "") if row else ""


async def save_session_summary(
    session: AsyncSession, *, session_id: UUID, summary: str
) -> None:
    row = await session.get(ChatSession, session_id)
    if row is None:
        return
    row.summary = summary
    await session.flush()
