from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import Principal, current_principal
from app.db.session import get_db
from app.models.collection import Collection
from app.schemas.collection import CollectionCreateRequest, CollectionRead, CollectionUpdateRequest
from app.schemas.common import OkResponse, Page
from app.services import collection_service

router = APIRouter(prefix="/collections", tags=["collections"])


async def _read(db: AsyncSession, items: list[Collection]) -> list[CollectionRead]:
    counts = await collection_service.document_counts(db, collection_ids=[c.id for c in items])
    return [
        CollectionRead(
            id=c.id,
            org_id=c.org_id,
            name=c.name,
            description=c.description,
            document_count=counts.get(c.id, 0),
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in items
    ]


@router.post("", response_model=CollectionRead, status_code=status.HTTP_201_CREATED)
async def create(
    payload: CollectionCreateRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    obj = await collection_service.create_collection(
        db,
        user_id=principal.user_id,
        org_id=principal.org_id,
        name=payload.name,
        description=payload.description,
    )
    await db.commit()
    await db.refresh(obj)
    return (await _read(db, [obj]))[0]


@router.get("", response_model=Page[CollectionRead])
async def list_collections(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    items, total = await collection_service.list_collections(
        db, org_id=principal.org_id, user_id=principal.user_id, page=page, page_size=page_size
    )
    return Page(items=await _read(db, items), total=total, page=page, page_size=page_size)


@router.get("/{collection_id}", response_model=CollectionRead)
async def get_one(
    collection_id: UUID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    obj = await collection_service.get_collection(
        db, collection_id=collection_id, org_id=principal.org_id, user_id=principal.user_id
    )
    return (await _read(db, [obj]))[0]


@router.patch("/{collection_id}", response_model=CollectionRead)
async def patch(
    collection_id: UUID,
    payload: CollectionUpdateRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    obj = await collection_service.update_collection(
        db,
        membership=principal.membership,
        collection_id=collection_id,
        name=payload.name,
        description=payload.description,
    )
    await db.commit()
    return (await _read(db, [obj]))[0]


@router.delete("/{collection_id}", response_model=OkResponse)
async def delete(
    collection_id: UUID,
    delete_documents: bool = Query(default=False),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    affected = await collection_service.delete_collection(
        db, membership=principal.membership, collection_id=collection_id, delete_documents=delete_documents
    )
    await db.commit()
    verb = "deleted" if delete_documents else "unassigned"
    return OkResponse(message=f"Collection deleted; {affected} document(s) {verb}")
