from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.orchestrator import (
    STATUS_AWAITING_APPROVAL,
    GenerationProgress,
    GenerationRequest,
    GenerationResult,
    citations_payload,
    generate,
    maybe_summarize,
    resume_stream,
    stream,
)
from app.ai.providers.registry import get_registry
from app.ai.tools.registry import list_tool_names, unknown_tool_names
from app.config import settings
from app.core.background import run_in_background, spawn_detached
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logger import get_logger
from app.core.metrics import APPROVALS
from app.db.session import db_session
from app.models.approval import Approval, ApprovalStatus
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.tool_call import ToolCall
from app.schemas.chat import SearchResult
from app.services.collection_service import ensure_collections
from app.services.session_service import autogenerate_title_if_needed, get_session, session_collection_ids
from app.services.usage_service import log_ai_usage
from app.utils.tokens import count_tokens

logger = get_logger(__name__)

TOOL_MESSAGE_MAX_CHARS = 8000


# --------------------------------------------------------------------------- validation


def _validate_tools(use_tools: bool, tool_names: list[str] | None) -> None:
    if not use_tools:
        return
    unknown = unknown_tool_names(tool_names)
    if unknown:
        raise ValidationError(
            "Unknown tools requested",
            details={"unknown": unknown, "available": list_tool_names()},
        )


async def _effective_collections(
    db: AsyncSession, *, user_id: UUID, session_obj: ChatSession, collection_ids: list[UUID] | None
) -> list[UUID] | None:
    """Per-turn scope wins over the session default; both are validated as the user's."""
    if collection_ids is not None:
        await ensure_collections(db, user_id=user_id, ids=collection_ids)
        return list(collection_ids) or None
    return session_collection_ids(session_obj)


# --------------------------------------------------------------------------- persistence


def _now() -> datetime:
    return datetime.now(UTC)


async def _persist_user_message(db: AsyncSession, *, session_id: UUID, content: str) -> ChatMessage:
    # Explicit timestamp: `now()` is the *transaction* start in Postgres, so a user and
    # assistant message persisted in one transaction would otherwise tie on `created_at`.
    msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.USER,
        content=content,
        tokens=count_tokens(content),
        created_at=_now(),
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


def _merge_usage(existing: dict[str, Any] | None, more: dict[str, int]) -> dict[str, int]:
    base = dict(existing or {})
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "embedding_tokens"):
        out[key] = int(base.get(key) or 0) + int(more.get(key) or 0)
    out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"] + out["embedding_tokens"]
    return out


def _generation_meta(
    *,
    use_rag: bool,
    use_tools: bool,
    tool_names: list[str] | None,
    model: str | None,
    collection_ids: list[UUID] | None,
    temperature: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    return {
        "use_rag": use_rag,
        "use_tools": use_tools,
        "tool_names": tool_names,
        "model": model,
        "collection_ids": [str(c) for c in collection_ids] if collection_ids else None,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


async def _persist_tool_messages(
    db: AsyncSession, *, session_id: UUID, tool_runs: list[dict[str, Any]], existing_call_ids: set[str]
) -> list[ChatMessage]:
    """One `role=tool` message per executed tool call, placed before the assistant reply."""
    out: list[ChatMessage] = []
    for run in tool_runs:
        call_id = str(run.get("call_id") or "")
        if call_id and call_id in existing_call_ids:
            continue
        output = str(run.get("output") or "")[:TOOL_MESSAGE_MAX_CHARS]
        msg = ChatMessage(
            session_id=session_id,
            role=MessageRole.TOOL,
            content=output,
            tokens=count_tokens(output),
            created_at=_now(),
            meta={
                "tool": run.get("tool"),
                "call_id": call_id or None,
                "input": run.get("input") or {},
                "status": run.get("status") or "ok",
                "latency_ms": int(run.get("latency_ms") or 0),
            },
        )
        db.add(msg)
        out.append(msg)
        existing_call_ids.add(call_id)
    if out:
        await db.flush()
    return out


async def _existing_tool_call_ids(db: AsyncSession, *, session_id: UUID, since: datetime | None) -> set[str]:
    stmt = select(ChatMessage.meta).where(
        ChatMessage.session_id == session_id, ChatMessage.role == MessageRole.TOOL
    )
    if since is not None:
        stmt = stmt.where(ChatMessage.created_at >= since)
    rows = (await db.execute(stmt)).scalars().all()
    return {str(m.get("call_id")) for m in rows if m and m.get("call_id")}


async def _log_tool_calls(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    message_id: UUID,
    tool_runs: list[dict[str, Any]],
) -> None:
    for run in tool_runs:
        raw_input = run.get("input")
        status = run.get("status") or "ok"
        db.add(
            ToolCall(
                user_id=user_id,
                session_id=session_id,
                message_id=message_id,
                tool_name=str(run.get("tool") or "unknown")[:64],
                arguments=raw_input if isinstance(raw_input, dict) else {"raw": raw_input},
                result=str(run.get("output", ""))[:5000],
                status=status if status in ("ok", "error", "denied") else "ok",
                error=str(run.get("output"))[:2000] if status == "error" else None,
                latency_ms=int(run.get("latency_ms") or 0),
            )
        )
    if tool_runs:
        await db.flush()


async def _persist_assistant_message(
    db: AsyncSession,
    *,
    session_id: UUID,
    content: str,
    citations: list[dict[str, Any]],
    tool_runs: list[dict[str, Any]],
    model: str,
    provider: str,
    latency_ms: int,
    usage: dict[str, int],
    generation: dict[str, Any],
    status: str = "ok",
    interrupt_reason: str | None = None,
    approval: dict[str, Any] | None = None,
    placeholder: ChatMessage | None = None,
) -> ChatMessage:
    meta: dict[str, Any] = {
        **((placeholder.meta if placeholder else None) or {}),
        "model": model,
        "provider": provider,
        "latency_ms": latency_ms,
        "citations": citations,
        "tool_calls": tool_runs,
        "usage": usage,
        "generation": generation,
        "status": status,
    }
    meta.pop("interrupt_reason", None)
    if interrupt_reason:
        meta["interrupt_reason"] = interrupt_reason
    if approval is not None:
        meta["approval"] = approval
    else:
        meta.pop("approval", None)
    if placeholder is not None:
        placeholder.content = content
        placeholder.tokens = usage.get("completion_tokens") or count_tokens(content)
        placeholder.meta = meta
        if status != STATUS_AWAITING_APPROVAL:
            # The reply is complete now: order it after the tool messages persisted during
            # the resumed run (history is user → tool… → assistant).
            placeholder.created_at = _now()
        await db.flush()
        return placeholder
    msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.ASSISTANT,
        content=content,
        tokens=usage.get("completion_tokens") or count_tokens(content),
        meta=meta,
        created_at=_now(),
    )
    db.add(msg)
    await db.flush()
    return msg


def approval_read(row: Approval) -> dict[str, Any]:
    return {
        "approval_id": str(row.id),
        "session_id": str(row.session_id),
        "message_id": str(row.message_id) if row.message_id else None,
        "status": row.status.value if isinstance(row.status, ApprovalStatus) else str(row.status),
        "expires_at": row.expires_at.isoformat(),
        "tool_calls": row.tool_calls or [],
    }


async def _create_approval(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    message_id: UUID,
    thread_id: str,
    payload: dict[str, Any],
    generation: dict[str, Any],
) -> Approval:
    row = Approval(
        user_id=user_id,
        session_id=session_id,
        message_id=message_id,
        thread_id=thread_id,
        status=ApprovalStatus.PENDING,
        tool_calls=list(payload.get("tool_calls") or []),
        generation=generation,
        expires_at=_now() + timedelta(minutes=settings.APPROVAL_TTL_MINUTES),
    )
    db.add(row)
    await db.flush()
    return row


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
    tool_runs: list[dict[str, Any]],
    model: str,
    provider: str,
    latency_ms: int,
    prompt_tokens: int,
    completion_tokens: int,
    embedding_tokens: int,
    generation: dict[str, Any],
    operation: str,
    status: str = "ok",
    interrupt_reason: str | None = None,
    approval: dict[str, Any] | None = None,
    placeholder: ChatMessage | None = None,
) -> tuple[ChatMessage, dict[str, int], list[ChatMessage]]:
    """Persist everything a turn produced: tool messages, the assistant message (new or the
    approval placeholder updated in place), the tool-call audit rows and the usage row."""
    usage = _usage_payload(prompt_tokens, completion_tokens, embedding_tokens)
    total_usage = _merge_usage((placeholder.meta or {}).get("usage") if placeholder else None, usage)

    tool_msgs: list[ChatMessage] = []
    if status != STATUS_AWAITING_APPROVAL:
        existing = await _existing_tool_call_ids(
            db, session_id=session_id, since=placeholder.created_at if placeholder else None
        )
        tool_msgs = await _persist_tool_messages(
            db, session_id=session_id, tool_runs=tool_runs, existing_call_ids=existing
        )

    assistant_msg = await _persist_assistant_message(
        db,
        session_id=session_id,
        content=text,
        citations=citations,
        tool_runs=tool_runs,
        model=model,
        provider=provider,
        latency_ms=latency_ms,
        usage=total_usage,
        generation=generation,
        status=status,
        interrupt_reason=interrupt_reason,
        approval=approval,
        placeholder=placeholder,
    )
    if status != STATUS_AWAITING_APPROVAL:
        await _log_tool_calls(
            db, user_id=user_id, session_id=session_id, message_id=assistant_msg.id, tool_runs=tool_runs
        )
    await log_ai_usage(
        db,
        user_id=user_id,
        session_id=session_id,
        operation=operation,
        model=model,
        provider=provider or "openai",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        meta={
            "message_id": str(assistant_msg.id),
            "embedding_tokens": embedding_tokens,
            "status": status,
            "tool_calls": len(tool_runs),
        },
    )
    return assistant_msg, total_usage, tool_msgs


# --------------------------------------------------------------------------- blocking


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
    model: str | None = None,
    collection_ids: list[UUID] | None = None,
    user_default_model: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    _validate_tools(use_tools, tool_names)
    get_registry().validate(model)
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)
    scope = await _effective_collections(
        db, user_id=user_id, session_obj=session_obj, collection_ids=collection_ids
    )
    user_msg = await _persist_user_message(db, session_id=session_id, content=content)

    req = GenerationRequest(
        user_id=user_id,
        session=session_obj,
        user_input=content,
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        model=model,
        user_default_model=user_default_model,
        collection_ids=scope,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=False,
        request_id=request_id,
    )
    result: GenerationResult = await generate(db, req)
    generation = _generation_meta(
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        model=result.model,
        collection_ids=scope,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    citation_payload = citations_payload(result.citations)
    approval_payload: dict[str, Any] | None = None
    if result.approval:
        assistant_msg, usage, tool_msgs = await _finish_turn(
            db,
            user_id=user_id,
            session_id=session_id,
            text=result.text,
            citations=citation_payload,
            tool_runs=result.tool_runs,
            model=result.model,
            provider=result.provider,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            embedding_tokens=result.embedding_tokens,
            generation=generation,
            operation="chat",
            status=STATUS_AWAITING_APPROVAL,
            approval=result.approval,
        )
        row = await _create_approval(
            db,
            user_id=user_id,
            session_id=session_id,
            message_id=assistant_msg.id,
            thread_id=result.thread_id,
            payload=result.approval,
            generation=generation,
        )
        approval_payload = approval_read(row)
        assistant_msg.meta = {**assistant_msg.meta, "approval": approval_payload}
        await db.flush()
    else:
        assistant_msg, usage, tool_msgs = await _finish_turn(
            db,
            user_id=user_id,
            session_id=session_id,
            text=result.text,
            citations=citation_payload,
            tool_runs=result.tool_runs,
            model=result.model,
            provider=result.provider,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            embedding_tokens=result.embedding_tokens,
            generation=generation,
            operation="chat",
        )
    await db.commit()
    if approval_payload is None:
        await _schedule_maintenance(session_id)

    return {
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        "tool_messages": tool_msgs,
        "citations": citation_payload,
        "tool_calls": result.tool_runs,
        "usage": usage,
        "approval": approval_payload,
    }


# --------------------------------------------------------------------------- streaming


async def _persist_interrupted_turn(
    *,
    user_id: UUID,
    session_id: UUID,
    progress: GenerationProgress,
    generation: dict[str, Any],
    placeholder_id: UUID | None = None,
) -> None:
    """Persist whatever a cancelled generation produced, on a fresh DB session.

    Called when the client disconnected mid-stream, so the request session may already be
    torn down; runs detached so a second cancellation cannot interrupt it.
    """
    async with db_session() as db:
        placeholder = await db.get(ChatMessage, placeholder_id) if placeholder_id else None
        await _finish_turn(
            db,
            user_id=user_id,
            session_id=session_id,
            text=progress.text,
            citations=progress.citations,
            tool_runs=progress.tool_runs or progress.tool_calls,
            model=progress.model,
            provider=progress.provider,
            latency_ms=progress.latency_ms,
            prompt_tokens=progress.prompt_tokens or count_tokens(progress.text),
            completion_tokens=progress.completion_tokens or count_tokens(progress.text),
            embedding_tokens=progress.embedding_tokens,
            generation=generation,
            operation="chat_stream",
            status="interrupted",
            interrupt_reason=progress.interrupt_reason or "disconnect",
            placeholder=placeholder,
        )
    await _schedule_maintenance(session_id)
    logger.info(
        "generation_interrupted_persisted",
        session_id=str(session_id),
        reason=progress.interrupt_reason,
        chars=len(progress.text),
    )


def _assistant_event(
    msg: ChatMessage, p: GenerationProgress, usage: dict[str, int], status: str
) -> dict[str, Any]:
    return {
        "type": "assistant_message",
        "data": {
            "id": str(msg.id),
            "session_id": str(msg.session_id),
            "role": "assistant",
            "content": msg.content,
            "created_at": msg.created_at.isoformat(),
            "citations": (msg.meta or {}).get("citations", []),
            "tool_calls": (msg.meta or {}).get("tool_calls", []),
            "usage": usage,
            "model": p.model,
            "interrupted": p.interrupted,
            "status": status,
            "approval": (msg.meta or {}).get("approval"),
        },
    }


async def _run_streaming_turn(
    db: AsyncSession,
    *,
    req: GenerationRequest,
    events: AsyncIterator[dict[str, Any]],
    generation: dict[str, Any],
    operation: str,
    placeholder: ChatMessage | None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Republish orchestrator events, turning `approval_required` into a persisted
    approval and finishing the turn (new message or placeholder updated in place)."""
    p = req.progress
    user_id, session_id = req.user_id, req.session.id

    try:
        async for evt in events:
            if evt["type"] == "approval_required":
                # Persist a placeholder (or reuse the one we are resuming) and the approval
                # so the decision can arrive later, from any worker.
                placeholder, _usage, _ = await _finish_turn(
                    db,
                    user_id=user_id,
                    session_id=session_id,
                    text=p.text,
                    citations=p.citations,
                    tool_runs=p.tool_runs,
                    model=p.model,
                    provider=p.provider,
                    latency_ms=p.latency_ms,
                    prompt_tokens=p.prompt_tokens,
                    completion_tokens=p.completion_tokens,
                    embedding_tokens=p.embedding_tokens,
                    generation=generation,
                    operation=operation,
                    status=STATUS_AWAITING_APPROVAL,
                    approval=evt["data"],
                    placeholder=placeholder,
                )
                row = await _create_approval(
                    db,
                    user_id=user_id,
                    session_id=session_id,
                    message_id=placeholder.id,
                    thread_id=req.thread_id,
                    payload=evt["data"],
                    generation=generation,
                )
                placeholder.meta = {**placeholder.meta, "approval": approval_read(row)}
                await db.commit()
                evt = {"type": "approval_required", "data": approval_read(row)}
            yield evt
    except (asyncio.CancelledError, GeneratorExit) as exc:
        task = None
        if not p.finished and (p.text or p.tool_calls or p.tool_runs):
            task = spawn_detached(
                f"persist_interrupted:{session_id}",
                _persist_interrupted_turn(
                    user_id=user_id,
                    session_id=session_id,
                    progress=p,
                    generation=generation,
                    placeholder_id=placeholder.id if placeholder else None,
                ),
            )
        if isinstance(exc, GeneratorExit) and task is not None:
            await task
        raise

    if p.approval and placeholder is not None:
        # The turn is parked: the placeholder already holds the partial state.
        await db.refresh(placeholder)
        await db.commit()
        yield _assistant_event(
            placeholder, p, (placeholder.meta or {}).get("usage", {}), STATUS_AWAITING_APPROVAL
        )
        return

    status = p.status
    assistant_msg, usage, _tool_msgs = await _finish_turn(
        db,
        user_id=user_id,
        session_id=session_id,
        text=p.text,
        citations=p.citations
        if not placeholder
        else ((placeholder.meta or {}).get("citations") or p.citations),
        tool_runs=p.tool_runs or p.tool_calls,
        model=p.model,
        provider=p.provider,
        latency_ms=p.latency_ms,
        prompt_tokens=p.prompt_tokens,
        completion_tokens=p.completion_tokens,
        embedding_tokens=p.embedding_tokens,
        generation=generation,
        operation=operation,
        status=status,
        interrupt_reason=p.interrupt_reason,
        placeholder=placeholder,
    )
    await db.commit()
    await _schedule_maintenance(session_id)
    yield _assistant_event(assistant_msg, p, usage, status)


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
    model: str | None = None,
    collection_ids: list[UUID] | None = None,
    user_default_model: str | None = None,
    request_id: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator producing structured streaming events.

    Persists the user message immediately, streams tokens, and persists the final
    assistant message + usage on completion — including when the stream is stopped or the
    client disconnects, in which case the partial text is saved with `status=interrupted`.
    """
    _validate_tools(use_tools, tool_names)
    get_registry().validate(model)
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)
    scope = await _effective_collections(
        db, user_id=user_id, session_obj=session_obj, collection_ids=collection_ids
    )
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

    req = GenerationRequest(
        user_id=user_id,
        session=session_obj,
        user_input=content,
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        model=model,
        user_default_model=user_default_model,
        collection_ids=scope,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=True,
        request_id=request_id,
        stop_event=stop_event,
    )
    generation = _generation_meta(
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        model=None,  # filled once the spec is resolved
        collection_ids=scope,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    events = stream(db, req)

    async def _events() -> AsyncIterator[dict[str, Any]]:
        async for evt in events:
            if not generation.get("model") and req.progress.model:
                generation["model"] = req.progress.model
            yield evt

    turn = _run_streaming_turn(
        db, req=req, events=_events(), generation=generation, operation="chat_stream", placeholder=None
    )
    try:
        async for evt in turn:
            yield evt
    finally:
        # Closing this generator must close the inner one *now* (not at garbage collection)
        # so partial replies are persisted before the caller moves on.
        await turn.aclose()


# --------------------------------------------------------------------------- approvals


async def _expire_if_needed(row: Approval) -> None:
    if row.status == ApprovalStatus.PENDING and row.expires_at <= _now():
        row.status = ApprovalStatus.EXPIRED
        APPROVALS.labels("expired").inc()


async def list_approvals(
    db: AsyncSession, *, user_id: UUID, session_id: UUID, status: str | None = None
) -> list[Approval]:
    await get_session(db, user_id=user_id, session_id=session_id)
    stmt = select(Approval).where(Approval.user_id == user_id, Approval.session_id == session_id)
    if status:
        stmt = stmt.where(Approval.status == ApprovalStatus(status))
    rows = (await db.execute(stmt.order_by(Approval.created_at.desc()))).scalars().all()
    changed = False
    for row in rows:
        before = row.status
        await _expire_if_needed(row)
        changed = changed or before != row.status
    if changed:
        await db.commit()
    if status:
        rows = [r for r in rows if r.status == ApprovalStatus(status)]
    return list(rows)


async def get_pending_approval(
    db: AsyncSession, *, user_id: UUID, session_id: UUID, approval_id: UUID
) -> Approval:
    """404 for unknown/foreign approvals, 409 when already resolved or expired.

    Called by the endpoint *before* the SSE response starts so clients get real HTTP
    status codes instead of an error frame inside a 200 stream.
    """
    row = await db.get(Approval, approval_id)
    if row is None or row.user_id != user_id or row.session_id != session_id:
        raise NotFoundError("Approval not found")
    await _expire_if_needed(row)
    if row.status != ApprovalStatus.PENDING:
        await db.commit()
        raise ConflictError(f"Approval already {row.status.value}", details={"status": row.status.value})
    return row


async def resolve_approval(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    approval_id: UUID,
    decision: str,
    tool_call_ids: list[str] | None = None,
    request_id: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Apply the user's decision and stream the rest of the agent turn."""
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)
    row = await get_pending_approval(db, user_id=user_id, session_id=session_id, approval_id=approval_id)

    placeholder = await db.get(ChatMessage, row.message_id) if row.message_id else None
    gen = row.generation or {}
    raw_collections = gen.get("collection_ids")
    req = GenerationRequest(
        user_id=user_id,
        session=session_obj,
        user_input="",
        use_rag=bool(gen.get("use_rag", True)),
        use_tools=True,
        tool_names=gen.get("tool_names"),
        model=gen.get("model"),
        collection_ids=[UUID(str(c)) for c in raw_collections] if raw_collections else None,
        temperature=gen.get("temperature"),
        max_tokens=gen.get("max_tokens"),
        streaming=True,
        request_id=request_id,
        stop_event=stop_event,
        thread_id=row.thread_id,
    )
    row.status = ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.DENIED
    row.decided_at = _now()
    row.decision = {"decision": decision, "tool_call_ids": tool_call_ids}
    await db.commit()
    APPROVALS.labels(row.status.value).inc()

    events = resume_stream(db, req, {"decision": decision, "tool_call_ids": tool_call_ids})
    turn = _run_streaming_turn(
        db, req=req, events=events, generation=dict(gen), operation="chat_stream", placeholder=placeholder
    )
    try:
        async for evt in turn:
            yield evt
    finally:
        await turn.aclose()


# --------------------------------------------------------------------------- history


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
    user_default_model: str | None = None,
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

    # Reproduce the original turn's settings (RAG/tools/model/scope) instead of guessing.
    previous = (last_assistant.meta or {}).get("generation") or {}
    use_rag = bool(previous.get("use_rag", True))
    use_tools = bool(previous.get("use_tools", False))
    tool_names = previous.get("tool_names")
    model = previous.get("model") if get_registry().is_available(previous.get("model")) else None
    raw_collections = previous.get("collection_ids")
    scope = [UUID(str(c)) for c in raw_collections] if raw_collections else None

    # Retire the old reply and the tool messages that belonged to it.
    now = _now()
    last_assistant.deleted_at = now
    old_tools = (
        (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.role == MessageRole.TOOL,
                    ChatMessage.deleted_at.is_(None),
                    ChatMessage.created_at > last_user.created_at,
                    ChatMessage.created_at <= last_assistant.created_at,
                )
            )
        )
        .scalars()
        .all()
    )
    for m in old_tools:
        m.deleted_at = now
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
            model=model,
            user_default_model=user_default_model,
            collection_ids=scope,
            temperature=temperature,
            max_tokens=max_tokens,
            streaming=False,
            request_id=request_id,
        ),
    )
    generation = _generation_meta(
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        model=result.model,
        collection_ids=scope,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    citation_payload = citations_payload(result.citations)
    status = STATUS_AWAITING_APPROVAL if result.approval else "ok"
    new_msg, usage, tool_msgs = await _finish_turn(
        db,
        user_id=user_id,
        session_id=session_id,
        text=result.text,
        citations=citation_payload,
        tool_runs=result.tool_runs,
        model=result.model,
        provider=result.provider,
        latency_ms=result.latency_ms,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        embedding_tokens=result.embedding_tokens,
        generation=generation,
        operation="chat_regenerate",
        status=status,
        approval=result.approval,
    )
    if result.approval:
        row = await _create_approval(
            db,
            user_id=user_id,
            session_id=session_id,
            message_id=new_msg.id,
            thread_id=result.thread_id,
            payload=result.approval,
            generation=generation,
        )
        new_msg.meta = {**new_msg.meta, "approval": approval_read(row)}
        await db.flush()
    await db.commit()
    if not result.approval:
        await _schedule_maintenance(session_id)
    return {
        "assistant_message": new_msg,
        "tool_messages": tool_msgs,
        "citations": citation_payload,
        "tool_calls": result.tool_runs,
        "usage": usage,
    }


# --------------------------------------------------------------------------- search


def _snippet(text: str) -> str:
    return (text[:240] + "…") if len(text) > 240 else text


async def search(db: AsyncSession, *, user_id: UUID, query: str, limit: int) -> list[SearchResult]:
    """Full-text search over the user's messages (stemmed, ranked, highlighted), falling
    back to substring matching for queries the text parser cannot use."""
    tsquery = func.plainto_tsquery("english", query)
    rank = func.ts_rank_cd(ChatMessage.content_tsv, tsquery).label("rank")
    headline = func.ts_headline(
        "english",
        ChatMessage.content,
        tsquery,
        "StartSel=<mark>, StopSel=</mark>, MaxWords=35, MinWords=15, MaxFragments=2",
    ).label("highlight")
    base_cols = (
        ChatSession.id.label("session_id"),
        ChatSession.title.label("session_title"),
        ChatMessage.id.label("message_id"),
        ChatMessage.role.label("role"),
        ChatMessage.content.label("snippet"),
        ChatMessage.created_at.label("created_at"),
    )
    stmt = (
        select(*base_cols, rank, headline)
        .join(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .where(
            ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None),
            ChatMessage.deleted_at.is_(None),
            ChatMessage.content_tsv.op("@@")(tsquery),
        )
        .order_by(desc(rank), desc(ChatMessage.created_at))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    results = [
        SearchResult(
            session_id=r.session_id,
            session_title=r.session_title,
            message_id=r.message_id,
            role=r.role,
            snippet=_snippet(r.snippet),
            highlight=r.highlight,
            rank=float(r.rank or 0.0),
            created_at=r.created_at,
        )
        for r in rows
    ]
    if results:
        return results

    pattern = f"%{query}%"
    stmt = (
        select(*base_cols)
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
            snippet=_snippet(r.snippet),
            created_at=r.created_at,
        )
        for r in rows
    ]
