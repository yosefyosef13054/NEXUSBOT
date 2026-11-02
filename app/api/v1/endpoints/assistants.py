from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import Principal, current_principal
from app.db.session import get_db
from app.models.assistant import Assistant
from app.schemas.assistant import AssistantCreateRequest, AssistantRead, AssistantUpdateRequest
from app.schemas.common import OkResponse, Page
from app.services import assistant_service

router = APIRouter(prefix="/assistants", tags=["assistants"])


def _read(a: Assistant) -> AssistantRead:
    return AssistantRead(
        id=a.id,
        org_id=a.org_id,
        name=a.name,
        description=a.description,
        avatar=a.avatar,
        system_prompt=a.system_prompt,
        model=a.model,
        tool_names=list(a.tool_names or []),
        collection_ids=[UUID(str(c)) for c in (a.collection_ids or [])],
        use_rag=a.use_rag,
        use_tools=a.use_tools,
        temperature=a.temperature,
        memory_enabled=a.memory_enabled,
        is_default=a.is_default,
        created_by=a.created_by,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


@router.get("", response_model=Page[AssistantRead])
async def list_assistants(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    items, total = await assistant_service.list_assistants(
        db, org_id=principal.org_id, page=page, page_size=page_size
    )
    return Page(items=[_read(a) for a in items], total=total, page=page, page_size=page_size)


@router.post("", response_model=AssistantRead, status_code=status.HTTP_201_CREATED)
async def create_assistant(
    payload: AssistantCreateRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    obj = await assistant_service.create_assistant(
        db, org_id=principal.org_id, user_id=principal.user_id, payload=payload
    )
    await db.commit()
    await db.refresh(obj)
    return _read(obj)


@router.get("/{assistant_id}", response_model=AssistantRead)
async def get_assistant(
    assistant_id: UUID, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
):
    return _read(
        await assistant_service.get_assistant(db, org_id=principal.org_id, assistant_id=assistant_id)
    )


@router.patch("/{assistant_id}", response_model=AssistantRead)
async def update_assistant(
    assistant_id: UUID,
    payload: AssistantUpdateRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    obj = await assistant_service.update_assistant(
        db, membership=principal.membership, assistant_id=assistant_id, payload=payload
    )
    await db.commit()
    return _read(obj)


@router.delete("/{assistant_id}", response_model=OkResponse)
async def delete_assistant(
    assistant_id: UUID, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
):
    await assistant_service.delete_assistant(db, membership=principal.membership, assistant_id=assistant_id)
    await db.commit()
    return OkResponse(message="Assistant deleted")
