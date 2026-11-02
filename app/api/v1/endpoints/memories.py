from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import Principal, current_principal
from app.core.exceptions import ConflictError, ValidationError
from app.db.session import get_db
from app.models.memory import Memory
from app.schemas.common import OkResponse, Page
from app.schemas.memory import MemoryCreateRequest, MemoryRead, MemoryUpdateRequest
from app.services import memory_service

router = APIRouter(prefix="/memories", tags=["memory"])


def _read(m: Memory) -> MemoryRead:
    return MemoryRead(
        id=m.id,
        content=m.content,
        kind=m.kind.value,  # type: ignore[arg-type]
        importance=m.importance,
        source_message_id=m.source_message_id,
        created_at=m.created_at,
        last_used_at=m.last_used_at,
    )


@router.get("", response_model=Page[MemoryRead])
async def list_memories(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    items, total = await memory_service.list_memories(
        db, user_id=principal.user_id, org_id=principal.org_id, page=page, page_size=page_size
    )
    return Page(items=[_read(m) for m in items], total=total, page=page, page_size=page_size)


@router.post("", response_model=MemoryRead, status_code=status.HTTP_201_CREATED)
async def create_memory(
    payload: MemoryCreateRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    obj = await memory_service.create_memory(
        db,
        user_id=principal.user_id,
        org_id=principal.org_id,
        content=payload.content,
        kind=payload.kind,
        importance=payload.importance,
        skip_duplicates=True,
    )
    if obj is None:
        if await memory_service.has_similar(db, user_id=principal.user_id, content=payload.content):
            raise ConflictError("A memory with this content already exists")
        raise ValidationError("Memory could not be stored (empty content or memory limit reached)")
    await db.commit()
    await db.refresh(obj)
    return _read(obj)


@router.patch("/{memory_id}", response_model=MemoryRead)
async def update_memory(
    memory_id: UUID,
    payload: MemoryUpdateRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    obj = await memory_service.update_memory(
        db,
        user_id=principal.user_id,
        memory_id=memory_id,
        content=payload.content,
        kind=payload.kind,
        importance=payload.importance,
    )
    await db.commit()
    return _read(obj)


@router.delete("/{memory_id}", response_model=OkResponse)
async def delete_memory(
    memory_id: UUID, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
):
    await memory_service.delete_memory(db, user_id=principal.user_id, memory_id=memory_id)
    await db.commit()
    return OkResponse(message="Memory deleted")


@router.delete("", response_model=OkResponse)
async def clear_memories(
    principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
):
    count = await memory_service.clear_memories(db, user_id=principal.user_id)
    await db.commit()
    return OkResponse(message=f"Cleared {count} memories")
