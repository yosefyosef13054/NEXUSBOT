from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import aiofiles
from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logger import get_logger
from app.models.document import Document, DocumentStatus
from app.utils.files import is_valid_docx, sha256_file_async, sniff_mime, storage_path_for

logger = get_logger(__name__)

_SNIFF_BYTES = 8192


def _extension_of(filename: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    allowed = [e.lower() for e in settings.ALLOWED_UPLOAD_EXTENSIONS]
    if not ext or ext not in allowed:
        raise ValidationError(
            f"Unsupported file type '{ext or '(none)'}'",
            details={"allowed_extensions": allowed},
        )
    return ext


async def _save_upload_to_disk(file: UploadFile, dest: Path) -> tuple[int, bytes]:
    """Stream the upload to disk enforcing the size cap; returns (size, first bytes)."""
    size = 0
    head = b""
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            if len(head) < _SNIFF_BYTES:
                head += chunk[: _SNIFF_BYTES - len(head)]
            size += len(chunk)
            if size > max_bytes:
                raise ValidationError(f"File exceeds {settings.MAX_UPLOAD_MB} MB limit")
            await out.write(chunk)
    return size, head


async def _discard(path: Path) -> None:
    await asyncio.to_thread(lambda: path.unlink(missing_ok=True))


async def upload_document(
    db: AsyncSession,
    *,
    user_id: UUID,
    file: UploadFile,
    session_id: UUID | None,
) -> tuple[Document, bool]:
    """Store an upload and create its Document row.

    Returns `(document, created)`. `created=False` means an identical file (same SHA-256)
    already exists for this user and scope; the caller should not enqueue ingestion.
    The client-supplied content type is ignored — the bytes are sniffed instead.
    """
    ext = _extension_of(file.filename)
    dest = storage_path_for(str(user_id), file.filename or f"upload{ext}")
    try:
        size, head = await _save_upload_to_disk(file, dest)
    except Exception:
        await _discard(dest)
        raise

    if size == 0:
        await _discard(dest)
        raise ValidationError("Empty file")

    mime = sniff_mime(head, ext)
    if mime is None or (ext == ".docx" and not await asyncio.to_thread(is_valid_docx, dest)):
        await _discard(dest)
        raise ValidationError("File content does not match its extension", details={"extension": ext})

    digest = await sha256_file_async(dest)
    duplicate_stmt = select(Document).where(
        Document.user_id == user_id,
        Document.content_hash == digest,
        Document.deleted_at.is_(None),
        Document.status != DocumentStatus.FAILED,
    )
    duplicate_stmt = (
        duplicate_stmt.where(Document.session_id == session_id)
        if session_id is not None
        else duplicate_stmt.where(Document.session_id.is_(None))
    )
    existing = (await db.execute(duplicate_stmt.limit(1))).scalar_one_or_none()
    if existing is not None:
        await _discard(dest)
        logger.info("upload_deduplicated", document_id=str(existing.id), user_id=str(user_id))
        return existing, False

    doc = Document(
        user_id=user_id,
        session_id=session_id,
        name=file.filename or dest.name,
        mime_type=mime,
        size_bytes=size,
        storage_path=str(dest),
        status=DocumentStatus.PENDING,
        content_hash=digest,
        meta={"sha256": digest, "progress": 0},
    )
    db.add(doc)
    await db.flush()
    await db.commit()
    return doc, True


async def list_documents(
    db: AsyncSession, *, user_id: UUID, page: int, page_size: int
) -> tuple[list[Document], int]:
    base = select(Document).where(Document.user_id == user_id, Document.deleted_at.is_(None))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                base.order_by(Document.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def get_document(db: AsyncSession, *, user_id: UUID, document_id: UUID) -> Document:
    obj = await db.get(Document, document_id)
    if obj is None or obj.user_id != user_id or obj.deleted_at is not None:
        raise NotFoundError("Document not found")
    return obj


async def soft_delete_document(db: AsyncSession, *, user_id: UUID, document_id: UUID) -> None:
    obj = await get_document(db, user_id=user_id, document_id=document_id)
    obj.deleted_at = datetime.now(UTC)
    await db.flush()
    await db.commit()
    # Best effort: the row is the source of truth, the bytes are reclaimable.
    try:
        await _discard(Path(obj.storage_path))
    except Exception:  # noqa: BLE001
        logger.warning("document_file_cleanup_failed", document_id=str(document_id))
