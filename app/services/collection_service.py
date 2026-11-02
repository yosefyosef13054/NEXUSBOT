from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.collection import Collection
from app.models.document import Document


async def create_collection(
    db: AsyncSession, *, user_id: UUID, name: str, description: str | None
) -> Collection:
    obj = Collection(user_id=user_id, name=name.strip(), description=(description or "").strip() or None)
    db.add(obj)
    await db.flush()
    return obj


async def get_collection(db: AsyncSession, *, user_id: UUID, collection_id: UUID) -> Collection:
    obj = await db.get(Collection, collection_id)
    if obj is None or obj.user_id != user_id or obj.deleted_at is not None:
        raise NotFoundError("Collection not found")
    return obj


async def ensure_collections(db: AsyncSession, *, user_id: UUID, ids: list[UUID] | None) -> list[UUID]:
    """Validate that every id belongs to the user; raises 422 listing the unknown ones."""
    if not ids:
        return []
    rows = (
        (
            await db.execute(
                select(Collection.id).where(
                    Collection.user_id == user_id, Collection.deleted_at.is_(None), Collection.id.in_(ids)
                )
            )
        )
        .scalars()
        .all()
    )
    found = set(rows)
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
    db: AsyncSession, *, user_id: UUID, page: int, page_size: int
) -> tuple[list[Collection], int]:
    base = select(Collection).where(Collection.user_id == user_id, Collection.deleted_at.is_(None))
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


async def update_collection(
    db: AsyncSession, *, user_id: UUID, collection_id: UUID, name: str | None, description: str | None
) -> Collection:
    obj = await get_collection(db, user_id=user_id, collection_id=collection_id)
    if name is not None:
        obj.name = name.strip()
    if description is not None:
        obj.description = description.strip() or None
    await db.flush()
    await db.refresh(obj)
    return obj


async def delete_collection(
    db: AsyncSession, *, user_id: UUID, collection_id: UUID, delete_documents: bool
) -> int:
    """Soft-delete the collection. Documents are unassigned, or soft-deleted when asked."""
    obj = await get_collection(db, user_id=user_id, collection_id=collection_id)
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
