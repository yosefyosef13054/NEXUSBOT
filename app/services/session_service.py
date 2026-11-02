from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import build_chat_llm
from app.ai.prompts.templates import TITLE_PROMPT
from app.ai.providers.registry import get_registry
from app.core.exceptions import NotFoundError
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.services.collection_service import ensure_collections

_UNSET: object = object()


async def _validated_meta(db: AsyncSession, *, org_id: UUID | None, meta: dict | None) -> dict:
    """`meta.collection_ids` is the chat's knowledge scope; make sure the ids are the org's."""
    meta = dict(meta or {})
    raw = meta.get("collection_ids")
    if raw:
        ids = [UUID(str(x)) for x in raw]
        await ensure_collections(db, org_id=org_id, ids=ids)
        meta["collection_ids"] = [str(i) for i in ids]
    return meta


async def _validated_assistant(
    db: AsyncSession, *, org_id: UUID | None, assistant_id: UUID | None
) -> UUID | None:
    if assistant_id is None or org_id is None:
        return None
    from app.services.assistant_service import get_assistant

    await get_assistant(db, org_id=org_id, assistant_id=assistant_id)
    return assistant_id


async def create_session(
    db: AsyncSession,
    *,
    user_id: UUID,
    title: str | None,
    system_prompt: str | None,
    model: str | None,
    org_id: UUID | None = None,
    assistant_id: UUID | None = None,
    meta: dict | None = None,
) -> ChatSession:
    obj = ChatSession(
        user_id=user_id,
        org_id=org_id,
        assistant_id=await _validated_assistant(db, org_id=org_id, assistant_id=assistant_id),
        title=title or "New chat",
        system_prompt=system_prompt,
        model=get_registry().validate(model),
        meta=await _validated_meta(db, org_id=org_id, meta=meta),
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
    db: AsyncSession, *, user_id: UUID, page: int, page_size: int, org_id: UUID | None = None
) -> tuple[list[ChatSession], int]:
    base = select(ChatSession).where(ChatSession.user_id == user_id, ChatSession.deleted_at.is_(None))
    if org_id is not None:
        base = base.where(ChatSession.org_id == org_id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                base.order_by(ChatSession.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def update_session(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    title: str | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
    assistant_id: UUID | None | object = _UNSET,
    meta: dict | None = None,
) -> ChatSession:
    obj = await get_session(db, user_id=user_id, session_id=session_id)
    if title is not None:
        obj.title = title
    if system_prompt is not None:
        obj.system_prompt = system_prompt
    if model is not None:
        obj.model = get_registry().validate(model)
    if assistant_id is not _UNSET:
        aid = assistant_id if isinstance(assistant_id, UUID) else None
        obj.assistant_id = await _validated_assistant(db, org_id=obj.org_id, assistant_id=aid)
    if meta is not None:
        merged = {**(obj.meta or {}), **meta}
        obj.meta = await _validated_meta(db, org_id=obj.org_id, meta=merged)
    await db.flush()
    # `updated_at` is set server-side via onupdate, so it's expired after the
    # UPDATE. Refresh now (in the async context) so response serialization
    # doesn't trigger a lazy load on a sync greenlet (MissingGreenlet -> 500).
    await db.refresh(obj)
    return obj


async def soft_delete_session(db: AsyncSession, *, user_id: UUID, session_id: UUID) -> None:
    obj = await get_session(db, user_id=user_id, session_id=session_id)
    obj.deleted_at = datetime.now(UTC)
    await db.flush()


def session_collection_ids(session: ChatSession) -> list[UUID] | None:
    raw = (session.meta or {}).get("collection_ids")
    if not raw:
        return None
    return [UUID(str(x)) for x in raw]


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
        llm = build_chat_llm(model=session.model, streaming=False, temperature=0.2, max_tokens=24)
        chain = TITLE_PROMPT | llm
        result = await chain.ainvoke({"first_message": first_user_msg[:1000]})
        title = str(result.content).strip().strip('"').strip("'")[:60]
        if title:
            session.title = title
            await db.flush()
    except Exception:  # noqa: BLE001
        return
