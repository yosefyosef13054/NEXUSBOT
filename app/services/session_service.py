from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import build_chat_llm
from app.ai.prompts.templates import TITLE_PROMPT
from app.core.exceptions import NotFoundError
from app.models.chat import ChatMessage, ChatSession, MessageRole


async def create_session(
    db: AsyncSession,
    *,
    user_id: UUID,
    title: str | None,
    system_prompt: str | None,
    model: str | None,
    meta: dict | None = None,
) -> ChatSession:
    obj = ChatSession(
        user_id=user_id,
        title=title or "New chat",
        system_prompt=system_prompt,
        model=model,
        meta=meta or {},
    )
    db.add(obj)
    await db.flush()
    return obj


async def get_session(db: AsyncSession, *, user_id: UUID, session_id: UUID) -> ChatSession:
    obj = await db.get(ChatSession, session_id)
    if obj is None or obj.user_id != user_id or obj.deleted_at is not None:
        raise NotFoundError("Session not found")
    return obj


async def list_sessions(
    db: AsyncSession, *, user_id: UUID, page: int, page_size: int
) -> tuple[list[ChatSession], int]:
    base = select(ChatSession).where(
        ChatSession.user_id == user_id, ChatSession.deleted_at.is_(None)
    )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await db.execute(
            base.order_by(ChatSession.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), int(total)


async def update_session(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    title: str | None = None,
    system_prompt: str | None = None,
    meta: dict | None = None,
) -> ChatSession:
    obj = await get_session(db, user_id=user_id, session_id=session_id)
    if title is not None:
        obj.title = title
    if system_prompt is not None:
        obj.system_prompt = system_prompt
    if meta is not None:
        obj.meta = {**(obj.meta or {}), **meta}
    await db.flush()
    return obj


async def soft_delete_session(db: AsyncSession, *, user_id: UUID, session_id: UUID) -> None:
    obj = await get_session(db, user_id=user_id, session_id=session_id)
    obj.deleted_at = datetime.now(timezone.utc)
    await db.flush()


async def autogenerate_title_if_needed(db: AsyncSession, *, session: ChatSession) -> None:
    """If a session is still 'New chat' after its first user turn, generate a title."""
    if session.title and session.title != "New chat":
        return
    first_user_msg = (
        await db.execute(
            select(ChatMessage.content)
            .where(
                ChatMessage.session_id == session.id,
                ChatMessage.role == MessageRole.USER,
            )
            .order_by(ChatMessage.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not first_user_msg:
        return
    try:
        llm = build_chat_llm(streaming=False, temperature=0.2, max_tokens=24)
        chain = TITLE_PROMPT | llm
        result = await chain.ainvoke({"first_message": first_user_msg[:1000]})
        title = str(result.content).strip().strip('"').strip("'")[:60]
        if title:
            session.title = title
            await db.flush()
    except Exception:  # noqa: BLE001
        return
