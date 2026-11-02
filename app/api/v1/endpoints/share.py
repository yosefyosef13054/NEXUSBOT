from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import Principal, current_principal
from app.core.rate_limit import ip_rate_limit_dependency
from app.db.session import get_db
from app.schemas.common import OkResponse
from app.schemas.share import ShareCreateRequest, SharedTranscript, ShareListResponse, ShareRead
from app.services import share_service
from app.services.session_service import get_session
from app.utils.files import safe_filename

router = APIRouter(prefix="/sessions/{session_id}", tags=["sharing"])
public_router = APIRouter(prefix="/shared", tags=["sharing"])


@router.get("/export")
async def export_session(
    session_id: UUID,
    format: Literal["markdown", "json"] = Query(default="markdown"),
    include_tools: bool = Query(default=True),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Download the active branch as Markdown or JSON."""
    session_obj = await get_session(db, user_id=principal.user_id, session_id=session_id)
    messages = await share_service.transcript(db, user_id=principal.user_id, session_id=session_id)
    stem = safe_filename(session_obj.title or "chat")[:60] or "chat"
    if format == "json":
        body = share_service.to_json_text(
            share_service.to_json(session_obj, messages, include_tools=include_tools)
        )
        return Response(
            body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{stem}.json"'},
        )
    body = share_service.to_markdown(session_obj, messages, include_tools=include_tools)
    return Response(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{stem}.md"'},
    )


@router.post("/share", response_model=ShareRead, status_code=status.HTTP_201_CREATED)
async def create_share(
    session_id: UUID,
    payload: ShareCreateRequest | None = None,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Mint a read-only link to this conversation (the token is shown once)."""
    row, token = await share_service.create_share(
        db,
        user_id=principal.user_id,
        session_id=session_id,
        expires_in_hours=payload.expires_in_hours if payload else None,
    )
    await db.commit()
    return ShareRead(
        id=row.id, token=token, url=share_service.share_url(token), expires_at=row.expires_at, views=row.views
    )


@router.get("/share", response_model=ShareListResponse)
async def list_shares(
    session_id: UUID, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
):
    rows = await share_service.list_shares(db, user_id=principal.user_id, session_id=session_id)
    return ShareListResponse(
        items=[ShareRead(id=r.id, token=None, url=None, expires_at=r.expires_at, views=r.views) for r in rows]
    )


@router.delete("/share", response_model=OkResponse)
async def revoke_shares(
    session_id: UUID,
    share_id: UUID | None = Query(default=None),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    count = await share_service.revoke_shares(
        db, user_id=principal.user_id, session_id=session_id, share_id=share_id
    )
    await db.commit()
    return OkResponse(message=f"Revoked {count} link(s)")


@public_router.get(
    "/{token}",
    response_model=SharedTranscript,
    dependencies=[Depends(ip_rate_limit_dependency("shared", requests=120))],
)
async def view_shared(token: str, db: AsyncSession = Depends(get_db)):
    """Public, read-only transcript behind a share token (no authentication)."""
    row, session_obj = await share_service.resolve_share(db, token)
    messages = await share_service.transcript(db, user_id=row.user_id, session_id=session_obj.id)
    await db.commit()
    payload = share_service.to_json(session_obj, messages, include_tools=False)
    return SharedTranscript(**payload, expires_at=row.expires_at)


@public_router.get(
    "/{token}/markdown", dependencies=[Depends(ip_rate_limit_dependency("shared", requests=120))]
)
async def view_shared_markdown(token: str, db: AsyncSession = Depends(get_db)):
    row, session_obj = await share_service.resolve_share(db, token)
    messages = await share_service.transcript(db, user_id=row.user_id, session_id=session_obj.id)
    await db.commit()
    return Response(
        share_service.to_markdown(session_obj, messages, include_tools=False),
        media_type="text/markdown; charset=utf-8",
    )
