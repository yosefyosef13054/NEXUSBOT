from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.orchestrator import GenerationRequest, generate, maybe_summarize, stream
from app.core.exceptions import NotFoundError
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.tool_call import ToolCall
from app.schemas.chat import SearchResult
from app.services.session_service import autogenerate_title_if_needed, get_session
from app.services.usage_service import log_ai_usage
from app.utils.tokens import count_tokens


async def _persist_user_message(
    db: AsyncSession, *, session_id: UUID, content: str
) -> ChatMessage:
    msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.USER,
        content=content,
        tokens=count_tokens(content),
    )
    db.add(msg)
    await db.flush()
    return msg


async def _persist_assistant_message(
    db: AsyncSession,
    *,
    session_id: UUID,
    content: str,
    citations: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    model: str,
    latency_ms: int,
) -> ChatMessage:
    msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.ASSISTANT,
        content=content,
        tokens=count_tokens(content),
        meta={
            "model": model,
            "latency_ms": latency_ms,
            "citations": citations,
            "tool_calls": tool_calls,
        },
    )
    db.add(msg)
    await db.flush()
    return msg


async def _log_tool_calls(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    message_id: UUID,
    tool_calls: list[dict[str, Any]],
) -> None:
    for call in tool_calls:
        db.add(
            ToolCall(
                user_id=user_id,
                session_id=session_id,
                message_id=message_id,
                tool_name=str(call.get("tool", "unknown"))[:64],
                arguments=call.get("input") if isinstance(call.get("input"), dict) else {"raw": call.get("input")},
                result=str(call.get("output", ""))[:5000],
                status="ok",
            )
        )
    if tool_calls:
        await db.flush()


async def send_message(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    content: str,
    use_rag: bool,
    use_tools: bool,
    tool_names: list[str] | None,
    temperature: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)
    user_msg = await _persist_user_message(db, session_id=session_id, content=content)

    result = await generate(
        db,
        GenerationRequest(
            user_id=user_id,
            session=session_obj,
            user_input=content,
            use_rag=use_rag,
            use_tools=use_tools,
            tool_names=tool_names,
            temperature=temperature,
            max_tokens=max_tokens,
            streaming=False,
        ),
    )

    citation_payload = [
        {
            "document_id": str(c.document_id),
            "document_name": c.document_name,
            "chunk_index": c.chunk_index,
            "score": round(c.score, 4),
            "excerpt": c.content[:300],
        }
        for c in result.citations
    ]
    assistant_msg = await _persist_assistant_message(
        db,
        session_id=session_id,
        content=result.text,
        citations=citation_payload,
        tool_calls=result.tool_calls,
        model=result.model,
        latency_ms=result.latency_ms,
    )
    await _log_tool_calls(
        db,
        user_id=user_id,
        session_id=session_id,
        message_id=assistant_msg.id,
        tool_calls=result.tool_calls,
    )

    await log_ai_usage(
        db,
        user_id=user_id,
        session_id=session_id,
        operation="chat",
        model=result.model,
        prompt_tokens=user_msg.tokens,
        completion_tokens=assistant_msg.tokens,
        latency_ms=result.latency_ms,
    )

    await autogenerate_title_if_needed(db, session=session_obj)
    await maybe_summarize(db, session_id=session_id)
    await db.commit()

    return {
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        "citations": citation_payload,
        "tool_calls": result.tool_calls,
        "usage": {
            "prompt_tokens": user_msg.tokens,
            "completion_tokens": assistant_msg.tokens,
            "total_tokens": user_msg.tokens + assistant_msg.tokens,
        },
    }


async def stream_message(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    content: str,
    use_rag: bool,
    use_tools: bool,
    tool_names: list[str] | None,
    temperature: float | None,
    max_tokens: int | None,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator producing structured streaming events.

    Persists the user message immediately, streams tokens, and persists the final
    assistant message + usage on completion.
    """
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)
    user_msg = await _persist_user_message(db, session_id=session_id, content=content)
    await db.commit()

    yield {
        "type": "user_message",
        "data": {
            "id": str(user_msg.id),
            "session_id": str(session_id),
            "content": user_msg.content,
            "role": "user",
            "created_at": user_msg.created_at.isoformat(),
        },
    }

    final_text = ""
    final_latency = 0
    final_model = ""
    prompt_tokens = user_msg.tokens
    completion_tokens = 0
    citations_payload: list[dict[str, Any]] = []
    tool_calls_payload: list[dict[str, Any]] = []

    async for evt in stream(
        db,
        GenerationRequest(
            user_id=user_id,
            session=session_obj,
            user_input=content,
            use_rag=use_rag,
            use_tools=use_tools,
            tool_names=tool_names,
            temperature=temperature,
            max_tokens=max_tokens,
            streaming=True,
        ),
    ):
        if evt["type"] == "citations":
            citations_payload = evt["data"]
        elif evt["type"] == "tool_call" and evt["data"].get("phase") == "end":
            tool_calls_payload.append(
                {"tool": evt["data"].get("tool"), "input": evt["data"].get("input"), "output": evt["data"].get("output")}
            )
        elif evt["type"] == "done":
            final_text = evt["data"]["text"]
            final_latency = evt["data"]["latency_ms"]
            final_model = evt["data"]["model"]
            completion_tokens = int(evt["data"].get("completion_tokens") or count_tokens(final_text))
        yield evt

    assistant_msg = await _persist_assistant_message(
        db,
        session_id=session_id,
        content=final_text,
        citations=citations_payload,
        tool_calls=tool_calls_payload,
        model=final_model,
        latency_ms=final_latency,
    )
    await _log_tool_calls(
        db,
        user_id=user_id,
        session_id=session_id,
        message_id=assistant_msg.id,
        tool_calls=tool_calls_payload,
    )
    await log_ai_usage(
        db,
        user_id=user_id,
        session_id=session_id,
        operation="chat_stream",
        model=final_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=final_latency,
    )
    await autogenerate_title_if_needed(db, session=session_obj)
    await maybe_summarize(db, session_id=session_id)
    await db.commit()

    yield {
        "type": "assistant_message",
        "data": {
            "id": str(assistant_msg.id),
            "session_id": str(session_id),
            "role": "assistant",
            "content": final_text,
            "created_at": assistant_msg.created_at.isoformat(),
            "citations": citations_payload,
            "tool_calls": tool_calls_payload,
        },
    }


async def list_history(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    cursor: datetime | None,
    limit: int,
) -> tuple[list[ChatMessage], datetime | None]:
    await get_session(db, user_id=user_id, session_id=session_id)
    stmt = select(ChatMessage).where(
        ChatMessage.session_id == session_id,
        ChatMessage.deleted_at.is_(None),
    )
    if cursor is not None:
        stmt = stmt.where(ChatMessage.created_at < cursor)
    stmt = stmt.order_by(desc(ChatMessage.created_at)).limit(limit + 1)
    rows = (await db.execute(stmt)).scalars().all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].created_at if has_more and rows else None
    return list(reversed(rows)), next_cursor


async def regenerate_last(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    temperature: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)

    last_assistant = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == session_id,
                ChatMessage.role == MessageRole.ASSISTANT,
                ChatMessage.deleted_at.is_(None),
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_assistant is None:
        raise NotFoundError("Nothing to regenerate")
    last_user = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.session_id == session_id,
                ChatMessage.role == MessageRole.USER,
                ChatMessage.created_at < last_assistant.created_at,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_user is None:
        raise NotFoundError("No user turn found")

    last_assistant.deleted_at = func.now()
    await db.flush()

    result = await generate(
        db,
        GenerationRequest(
            user_id=user_id,
            session=session_obj,
            user_input=last_user.content,
            use_rag=True,
            use_tools=False,
            temperature=temperature,
            max_tokens=max_tokens,
            streaming=False,
        ),
    )
    citation_payload = [
        {
            "document_id": str(c.document_id),
            "document_name": c.document_name,
            "chunk_index": c.chunk_index,
            "score": round(c.score, 4),
            "excerpt": c.content[:300],
        }
        for c in result.citations
    ]
    new_msg = await _persist_assistant_message(
        db,
        session_id=session_id,
        content=result.text,
        citations=citation_payload,
        tool_calls=result.tool_calls,
        model=result.model,
        latency_ms=result.latency_ms,
    )
    await log_ai_usage(
        db,
        user_id=user_id,
        session_id=session_id,
        operation="chat_regenerate",
        model=result.model,
        prompt_tokens=count_tokens(last_user.content),
        completion_tokens=new_msg.tokens,
        latency_ms=result.latency_ms,
    )
    await db.commit()
    return {
        "assistant_message": new_msg,
        "citations": citation_payload,
        "tool_calls": result.tool_calls,
    }


async def search(
    db: AsyncSession, *, user_id: UUID, query: str, limit: int
) -> list[SearchResult]:
    pattern = f"%{query}%"
    stmt = (
        select(
            ChatSession.id.label("session_id"),
            ChatSession.title.label("session_title"),
            ChatMessage.id.label("message_id"),
            ChatMessage.role.label("role"),
            ChatMessage.content.label("snippet"),
            ChatMessage.created_at.label("created_at"),
        )
        .join(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .where(
            ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None),
            ChatMessage.deleted_at.is_(None),
            or_(ChatMessage.content.ilike(pattern), ChatSession.title.ilike(pattern)),
        )
        .order_by(desc(ChatMessage.created_at))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        SearchResult(
            session_id=r.session_id,
            session_title=r.session_title,
            message_id=r.message_id,
            role=r.role,
            snippet=(r.snippet[:240] + "…") if len(r.snippet) > 240 else r.snippet,
            created_at=r.created_at,
        )
        for r in rows
    ]
