from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import aiofiles
from fastapi import UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logger import get_logger
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.models.organization import Membership, OrgRole
from app.services.collection_service import ensure_collections
from app.services.org_service import has_role
from app.storage import get_storage, storage_key
from app.utils.files import is_valid_docx, safe_filename, sha256_file_async, sniff_mime, storage_path_for
from app.utils.web import UnsafeUrlError, fetch_url_safely

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


async def _validate_collection(
    db: AsyncSession, *, user_id: UUID, collection_id: UUID | None, org_id: UUID | None = None
) -> None:
    if collection_id is not None:
        await ensure_collections(db, ids=[collection_id], org_id=org_id, user_id=user_id)


async def _register(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID | None,
    collection_id: UUID | None,
    dest: Path,
    size: int,
    head: bytes,
    ext: str,
    name: str,
    source_url: str | None = None,
    org_id: UUID | None = None,
) -> tuple[Document, bool]:
    """Validate stored bytes, dedupe by hash and create the Document row."""
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

    # The key keeps the scratch file's extension: loaders pick a parser by suffix.
    key = storage_key("documents", str(user_id), dest.name.split("_", 1)[-1] or dest.name)
    await get_storage().put_file(key, dest, content_type=mime)
    doc = Document(
        user_id=user_id,
        org_id=org_id,
        session_id=session_id,
        collection_id=collection_id,
        name=name[:512],
        mime_type=mime,
        size_bytes=size,
        storage_path=key,
        source_url=source_url,
        status=DocumentStatus.PENDING,
        content_hash=digest,
        meta={"sha256": digest, "progress": 0},
    )
    db.add(doc)
    await db.flush()
    await db.commit()
    return doc, True


async def upload_document(
    db: AsyncSession,
    *,
    user_id: UUID,
    file: UploadFile,
    session_id: UUID | None,
    collection_id: UUID | None = None,
    org_id: UUID | None = None,
) -> tuple[Document, bool]:
    """Store an upload and create its Document row.

    Returns `(document, created)`. `created=False` means an identical file (same SHA-256)
    already exists for this user and scope; the caller should not enqueue ingestion.
    The client-supplied content type is ignored — the bytes are sniffed instead.
    """
    await _validate_collection(db, user_id=user_id, collection_id=collection_id, org_id=org_id)
    ext = _extension_of(file.filename)
    dest = storage_path_for(str(user_id), file.filename or f"upload{ext}")
    try:
        size, head = await _save_upload_to_disk(file, dest)
    except Exception:
        await _discard(dest)
        raise
    return await _register(
        db,
        user_id=user_id,
        session_id=session_id,
        collection_id=collection_id,
        dest=dest,
        size=size,
        head=head,
        ext=ext,
        name=file.filename or dest.name,
        org_id=org_id,
    )


async def ingest_from_url(
    db: AsyncSession,
    *,
    user_id: UUID,
    url: str,
    session_id: UUID | None,
    collection_id: UUID | None = None,
    org_id: UUID | None = None,
) -> tuple[Document, bool]:
    """Download a public web page / PDF / text resource and register it like an upload."""
    await _validate_collection(db, user_id=user_id, collection_id=collection_id, org_id=org_id)
    try:
        resource = await fetch_url_safely(url)
    except UnsafeUrlError as e:
        raise ValidationError(f"Cannot fetch URL: {e}", details={"url": url})
    except Exception as e:  # noqa: BLE001 — network errors
        raise ValidationError(f"Cannot fetch URL: {str(e)[:200]}", details={"url": url})

    if resource.is_pdf:
        ext = ".pdf"
    elif resource.is_html:
        ext = ".html"
    elif resource.content_type == "application/json":
        ext = ".json"
    elif resource.content_type == "text/markdown":
        ext = ".md"
    else:
        ext = ".txt"

    parts = urlsplit(resource.final_url)
    base = resource.title or Path(parts.path).name or parts.netloc or "page"
    base = safe_filename(base.replace(" ", "_"))[:120]
    if not base.lower().endswith(ext):
        base = f"{base}{ext}"
    dest = storage_path_for(str(user_id), base)
    async with aiofiles.open(dest, "wb") as out:
        await out.write(resource.body)
    display_name = (resource.title or resource.final_url)[:512]
    if not display_name.lower().endswith(ext) and not resource.title:
        display_name = resource.final_url[:512]
    return await _register(
        db,
        user_id=user_id,
        session_id=session_id,
        collection_id=collection_id,
        dest=dest,
        size=len(resource.body),
        head=resource.body[:_SNIFF_BYTES],
        ext=ext,
        name=display_name,
        source_url=resource.final_url,
        org_id=org_id,
    )


async def list_documents(
    db: AsyncSession,
    *,
    user_id: UUID,
    page: int,
    page_size: int,
    collection_id: UUID | None = None,
    org_id: UUID | None = None,
) -> tuple[list[Document], int]:
    """Own documents plus every document shared through one of the org's collections."""
    base = select(Document).where(Document.deleted_at.is_(None))
    if org_id is not None:
        base = base.where(
            Document.org_id == org_id, or_(Document.user_id == user_id, Document.collection_id.is_not(None))
        )
    else:
        base = base.where(Document.user_id == user_id)
    if collection_id is not None:
        base = base.where(Document.collection_id == collection_id)
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


async def get_document(
    db: AsyncSession, *, user_id: UUID, document_id: UUID, org_id: UUID | None = None
) -> Document:
    obj = await db.get(Document, document_id)
    if obj is None or obj.deleted_at is not None:
        raise NotFoundError("Document not found")
    if obj.user_id == user_id:
        return obj
    if org_id is not None and obj.org_id == org_id and obj.collection_id is not None:
        return obj  # shared through a collection of the active organization
    raise NotFoundError("Document not found")


async def owned_document(
    db: AsyncSession, *, user_id: UUID, document_id: UUID, membership: Membership | None = None
) -> Document:
    """Documents can be changed by their uploader or an admin of their organization."""
    obj = await db.get(Document, document_id)
    if obj is None or obj.deleted_at is not None:
        raise NotFoundError("Document not found")
    if obj.user_id == user_id:
        return obj
    if membership is not None and obj.org_id == membership.org_id and has_role(membership, OrgRole.ADMIN):
        return obj
    raise NotFoundError("Document not found")


_UNSET = object()


async def update_document(
    db: AsyncSession,
    *,
    user_id: UUID,
    document_id: UUID,
    name: str | None = None,
    collection_id: UUID | None | object = _UNSET,
    membership: Membership | None = None,
) -> Document:
    obj = await owned_document(db, user_id=user_id, document_id=document_id, membership=membership)
    if name is not None:
        obj.name = name.strip()[:512]
    if collection_id is not _UNSET:
        cid = collection_id if isinstance(collection_id, UUID) else None
        await _validate_collection(db, user_id=user_id, collection_id=cid, org_id=obj.org_id)
        obj.collection_id = cid
    await db.flush()
    await db.commit()
    await db.refresh(obj)
    return obj


async def list_chunks(
    db: AsyncSession,
    *,
    user_id: UUID,
    document_id: UUID,
    page: int,
    page_size: int,
    org_id: UUID | None = None,
) -> tuple[list[DocumentChunk], int]:
    await get_document(db, user_id=user_id, document_id=document_id, org_id=org_id)
    base = select(DocumentChunk).where(DocumentChunk.document_id == document_id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                base.order_by(DocumentChunk.chunk_index.asc()).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def get_chunk(
    db: AsyncSession, *, user_id: UUID, document_id: UUID, chunk_id: UUID, org_id: UUID | None = None
) -> DocumentChunk:
    await get_document(db, user_id=user_id, document_id=document_id, org_id=org_id)
    chunk = await db.get(DocumentChunk, chunk_id)
    if chunk is None or chunk.document_id != document_id:
        raise NotFoundError("Chunk not found")
    return chunk


async def soft_delete_document(
    db: AsyncSession, *, user_id: UUID, document_id: UUID, membership: Membership | None = None
) -> None:
    obj = await owned_document(db, user_id=user_id, document_id=document_id, membership=membership)
    obj.deleted_at = datetime.now(UTC)
    await db.flush()
    await db.commit()
    # Best effort: the row is the source of truth, the bytes are reclaimable.
    try:
        await get_storage().delete(obj.storage_path)
    except Exception:  # noqa: BLE001
        logger.warning("document_file_cleanup_failed", document_id=str(document_id))
