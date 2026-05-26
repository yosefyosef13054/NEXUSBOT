from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.chat import SessionCreateRequest, SessionRead, SessionUpdateRequest
from app.schemas.common import OkResponse, Page
from app.services import session_service

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
async def create(
    payload: SessionCreateRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    obj = await session_service.create_session(
        db,
        user_id=user.id,
        title=payload.title,
        system_prompt=payload.system_prompt,
        model=payload.model,
        meta=payload.meta,
    )
    await db.commit()
    return obj


@router.get("", response_model=Page[SessionRead])
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    items, total = await session_service.list_sessions(
        db, user_id=user.id, page=page, page_size=page_size
    )
    return Page(items=[SessionRead.model_validate(i) for i in items],
                total=total, page=page, page_size=page_size)


@router.get("/{session_id}", response_model=SessionRead)
async def get_one(
    session_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return await session_service.get_session(db, user_id=user.id, session_id=session_id)


@router.patch("/{session_id}", response_model=SessionRead)
async def patch(
    session_id: UUID,
    payload: SessionUpdateRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    obj = await session_service.update_session(
        db,
        user_id=user.id,
        session_id=session_id,
        title=payload.title,
        system_prompt=payload.system_prompt,
        meta=payload.meta,
    )
    await db.commit()
    return obj


@router.delete("/{session_id}", response_model=OkResponse)
async def delete(
    session_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await session_service.soft_delete_session(db, user_id=user.id, session_id=session_id)
    await db.commit()
    return OkResponse(message="Session deleted")
