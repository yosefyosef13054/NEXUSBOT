from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.collection import Collection
from app.models.document import Document
from app.models.organization import Membership, OrgRole
from app.services.org_service import has_role


async def create_collection(
    db: AsyncSession, *, user_id: UUID, org_id: UUID | None, name: str, description: str | None
) -> Collection:
    obj = Collection(
        user_id=user_id, org_id=org_id, name=name.strip(), description=(description or "").strip() or None
    )
    db.add(obj)
    await db.flush()
    return obj


def _visible(obj: Collection | None, *, org_id: UUID | None, user_id: UUID | None) -> bool:
    if obj is None or obj.deleted_at is not None:
        return False
    if org_id is not None and obj.org_id == org_id:
        return True
    return user_id is not None and obj.user_id == user_id and obj.org_id is None


async def get_collection(
    db: AsyncSession, *, collection_id: UUID, org_id: UUID | None = None, user_id: UUID | None = None
) -> Collection:
    """Collections are shared with every member of their organization."""
    obj = await db.get(Collection, collection_id)
    if not _visible(obj, org_id=org_id, user_id=user_id):
        raise NotFoundError("Collection not found")
    assert obj is not None
    return obj


async def ensure_collections(
    db: AsyncSession, *, ids: list[UUID] | None, org_id: UUID | None = None, user_id: UUID | None = None
) -> list[UUID]:
    """Validate that every id is visible in the scope; raises 422 listing the unknown ones."""
    if not ids:
        return []
    stmt = select(Collection.id).where(Collection.deleted_at.is_(None), Collection.id.in_(ids))
    if org_id is not None:
        stmt = stmt.where(Collection.org_id == org_id)
    elif user_id is not None:
        stmt = stmt.where(Collection.user_id == user_id)
    found = set((await db.execute(stmt)).scalars().all())
    unknown = [str(i) for i in ids if i not in found]
    if unknown:
        raise ValidationError("Unknown collections", details={"unknown": unknown})
    return list(ids)


async def document_counts(db: AsyncSession, *, collection_ids: list[UUID]) -> dict[UUID, int]:
    if not collection_ids:
        return {}
    rows = (
        await db.execute(
            select(Document.collection_id, func.count())
            .where(Document.collection_id.in_(collection_ids), Document.deleted_at.is_(None))
            .group_by(Document.collection_id)
        )
    ).all()
    return {cid: int(n) for cid, n in rows}


async def list_collections(
    db: AsyncSession, *, org_id: UUID | None, user_id: UUID, page: int, page_size: int
) -> tuple[list[Collection], int]:
    base = select(Collection).where(Collection.deleted_at.is_(None))
    base = (
        base.where(Collection.org_id == org_id)
        if org_id is not None
        else base.where(Collection.user_id == user_id)
    )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                base.order_by(Collection.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


def can_manage(membership: Membership | None, collection: Collection) -> bool:
    if membership is None:
        return False
    return collection.user_id == membership.user_id or has_role(membership, OrgRole.ADMIN)


async def update_collection(
    db: AsyncSession,
    *,
    membership: Membership,
    collection_id: UUID,
    name: str | None,
    description: str | None,
) -> Collection:
    obj = await get_collection(
        db, collection_id=collection_id, org_id=membership.org_id, user_id=membership.user_id
    )
    if not can_manage(membership, obj):
        raise ForbiddenError("Only the creator or an organization admin can edit this collection")
    if name is not None:
        obj.name = name.strip()
    if description is not None:
        obj.description = description.strip() or None
    await db.flush()
    await db.refresh(obj)
    return obj


async def delete_collection(
    db: AsyncSession, *, membership: Membership, collection_id: UUID, delete_documents: bool
) -> int:
    """Soft-delete the collection. Documents are unassigned, or soft-deleted when asked."""
    obj = await get_collection(
        db, collection_id=collection_id, org_id=membership.org_id, user_id=membership.user_id
    )
    if not can_manage(membership, obj):
        raise ForbiddenError("Only the creator or an organization admin can delete this collection")
    now = datetime.now(UTC)
    if delete_documents:
        docs = (
            (
                await db.execute(
                    select(Document).where(
                        Document.collection_id == collection_id, Document.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        for d in docs:
            d.deleted_at = now
            try:
                Path(d.storage_path).unlink(missing_ok=True)
            except OSError:
                pass
        affected = len(docs)
    else:
        result = await db.execute(
            update(Document).where(Document.collection_id == collection_id).values(collection_id=None)
        )
        affected = int(result.rowcount or 0)
    obj.deleted_at = now
    await db.flush()
    return affected
