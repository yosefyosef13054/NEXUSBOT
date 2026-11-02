from __future__ import annotations

import json
from datetime import datetime
from typing import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background import spawn_detached
from app.core.dependencies import current_user
from app.core.exceptions import AppException, request_id_of
from app.core.rate_limit import rate_limit_dependency
from app.db.session import AsyncSessionLocal, get_db
from app.models.user import User
from app.schemas.chat import (
    ApprovalDecisionRequest,
    ApprovalListResponse,
    ApprovalRead,
    HistoryResponse,
    MessageRead,
    RegenerateRequest,
    RegenerateResponse,
    SearchRequest,
    SearchResult,
    SendMessageRequest,
    SendMessageResponse,
)
from app.services import chat_service

router = APIRouter(prefix="/sessions/{session_id}/messages", tags=["chat"])
approvals_router = APIRouter(prefix="/sessions/{session_id}/approvals", tags=["chat"])
search_router = APIRouter(prefix="/search", tags=["chat"])


def _error_event(e: AppException) -> dict:
    return {"type": "error", "data": {"code": e.code, "message": e.message, "details": e.details}}


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse(events_factory, *, session_id: UUID) -> StreamingResponse:
    """Wrap an event generator factory in an SSE response with its own DB session.

    FastAPI tears down `Depends(get_db)` before a StreamingResponse body runs, so the
    stream owns its own session. On client disconnect this generator is cancelled; the
    session is then closed by a detached task (awaiting inside a cancelled scope is
    impossible) so no connection is left "idle in transaction".
    """

    async def _gen() -> AsyncIterator[str]:
        # Comment frame: flushes proxies and lets clients detect the stream opened.
        yield ": connected\n\n"
        stream_db = AsyncSessionLocal()
        try:
            async for evt in events_factory(stream_db):
                yield f"data: {json.dumps(evt, default=str)}\n\n"
        except AppException as e:
            yield f"data: {json.dumps(_error_event(e), default=str)}\n\n"
        finally:
            spawn_detached(f"close_stream_session:{session_id}", stream_db.close())

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post(
    "",
    response_model=SendMessageResponse,
    dependencies=[Depends(rate_limit_dependency("chat"))],
)
async def send_message(
    session_id: UUID,
    payload: SendMessageRequest,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await chat_service.send_message(
        db,
        user_id=user.id,
        session_id=session_id,
        content=payload.content,
        use_rag=payload.use_rag,
        use_tools=payload.use_tools,
        tool_names=payload.tool_names,
        model=payload.model,
        collection_ids=payload.collection_ids,
        user_default_model=user.default_model,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        request_id=request_id_of(request),
    )
    return SendMessageResponse(
        user_message=MessageRead.model_validate(result["user_message"]),
        assistant_message=MessageRead.model_validate(result["assistant_message"]),
        tool_messages=[MessageRead.model_validate(m) for m in result["tool_messages"]],
        citations=result["citations"],
        tool_calls=result["tool_calls"],
        usage=result["usage"],
        approval=result["approval"],
    )


@router.post(
    "/stream",
    dependencies=[Depends(rate_limit_dependency("chat_stream"))],
)
async def stream_message(
    session_id: UUID,
    payload: SendMessageRequest,
    request: Request,
    user: User = Depends(current_user),
):
    """Server-Sent Events stream.

    Frame format: `data: <json>\\n\\n`. Final frame's `type=="assistant_message"`.
    Closing the connection cancels the generation; the partial reply is persisted.
    """
    request_id = request_id_of(request)

    def _events(stream_db: AsyncSession):
        return chat_service.stream_message(
            stream_db,
            user_id=user.id,
            session_id=session_id,
            content=payload.content,
            use_rag=payload.use_rag,
            use_tools=payload.use_tools,
            tool_names=payload.tool_names,
            model=payload.model,
            collection_ids=payload.collection_ids,
            user_default_model=user.default_model,
            temperature=payload.temperature,
            max_tokens=payload.max_tokens,
            request_id=request_id,
        )

    return _sse(_events, session_id=session_id)


@router.get("", response_model=HistoryResponse)
async def list_history(
    session_id: UUID,
    cursor: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    items, next_cursor = await chat_service.list_history(
        db, user_id=user.id, session_id=session_id, cursor=cursor, limit=limit
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
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    out = await chat_service.regenerate_last(
        db,
        user_id=user.id,
        session_id=session_id,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        user_default_model=user.default_model,
        request_id=request_id_of(request),
    )
    return RegenerateResponse(
        assistant_message=MessageRead.model_validate(out["assistant_message"]),
        tool_messages=[MessageRead.model_validate(m) for m in out["tool_messages"]],
        citations=out["citations"],
        tool_calls=out["tool_calls"],
        usage=out["usage"],
    )


@approvals_router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    session_id: UUID,
    status: str | None = Query(default=None, pattern="^(pending|approved|denied|expired)$"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await chat_service.list_approvals(db, user_id=user.id, session_id=session_id, status=status)
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
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve or deny pending tool calls; streams the rest of the turn as SSE."""
    request_id = request_id_of(request)
    # Validate up front so 404/409 are real HTTP responses, not frames in a 200 stream.
    await chat_service.get_pending_approval(
        db, user_id=user.id, session_id=session_id, approval_id=approval_id
    )

    def _events(stream_db: AsyncSession):
        return chat_service.resolve_approval(
            stream_db,
            user_id=user.id,
            session_id=session_id,
            approval_id=approval_id,
            decision=payload.decision,
            tool_call_ids=payload.tool_call_ids,
            request_id=request_id,
        )

    return _sse(_events, session_id=session_id)


@search_router.post("", response_model=list[SearchResult])
async def search_messages(
    payload: SearchRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return await chat_service.search(db, user_id=user.id, query=payload.query, limit=payload.limit)
