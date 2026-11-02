from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.dependencies import Principal, current_principal
from app.core.rate_limit import rate_limit_dependency
from app.db.session import get_db
from app.models.attachment import Attachment
from app.schemas.attachment import AttachmentListResponse, AttachmentRead
from app.schemas.common import OkResponse
from app.services import attachment_service
from app.storage import get_storage
from app.utils.files import safe_filename

router = APIRouter(prefix="/sessions/{session_id}/attachments", tags=["attachments"])


def _read(att: Attachment, *, include_text: bool = False) -> AttachmentRead:
    return AttachmentRead(**attachment_service.read_payload(att, include_text=include_text))


@router.post(
    "",
    response_model=AttachmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit_dependency("attachments_upload", requests=60))],
)
async def upload_attachment(
    session_id: UUID,
    file: UploadFile = File(...),
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Upload an image, audio clip or file for this session.

    Bind it to a message by listing its id in `attachment_ids` of the next send. Images go
    to vision-capable models; files have their text extracted; audio is transcribed when
    speech-to-text is configured (`text_excerpt`).
    """
    att = await attachment_service.upload(
        db, user_id=principal.user_id, org_id=principal.org_id, session_id=session_id, file=file
    )
    await db.commit()
    return _read(att, include_text=True)


@router.get("", response_model=AttachmentListResponse)
async def list_attachments(
    session_id: UUID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    rows = await attachment_service.list_for_session(db, user_id=principal.user_id, session_id=session_id)
    return AttachmentListResponse(items=[_read(a) for a in rows])


@router.get("/{attachment_id}", response_model=AttachmentRead)
async def get_attachment(
    session_id: UUID,
    attachment_id: UUID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    att = await attachment_service.get_attachment(
        db, user_id=principal.user_id, session_id=session_id, attachment_id=attachment_id
    )
    return _read(att, include_text=True)


@router.get("/{attachment_id}/content")
async def attachment_content(
    session_id: UUID,
    attachment_id: UUID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """The bytes (streamed), or a redirect to a presigned URL on S3-backed storage."""
    att = await attachment_service.get_attachment(
        db, user_id=principal.user_id, session_id=session_id, attachment_id=attachment_id
    )
    storage = get_storage()
    url = await storage.presigned_url(
        att.storage_key, expires_seconds=settings.S3_PRESIGN_SECONDS, filename=att.name
    )
    if url:
        return RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    headers = {
        "Content-Disposition": f'inline; filename="{safe_filename(att.name)}"',
        "Content-Length": str(att.size_bytes),
        "Cache-Control": "private, max-age=3600",
    }
    return StreamingResponse(storage.stream(att.storage_key), media_type=att.mime_type, headers=headers)


@router.delete("/{attachment_id}", response_model=OkResponse)
async def delete_attachment(
    session_id: UUID,
    attachment_id: UUID,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    await attachment_service.soft_delete(
        db, user_id=principal.user_id, session_id=session_id, attachment_id=attachment_id
    )
    await db.commit()
    return OkResponse(message="Attachment deleted")
