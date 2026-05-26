from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import aiofiles
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.models.document import Document, DocumentStatus
from app.utils.files import storage_path_for


_ALLOWED_MIME_PREFIXES = (
    "text/",
    "application/pdf",
    "application/json",
    "application/vnd.openxmlformats-officedocument",
    "application/octet-stream",
)


def _validate_mime(file: UploadFile) -> None:
    if not file.content_type:
        return
    if not any(file.content_type.startswith(p) for p in _ALLOWED_MIME_PREFIXES):
        raise ValidationError(f"Unsupported mime type: {file.content_type}")


async def _save_upload_to_disk(file: UploadFile, dest: Path) -> int:
    size = 0
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValidationError(f"File exceeds {settings.MAX_UPLOAD_MB} MB limit")
            await out.write(chunk)
    return size


async def upload_document(
    db: AsyncSession,
    *,
    user_id: UUID,
    file: UploadFile,
    session_id: UUID | None,
) -> Document:
    _validate_mime(file)
    dest = storage_path_for(str(user_id), file.filename or "upload.bin")
    size = await _save_upload_to_disk(file, dest)

    doc = Document(
        user_id=user_id,
        session_id=session_id,
        name=file.filename or dest.name,
        mime_type=file.content_type or "application/octet-stream",
        size_bytes=size,
        storage_path=str(dest),
        status=DocumentStatus.PENDING,
    )
    db.add(doc)
    await db.flush()
    await db.commit()
    return doc


async def list_documents(
    db: AsyncSession, *, user_id: UUID, page: int, page_size: int
) -> tuple[list[Document], int]:
    base = select(Document).where(Document.user_id == user_id, Document.deleted_at.is_(None))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Document.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), int(total)


async def get_document(db: AsyncSession, *, user_id: UUID, document_id: UUID) -> Document:
    obj = await db.get(Document, document_id)
    if obj is None or obj.user_id != user_id or obj.deleted_at is not None:
        raise NotFoundError("Document not found")
    return obj


async def soft_delete_document(db: AsyncSession, *, user_id: UUID, document_id: UUID) -> None:
    obj = await get_document(db, user_id=user_id, document_id=document_id)
    obj.deleted_at = datetime.now(timezone.utc)
    await db.flush()
    await db.commit()
