from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import current_user
from app.core.exceptions import ServiceUnavailableError
from app.core.logger import get_logger
from app.core.rate_limit import rate_limit_dependency
from app.db.session import get_db
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.models.user import User
from app.schemas.common import OkResponse, Page
from app.schemas.document import (
    ChunkRead,
    DocumentIngestResponse,
    DocumentRead,
    DocumentUpdateRequest,
    UrlIngestRequest,
)
from app.services import document_service
from app.workers.tasks import ingest_document_task

router = APIRouter(prefix="/documents", tags=["documents"])
logger = get_logger(__name__)


async def _enqueue(db: AsyncSession, doc: Document, created: bool) -> DocumentIngestResponse:
    if not created:
        return DocumentIngestResponse(document=DocumentRead.model_validate(doc), queued=False)
    try:
        ingest_document_task.delay(str(doc.id))
    except Exception as e:  # noqa: BLE001 — broker down
        logger.exception("ingest_enqueue_failed", document_id=str(doc.id))
        doc.status = DocumentStatus.FAILED
        doc.error = f"Could not queue ingestion: {str(e)[:200]}"
        await db.commit()
        raise ServiceUnavailableError("Ingestion queue unavailable; please retry later")
    return DocumentIngestResponse(document=DocumentRead.model_validate(doc), queued=True)


def _chunk_read(c: DocumentChunk) -> ChunkRead:
    meta = c.meta or {}
    return ChunkRead(
        id=c.id,
        chunk_index=c.chunk_index,
        content=c.content,
        tokens=c.tokens,
        page=meta.get("page"),
        section=meta.get("section"),
        start_index=meta.get("start_index"),
        end_index=meta.get("end_index"),
    )


@router.post(
    "",
    response_model=DocumentIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit_dependency("documents_upload", requests=30))],
)
async def upload(
    file: UploadFile = File(...),
    session_id: UUID | None = Form(default=None),
    collection_id: UUID | None = Form(default=None),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    doc, created = await document_service.upload_document(
        db, user_id=user.id, file=file, session_id=session_id, collection_id=collection_id
    )
    return await _enqueue(db, doc, created)


@router.post(
    "/from-url",
    response_model=DocumentIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit_dependency("documents_upload", requests=30))],
)
async def ingest_url(
    payload: UrlIngestRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    doc, created = await document_service.ingest_from_url(
        db,
        user_id=user.id,
        url=str(payload.url),
        session_id=payload.session_id,
        collection_id=payload.collection_id,
    )
    return await _enqueue(db, doc, created)


@router.get("", response_model=Page[DocumentRead])
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    collection_id: UUID | None = Query(default=None),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    items, total = await document_service.list_documents(
        db, user_id=user.id, page=page, page_size=page_size, collection_id=collection_id
    )
    return Page(
        items=[DocumentRead.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{document_id}", response_model=DocumentRead)
async def get(
    document_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return await document_service.get_document(db, user_id=user.id, document_id=document_id)


@router.patch("/{document_id}", response_model=DocumentRead)
async def patch(
    document_id: UUID,
    payload: DocumentUpdateRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kwargs: dict = {"name": payload.name}
    if "collection_id" in payload.model_fields_set:
        kwargs["collection_id"] = payload.collection_id
    return await document_service.update_document(db, user_id=user.id, document_id=document_id, **kwargs)


@router.get("/{document_id}/chunks", response_model=Page[ChunkRead])
async def list_chunks(
    document_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    items, total = await document_service.list_chunks(
        db, user_id=user.id, document_id=document_id, page=page, page_size=page_size
    )
    return Page(items=[_chunk_read(c) for c in items], total=total, page=page, page_size=page_size)


@router.get("/{document_id}/chunks/{chunk_id}", response_model=ChunkRead)
async def get_chunk(
    document_id: UUID,
    chunk_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return _chunk_read(
        await document_service.get_chunk(db, user_id=user.id, document_id=document_id, chunk_id=chunk_id)
    )


@router.delete("/{document_id}", response_model=OkResponse)
async def delete(
    document_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await document_service.soft_delete_document(db, user_id=user.id, document_id=document_id)
    return OkResponse(message="Document deleted")
