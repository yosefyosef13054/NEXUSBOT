from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.common import OkResponse, Page
from app.schemas.document import DocumentIngestResponse, DocumentRead
from app.services import document_service
from app.workers.tasks import ingest_document_task

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "",
    response_model=DocumentIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload(
    file: UploadFile = File(...),
    session_id: UUID | None = Form(default=None),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await document_service.upload_document(
        db, user_id=user.id, file=file, session_id=session_id
    )
    ingest_document_task.delay(str(doc.id))
    return DocumentIngestResponse(document=DocumentRead.model_validate(doc), queued=True)


@router.get("", response_model=Page[DocumentRead])
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    items, total = await document_service.list_documents(
        db, user_id=user.id, page=page, page_size=page_size
    )
    return Page(
        items=[DocumentRead.model_validate(i) for i in items],
        total=total, page=page, page_size=page_size,
    )


@router.get("/{document_id}", response_model=DocumentRead)
async def get(
    document_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    return await document_service.get_document(db, user_id=user.id, document_id=document_id)


@router.delete("/{document_id}", response_model=OkResponse)
async def delete(
    document_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await document_service.soft_delete_document(db, user_id=user.id, document_id=document_id)
    return OkResponse(message="Document deleted")
