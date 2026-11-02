from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, AsyncIterator, Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import idempotency
from app.core.background import spawn_detached
from app.core.dependencies import Principal, current_principal
from app.core.exceptions import AppException, NotFoundError, request_id_of
from app.core.rate_limit import rate_limit_dependency
from app.db.session import AsyncSessionLocal, get_db
from app.schemas.chat import (
    ApprovalDecisionRequest,
    ApprovalListResponse,
    ApprovalRead,
    BackgroundSendResponse,
    BranchRequest,
    GenerationActiveResponse,
    HistoryResponse,
    MessageRead,
    RegenerateRequest,
    RegenerateResponse,
    SearchRequest,
    SearchResult,
    SendMessageRequest,
    SendMessageResponse,
    SiblingsResponse,
)
from app.schemas.common import OkResponse
from app.schemas.usage import FeedbackRead, FeedbackRequest
from app.services import chat_service, feedback_service
from app.services.chat_service import TurnScope
from app.services.session_service import get_session

router = APIRouter(prefix="/sessions/{session_id}/messages", tags=["chat"])
approvals_router = APIRouter(prefix="/sessions/{session_id}/approvals", tags=["chat"])
generations_router = APIRouter(prefix="/sessions/{session_id}", tags=["chat"])
search_router = APIRouter(prefix="/search", tags=["chat"])

IdempotencyKey = Annotated[str | None, Header(alias=idempotency.HEADER, max_length=128)]


def _error_event(e: AppException) -> dict:
    return {"type": "error", "data": {"code": e.code, "message": e.message, "details": e.details}}


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _frame(evt: dict) -> str:
    """One SSE frame; the generation log's `seq` becomes the `id:` so clients can resume."""
    seq = evt.get("seq")
    prefix = f"id: {seq}\n" if seq is not None else ""
    return f"{prefix}data: {json.dumps(evt, default=str)}\n\n"


def _sse(
    events_factory: Callable[[AsyncSession], AsyncIterator[dict]], *, session_id: UUID
) -> StreamingResponse:
    """Wrap an event generator factory in an SSE response with its own DB session.

    FastAPI tears down `Depends(get_db)` before a StreamingResponse body runs, so the
    stream owns its own session. The generation itself runs detached (see
    `chat_service._run_generation_job`): a client that disconnects can re-attach through
    the generation events endpoint, and nothing is cancelled by the disconnect alone.
    """

    async def _gen() -> AsyncIterator[str]:
        # Comment frame: flushes proxies and lets clients detect the stream opened.
        yield ": connected\n\n"
        stream_db = AsyncSessionLocal()
        try:
            async for evt in events_factory(stream_db):
                yield _frame(evt)
        except AppException as e:
            yield _frame(_error_event(e))
        finally:
            spawn_detached(f"close_stream_session:{session_id}", stream_db.close())

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)


def _explicit(payload: SendMessageRequest) -> set[str]:
    """Fields the client set on purpose (they override assistant defaults)."""
    return set(payload.model_fields_set)


# --------------------------------------------------------------------------- messages


@router.post(
    "",
    response_model=SendMessageResponse,
    dependencies=[Depends(rate_limit_dependency("chat"))],
)
async def send_message(
    session_id: UUID,
    payload: SendMessageRequest,
    request: Request,
    idempotency_key: IdempotencyKey = None,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Blocking turn. Retrying with the same `Idempotency-Key` returns the original result."""
    result = await chat_service.send_message(
        db,
        user_id=principal.user_id,
        session_id=session_id,
        content=payload.content,
        use_rag=payload.use_rag,
        use_tools=payload.use_tools,
        tool_names=payload.tool_names,
        model=payload.model,
        collection_ids=payload.collection_ids,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        request_id=request_id_of(request),
        scope=TurnScope.from_principal(principal),
        explicit=_explicit(payload),
        idempotency_key=idempotency_key,
        attachment_ids=payload.attachment_ids,
        voice_reply=payload.voice_reply,
        voice=payload.voice,
        audio_format=payload.audio_format,
    )
    return SendMessageResponse(
        user_message=MessageRead.model_validate(result["user_message"]),
        assistant_message=MessageRead.model_validate(result["assistant_message"]),
        tool_messages=[MessageRead.model_validate(m) for m in result["tool_messages"]],
        citations=result["citations"],
        tool_calls=result["tool_calls"],
        usage=result["usage"],
        approval=result["approval"],
        artifacts=result.get("artifacts", []),
    )


@router.post(
    "/stream",
    dependencies=[Depends(rate_limit_dependency("chat_stream"))],
)
async def stream_message(
    session_id: UUID,
    payload: SendMessageRequest,
    request: Request,
    idempotency_key: IdempotencyKey = None,
    principal: Principal = Depends(current_principal),
):
    """Server-Sent Events stream.

    Frames: `id: <seq>` + `data: <json>`. The first frame is `user_message` (carrying
    `generation_id`), the last is `assistant_message`. Dropping the connection does not
    stop the generation: resume with `GET …/generations/{generation_id}/events?after=<seq>`
    or stop it with `POST …/generations/{generation_id}/stop`.
    """
    request_id = request_id_of(request)
    scope = TurnScope.from_principal(principal)
    explicit = _explicit(payload)

    def _events(stream_db: AsyncSession) -> AsyncIterator[dict]:
        return chat_service.stream_message(
            stream_db,
            user_id=principal.user_id,
            session_id=session_id,
            content=payload.content,
            use_rag=payload.use_rag,
            use_tools=payload.use_tools,
            tool_names=payload.tool_names,
            model=payload.model,
            collection_ids=payload.collection_ids,
            temperature=payload.temperature,
            max_tokens=payload.max_tokens,
            request_id=request_id,
            scope=scope,
            explicit=explicit,
            idempotency_key=idempotency_key,
            attachment_ids=payload.attachment_ids,
            voice_reply=payload.voice_reply,
            voice=payload.voice,
            audio_format=payload.audio_format,
        )

    return _sse(_events, session_id=session_id)


@router.post(
    "/background",
    response_model=BackgroundSendResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit_dependency("chat_stream"))],
)
async def send_message_background(
    session_id: UUID,
    payload: SendMessageRequest,
    request: Request,
    idempotency_key: IdempotencyKey = None,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Start a turn and return immediately; attach with the generation events endpoint.

    The reply is produced even if no client is connected (mobile apps going to
    background, flaky networks).
    """
    handle = await chat_service.start_turn(
        db,
        scope=TurnScope.from_principal(principal),
        session_id=session_id,
        content=payload.content,
        use_rag=payload.use_rag,
        use_tools=payload.use_tools,
        tool_names=payload.tool_names,
        model=payload.model,
        collection_ids=payload.collection_ids,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        explicit=_explicit(payload),
        request_id=request_id_of(request),
        idempotency_key=idempotency_key,
        attachment_ids=payload.attachment_ids,
        voice_reply=payload.voice_reply,
        voice=payload.voice,
        audio_format=payload.audio_format,
        notify=True,
    )
    if handle.user_message is None:
        # Replayed idempotent request whose user message has since been deleted.
        raise NotFoundError("Original message not found")
    return BackgroundSendResponse(
        generation_id=handle.generation_id, user_message=MessageRead.model_validate(handle.user_message)
    )


@router.get("", response_model=HistoryResponse)
async def list_history(
    session_id: UUID,
    cursor: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """The active branch (oldest first). Branched messages carry `siblings {index, count}`."""
    items, next_cursor = await chat_service.list_history(
        db, user_id=principal.user_id, session_id=session_id, cursor=cursor, limit=limit
    )
    return HistoryResponse(items=[MessageRead.model_validate(m) for m in items], next_cursor=next_cursor)


@router.post(
    "/regenerate",
    response_model=RegenerateResponse,
    dependencies=[Depends(rate_limit_dependency("chat"))],
)
async def regenerate(
    session_id: UUID,
    payload: RegenerateRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Blocking regeneration of the last reply; the previous reply is kept as a sibling."""
    out = await chat_service.regenerate_last(
        db,
        user_id=principal.user_id,
        session_id=session_id,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        request_id=request_id_of(request),
        scope=TurnScope.from_principal(principal),
    )
    return RegenerateResponse(
        assistant_message=MessageRead.model_validate(out["assistant_message"]),
        tool_messages=[MessageRead.model_validate(m) for m in out["tool_messages"]],
        citations=out["citations"],
        tool_calls=out["tool_calls"],
        usage=out["usage"],
    )


@router.post(
    "/regenerate/stream",
    dependencies=[Depends(rate_limit_dependency("chat_stream"))],
)
async def regenerate_stream(
    session_id: UUID,
    payload: RegenerateRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
):
    """Streaming regeneration (same frames as `/stream`)."""
    request_id = request_id_of(request)
    scope = TurnScope.from_principal(principal)

    def _events(stream_db: AsyncSession) -> AsyncIterator[dict]:
        return chat_service.regenerate_stream(
            stream_db,
            scope=scope,
            session_id=session_id,
            temperature=payload.temperature,
            max_tokens=payload.max_tokens,
            request_id=request_id,
        )

    return _sse(_events, session_id=session_id)


@router.post(
    "/{message_id}/edit",
    dependencies=[Depends(rate_limit_dependency("chat_stream"))],
)
async def edit_message(
    session_id: UUID,
    message_id: UUID,
    payload: SendMessageRequest,
    request: Request,
    idempotency_key: IdempotencyKey = None,
    principal: Principal = Depends(current_principal),
):
    """Edit a user message: the edit becomes a sibling branch and is answered (SSE)."""
    request_id = request_id_of(request)
    scope = TurnScope.from_principal(principal)
    explicit = _explicit(payload)

    def _events(stream_db: AsyncSession) -> AsyncIterator[dict]:
        return chat_service.edit_message(
            stream_db,
            scope=scope,
            session_id=session_id,
            message_id=message_id,
            content=payload.content,
            use_rag=payload.use_rag,
            use_tools=payload.use_tools,
            tool_names=payload.tool_names,
            model=payload.model,
            collection_ids=payload.collection_ids,
            temperature=payload.temperature,
            max_tokens=payload.max_tokens,
            explicit=explicit,
            request_id=request_id,
            idempotency_key=idempotency_key,
            attachment_ids=payload.attachment_ids,
            voice_reply=payload.voice_reply,
            voice=payload.voice,
            audio_format=payload.audio_format,
        )

    return _sse(_events, session_id=session_id)


@router.get("/{message_id}/siblings", response_model=SiblingsResponse)
async def message_siblings(
    session_id: UUID,
    message_id: UUID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """All alternatives of a message (edits of a user message, regenerations of a reply)."""
    items, active_index = await chat_service.siblings(
        db, user_id=principal.user_id, session_id=session_id, message_id=message_id
    )
    return SiblingsResponse(items=[MessageRead.model_validate(m) for m in items], active_index=active_index)


@router.post("/{message_id}/feedback", response_model=FeedbackRead)
async def set_feedback(
    session_id: UUID,
    message_id: UUID,
    payload: FeedbackRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Thumbs up / down (upsert) on an assistant message."""
    row = await feedback_service.set_feedback(
        db,
        user_id=principal.user_id,
        org_id=principal.org_id,
        session_id=session_id,
        message_id=message_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    await db.commit()
    return FeedbackRead(
        message_id=row.message_id,
        session_id=row.session_id,
        user_id=row.user_id,
        rating=row.rating,
        comment=row.comment,
        created_at=row.created_at,
    )


@router.delete("/{message_id}/feedback", response_model=OkResponse)
async def delete_feedback(
    session_id: UUID,
    message_id: UUID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    removed = await feedback_service.delete_feedback(
        db, user_id=principal.user_id, session_id=session_id, message_id=message_id
    )
    await db.commit()
    return OkResponse(message="Feedback removed" if removed else "No feedback to remove")


# --------------------------------------------------------------------------- branches & generations


@generations_router.post("/branch", response_model=HistoryResponse)
async def switch_branch(
    session_id: UUID,
    payload: BranchRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Make the branch containing `leaf_message_id` active and return it."""
    await chat_service.switch_branch(
        db, user_id=principal.user_id, session_id=session_id, leaf_message_id=payload.leaf_message_id
    )
    await db.commit()
    items, next_cursor = await chat_service.list_history(
        db, user_id=principal.user_id, session_id=session_id, cursor=None, limit=200
    )
    return HistoryResponse(items=[MessageRead.model_validate(m) for m in items], next_cursor=next_cursor)


@generations_router.get("/generations/active", response_model=GenerationActiveResponse | None)
async def active_generation(
    session_id: UUID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """The in-flight generation for this session (re-attach when a chat screen opens), or null."""
    await get_session(db, user_id=principal.user_id, session_id=session_id)
    info = await chat_service.active_generation(session_id)
    return GenerationActiveResponse(**info) if info else None


@generations_router.get("/generations/{generation_id}/events")
async def generation_events(
    session_id: UUID,
    generation_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Replay a generation's frames after `after` (or `Last-Event-ID`) and follow it live.

    Returns 404 once the log has expired (`STREAM_EVENTS_TTL_SECONDS`).
    """
    await get_session(db, user_id=principal.user_id, session_id=session_id)
    info = await chat_service.generation_info(session_id, generation_id)
    if info.get("user_id") != str(principal.user_id):
        raise NotFoundError("Generation not found or expired")
    if last_event_id and last_event_id.strip().isdigit():
        after = max(after, int(last_event_id.strip()))

    def _events(_db: AsyncSession) -> AsyncIterator[dict]:
        return chat_service.follow_generation(generation_id, after=after)

    return _sse(_events, session_id=session_id)


@generations_router.post("/generations/{generation_id}/stop", response_model=OkResponse)
async def stop_generation(
    session_id: UUID,
    generation_id: str,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Stop an in-flight generation; the partial reply is persisted (`interrupted: true`)."""
    await get_session(db, user_id=principal.user_id, session_id=session_id)
    info = await chat_service.generation_info(session_id, generation_id)
    if info.get("status") != "running":
        return OkResponse(message="Generation already finished")
    await chat_service.request_stop(generation_id)
    return OkResponse(message="Stop requested")


# --------------------------------------------------------------------------- approvals


@approvals_router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    session_id: UUID,
    status: str | None = Query(default=None, pattern="^(pending|approved|denied|expired)$"),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    rows = await chat_service.list_approvals(
        db, user_id=principal.user_id, session_id=session_id, status=status
    )
    return ApprovalListResponse(items=[ApprovalRead(**chat_service.approval_read(r)) for r in rows])


@approvals_router.post(
    "/{approval_id}",
    dependencies=[Depends(rate_limit_dependency("chat_stream"))],
)
async def decide_approval(
    session_id: UUID,
    approval_id: UUID,
    payload: ApprovalDecisionRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Approve or deny pending tool calls; streams the rest of the turn as SSE."""
    request_id = request_id_of(request)
    scope = TurnScope.from_principal(principal)
    # Validate up front so 404/409 are real HTTP responses, not frames in a 200 stream.
    await chat_service.get_pending_approval(
        db, user_id=principal.user_id, session_id=session_id, approval_id=approval_id
    )

    def _events(stream_db: AsyncSession) -> AsyncIterator[dict]:
        return chat_service.resolve_approval(
            stream_db,
            user_id=principal.user_id,
            session_id=session_id,
            approval_id=approval_id,
            decision=payload.decision,
            tool_call_ids=payload.tool_call_ids,
            request_id=request_id,
            scope=scope,
        )

    return _sse(_events, session_id=session_id)


# --------------------------------------------------------------------------- search


@search_router.post("", response_model=list[SearchResult])
async def search_messages(
    payload: SearchRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Full-text search over the caller's messages in the active organization."""
    return await chat_service.search(
        db, user_id=principal.user_id, query=payload.query, limit=payload.limit, org_id=principal.org_id
    )
