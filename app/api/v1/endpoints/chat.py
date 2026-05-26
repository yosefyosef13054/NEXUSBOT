from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import current_user
from app.core.rate_limit import rate_limit_dependency
from app.db.session import get_db
from app.models.user import User
from app.schemas.chat import (
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


@router.post(
    "",
    response_model=SendMessageResponse,
    dependencies=[Depends(rate_limit_dependency("chat"))],
)
async def send_message(
    session_id: UUID,
    payload: SendMessageRequest,
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
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
    )
    return SendMessageResponse(
        user_message=MessageRead.model_validate(result["user_message"]),
        assistant_message=MessageRead.model_validate(result["assistant_message"]),
        citations=result["citations"],
        tool_calls=result["tool_calls"],
        usage=result["usage"],
    )


@router.post(
    "/stream",
    dependencies=[Depends(rate_limit_dependency("chat_stream"))],
)
async def stream_message(
    session_id: UUID,
    payload: SendMessageRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Server-Sent Events stream.

    Frame format: `data: <json>\\n\\n`. Final frame's `type=="assistant_message"`.
    """
    async def _gen():
        async for evt in chat_service.stream_message(
            db,
            user_id=user.id,
            session_id=session_id,
            content=payload.content,
            use_rag=payload.use_rag,
            use_tools=payload.use_tools,
            tool_names=payload.tool_names,
            temperature=payload.temperature,
            max_tokens=payload.max_tokens,
        ):
            yield f"data: {json.dumps(evt, default=str)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
    return HistoryResponse(
        items=[MessageRead.model_validate(m) for m in items], next_cursor=next_cursor
    )


@router.post("/regenerate", response_model=RegenerateResponse)
async def regenerate(
    session_id: UUID,
    payload: RegenerateRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    out = await chat_service.regenerate_last(
        db,
        user_id=user.id,
        session_id=session_id,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
    )
    return RegenerateResponse(
        assistant_message=MessageRead.model_validate(out["assistant_message"]),
        citations=out["citations"],
        tool_calls=out["tool_calls"],
    )


search_router = APIRouter(prefix="/search", tags=["chat"])


@search_router.post("", response_model=list[SearchResult])
async def search_messages(
    payload: SearchRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return await chat_service.search(db, user_id=user.id, query=payload.query, limit=payload.limit)
