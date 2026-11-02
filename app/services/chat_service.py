from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.orchestrator import (
    GenerationProgress,
    GenerationRequest,
    GenerationResult,
    citations_payload,
    generate,
    maybe_summarize,
    stream,
)
from app.ai.tools.registry import list_tool_names, unknown_tool_names
from app.core.background import run_in_background, spawn_detached
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logger import get_logger
from app.db.session import db_session
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.tool_call import ToolCall
from app.schemas.chat import SearchResult
from app.services.session_service import autogenerate_title_if_needed, get_session
from app.services.usage_service import log_ai_usage
from app.utils.tokens import count_tokens

logger = get_logger(__name__)


def _validate_tools(use_tools: bool, tool_names: list[str] | None) -> None:
    if not use_tools:
        return
    unknown = unknown_tool_names(tool_names)
    if unknown:
        raise ValidationError(
            "Unknown tools requested",
            details={"unknown": unknown, "available": list_tool_names()},
        )


async def _persist_user_message(db: AsyncSession, *, session_id: UUID, content: str) -> ChatMessage:
    # Explicit timestamp: `now()` is the *transaction* start in Postgres, so a user and
    # assistant message persisted in one transaction would otherwise tie on `created_at`.
    msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.USER,
        content=content,
        tokens=count_tokens(content),
        created_at=datetime.now(UTC),
    )
    db.add(msg)
    await db.flush()
    return msg


def _usage_payload(prompt: int, completion: int, embedding: int = 0) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "embedding_tokens": embedding,
        "total_tokens": prompt + completion + embedding,
    }


async def _persist_assistant_message(
    db: AsyncSession,
    *,
    session_id: UUID,
    content: str,
    citations: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    model: str,
    latency_ms: int,
    usage: dict[str, int],
    generation: dict[str, Any],
    status: str = "ok",
    interrupt_reason: str | None = None,
) -> ChatMessage:
    meta: dict[str, Any] = {
        "model": model,
        "latency_ms": latency_ms,
        "citations": citations,
        "tool_calls": tool_calls,
        "usage": usage,
        "generation": generation,
        "status": status,
    }
    if interrupt_reason:
        meta["interrupt_reason"] = interrupt_reason
    msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.ASSISTANT,
        content=content,
        tokens=usage.get("completion_tokens") or count_tokens(content),
        meta=meta,
        created_at=datetime.now(UTC),
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
        raw_input = call.get("input")
        db.add(
            ToolCall(
                user_id=user_id,
                session_id=session_id,
                message_id=message_id,
                tool_name=str(call.get("tool") or "unknown")[:64],
                arguments=raw_input if isinstance(raw_input, dict) else {"raw": raw_input},
                result=str(call.get("output", ""))[:5000],
                status="error" if call.get("error") else "ok",
                error=str(call["error"])[:2000] if call.get("error") else None,
                latency_ms=int(call.get("latency_ms") or 0),
            )
        )
    if tool_calls:
        await db.flush()


def _generation_meta(
    *,
    use_rag: bool,
    use_tools: bool,
    tool_names: list[str] | None,
    temperature: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    return {
        "use_rag": use_rag,
        "use_tools": use_tools,
        "tool_names": tool_names,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


async def post_turn_maintenance(session_id: UUID) -> None:
    """Title generation + summary compaction, after the client already has its answer."""
    async with db_session() as db:
        session_obj = await db.get(ChatSession, session_id)
        if session_obj is None or session_obj.deleted_at is not None:
            return
        await autogenerate_title_if_needed(db, session=session_obj)
        await maybe_summarize(db, session_id=session_id)


async def _schedule_maintenance(session_id: UUID) -> None:
    await run_in_background(f"post_turn_maintenance:{session_id}", post_turn_maintenance(session_id))


async def _finish_turn(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    text: str,
    citations: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    model: str,
    latency_ms: int,
    prompt_tokens: int,
    completion_tokens: int,
    embedding_tokens: int,
    generation: dict[str, Any],
    operation: str,
    status: str = "ok",
    interrupt_reason: str | None = None,
) -> tuple[ChatMessage, dict[str, int]]:
    usage = _usage_payload(prompt_tokens, completion_tokens, embedding_tokens)
    assistant_msg = await _persist_assistant_message(
        db,
        session_id=session_id,
        content=text,
        citations=citations,
        tool_calls=tool_calls,
        model=model,
        latency_ms=latency_ms,
        usage=usage,
        generation=generation,
        status=status,
        interrupt_reason=interrupt_reason,
    )
    await _log_tool_calls(
        db, user_id=user_id, session_id=session_id, message_id=assistant_msg.id, tool_calls=tool_calls
    )
    await log_ai_usage(
        db,
        user_id=user_id,
        session_id=session_id,
        operation=operation,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        meta={
            "message_id": str(assistant_msg.id),
            "embedding_tokens": embedding_tokens,
            "status": status,
            "tool_calls": len(tool_calls),
        },
    )
    return assistant_msg, usage


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
    request_id: str | None = None,
) -> dict[str, Any]:
    _validate_tools(use_tools, tool_names)
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)
    user_msg = await _persist_user_message(db, session_id=session_id, content=content)
    generation = _generation_meta(
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    result: GenerationResult = await generate(
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
            request_id=request_id,
        ),
    )

    citation_payload = citations_payload(result.citations)
    assistant_msg, usage = await _finish_turn(
        db,
        user_id=user_id,
        session_id=session_id,
        text=result.text,
        citations=citation_payload,
        tool_calls=result.tool_calls,
        model=result.model,
        latency_ms=result.latency_ms,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        embedding_tokens=result.embedding_tokens,
        generation=generation,
        operation="chat",
    )
    await db.commit()
    await _schedule_maintenance(session_id)

    return {
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        "citations": citation_payload,
        "tool_calls": result.tool_calls,
        "usage": usage,
    }


async def _persist_interrupted_turn(
    *,
    user_id: UUID,
    session_id: UUID,
    progress: GenerationProgress,
    generation: dict[str, Any],
) -> None:
    """Persist whatever a cancelled generation produced, on a fresh DB session.

    Called when the client disconnected mid-stream, so the request session may already be
    torn down; shielded by the caller so a second cancellation cannot interrupt it.
    """
    async with db_session() as db:
        await _finish_turn(
            db,
            user_id=user_id,
            session_id=session_id,
            text=progress.text,
            citations=progress.citations,
            tool_calls=progress.tool_calls,
            model=progress.model,
            latency_ms=progress.latency_ms,
            prompt_tokens=progress.prompt_tokens or count_tokens(progress.text),
            completion_tokens=progress.completion_tokens or count_tokens(progress.text),
            embedding_tokens=progress.embedding_tokens,
            generation=generation,
            operation="chat_stream",
            status="interrupted",
            interrupt_reason=progress.interrupt_reason or "disconnect",
        )
    await _schedule_maintenance(session_id)
    logger.info(
        "generation_interrupted_persisted",
        session_id=str(session_id),
        reason=progress.interrupt_reason,
        chars=len(progress.text),
    )


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
    request_id: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator producing structured streaming events.

    Persists the user message immediately, streams tokens, and persists the final
    assistant message + usage on completion — including when the stream is stopped or the
    client disconnects, in which case the partial text is saved with `status=interrupted`.
    """
    _validate_tools(use_tools, tool_names)
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)
    user_msg = await _persist_user_message(db, session_id=session_id, content=content)
    await db.commit()
    generation = _generation_meta(
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        temperature=temperature,
        max_tokens=max_tokens,
    )

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

    req = GenerationRequest(
        user_id=user_id,
        session=session_obj,
        user_input=content,
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=True,
        request_id=request_id,
        stop_event=stop_event,
    )
    p = req.progress

    try:
        async for evt in stream(db, req):
            yield evt
    except (asyncio.CancelledError, GeneratorExit) as exc:
        # The consumer is gone. We may be inside a cancelled scope where every `await`
        # re-raises, so persistence runs as a detached task on the loop; during a
        # regular `aclose()` (GeneratorExit) it is safe to wait for it.
        task = None
        if not p.finished and (p.text or p.tool_calls):
            task = spawn_detached(
                f"persist_interrupted:{session_id}",
                _persist_interrupted_turn(
                    user_id=user_id, session_id=session_id, progress=p, generation=generation
                ),
            )
        if isinstance(exc, GeneratorExit) and task is not None:
            await task
        raise

    status = "error" if p.errored else ("interrupted" if p.interrupted else "ok")
    assistant_msg, usage = await _finish_turn(
        db,
        user_id=user_id,
        session_id=session_id,
        text=p.text,
        citations=p.citations,
        tool_calls=p.tool_calls,
        model=p.model,
        latency_ms=p.latency_ms,
        prompt_tokens=p.prompt_tokens,
        completion_tokens=p.completion_tokens,
        embedding_tokens=p.embedding_tokens,
        generation=generation,
        operation="chat_stream",
        status=status,
        interrupt_reason=p.interrupt_reason,
    )
    await db.commit()
    await _schedule_maintenance(session_id)

    yield {
        "type": "assistant_message",
        "data": {
            "id": str(assistant_msg.id),
            "session_id": str(session_id),
            "role": "assistant",
            "content": p.text,
            "created_at": assistant_msg.created_at.isoformat(),
            "citations": p.citations,
            "tool_calls": p.tool_calls,
            "usage": usage,
            "interrupted": p.interrupted,
            "status": status,
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
    request_id: str | None = None,
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
                ChatMessage.deleted_at.is_(None),
                ChatMessage.created_at <= last_assistant.created_at,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_user is None:
        raise NotFoundError("No user turn found")

    # Reproduce the original turn's settings (RAG/tools) instead of guessing.
    previous = (last_assistant.meta or {}).get("generation") or {}
    use_rag = bool(previous.get("use_rag", True))
    use_tools = bool(previous.get("use_tools", False))
    tool_names = previous.get("tool_names")
    generation = _generation_meta(
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    last_assistant.deleted_at = func.now()
    await db.flush()

    result = await generate(
        db,
        GenerationRequest(
            user_id=user_id,
            session=session_obj,
            user_input=last_user.content,
            use_rag=use_rag,
            use_tools=use_tools,
            tool_names=tool_names,
            temperature=temperature,
            max_tokens=max_tokens,
            streaming=False,
            request_id=request_id,
        ),
    )
    citation_payload = citations_payload(result.citations)
    new_msg, usage = await _finish_turn(
        db,
        user_id=user_id,
        session_id=session_id,
        text=result.text,
        citations=citation_payload,
        tool_calls=result.tool_calls,
        model=result.model,
        latency_ms=result.latency_ms,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        embedding_tokens=result.embedding_tokens,
        generation=generation,
        operation="chat_regenerate",
    )
    await db.commit()
    await _schedule_maintenance(session_id)
    return {
        "assistant_message": new_msg,
        "citations": citation_payload,
        "tool_calls": result.tool_calls,
        "usage": usage,
    }


async def search(db: AsyncSession, *, user_id: UUID, query: str, limit: int) -> list[SearchResult]:
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
