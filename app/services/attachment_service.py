"""Message attachments: upload, sniffing, text extraction / transcription, binding to
messages, model inputs (vision + inline file text) and tool artifacts."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import aiofiles
from fastapi import UploadFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.rag.loader import load_file_as_documents
from app.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logger import get_logger
from app.models.attachment import Attachment, AttachmentKind
from app.services.session_service import get_session
from app.storage import get_storage, storage_key
from app.utils.files import is_valid_docx, sniff_mime, storage_path_for
from app.utils.media import image_size, sniff_audio, sniff_image

logger = get_logger(__name__)

_SNIFF_BYTES = 8192
INPUT_KINDS = (AttachmentKind.IMAGE, AttachmentKind.FILE, AttachmentKind.AUDIO)


@dataclass
class AttachmentInput:
    """What the model gets for one attachment (built at generation time)."""

    id: str
    kind: str
    name: str
    mime_type: str
    data_url: str | None = None  # images
    text: str | None = None  # extracted text / transcript


def content_url(att: Attachment) -> str:
    return f"{settings.API_V1_PREFIX}/sessions/{att.session_id}/attachments/{att.id}/content"


def summary(att: Attachment) -> dict[str, Any]:
    """Compact form stored in `ChatMessage.meta.attachments` and sent in stream events."""
    return {
        "id": str(att.id),
        "kind": att.kind.value,
        "name": att.name,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "width": att.width,
        "height": att.height,
        "url": content_url(att),
        "description": att.description,
        "has_text": bool(att.text_excerpt),
    }


def read_payload(att: Attachment, *, include_text: bool = False) -> dict[str, Any]:
    return {
        **summary(att),
        "session_id": att.session_id,
        "message_id": att.message_id,
        "text_excerpt": att.text_excerpt if include_text else None,
        "created_at": att.created_at,
    }


# --------------------------------------------------------------------------- sniffing


def classify(head: bytes, filename: str | None) -> tuple[AttachmentKind, str]:
    """(kind, mime) from content; raises 422 for anything not allowed."""
    image = sniff_image(head)
    if image:
        if image not in settings.ALLOWED_IMAGE_TYPES:
            raise ValidationError(
                f"Image type {image} is not allowed", details={"allowed": settings.ALLOWED_IMAGE_TYPES}
            )
        return AttachmentKind.IMAGE, image
    audio = sniff_audio(head)
    if audio:
        if audio not in settings.ALLOWED_AUDIO_TYPES:
            raise ValidationError(
                f"Audio type {audio} is not allowed", details={"allowed": settings.ALLOWED_AUDIO_TYPES}
            )
        return AttachmentKind.AUDIO, audio
    ext = Path(filename or "").suffix.lower()
    allowed = [e.lower() for e in settings.ALLOWED_UPLOAD_EXTENSIONS]
    if not ext or ext not in allowed:
        raise ValidationError(
            f"Unsupported attachment type '{ext or '(none)'}'",
            details={"allowed_extensions": allowed, "allowed_images": settings.ALLOWED_IMAGE_TYPES},
        )
    mime = sniff_mime(head, ext)
    if mime is None:
        raise ValidationError("File content does not match its extension", details={"extension": ext})
    return AttachmentKind.FILE, mime


async def _save_to_temp(file: UploadFile, dest: Path) -> tuple[int, bytes]:
    size = 0
    head = b""
    max_bytes = settings.MAX_ATTACHMENT_MB * 1024 * 1024
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            if len(head) < _SNIFF_BYTES:
                head += chunk[: _SNIFF_BYTES - len(head)]
            size += len(chunk)
            if size > max_bytes:
                raise ValidationError(f"Attachment exceeds {settings.MAX_ATTACHMENT_MB} MB limit")
            await out.write(chunk)
    return size, head


def _extract_text(path: Path) -> str:
    docs = load_file_as_documents(path)
    text = "\n\n".join(d.page_content for d in docs if d.page_content)
    return text[: settings.ATTACHMENT_TEXT_MAX_CHARS]


async def _transcribe_file(path: Path, *, name: str, mime: str) -> str | None:
    from app.ai.voice import stt

    if not stt.available():
        return None
    try:
        async with aiofiles.open(path, "rb") as f:
            data = await f.read()
        result = await stt.transcribe(data, filename=name, mime_type=mime)
        return result.text[: settings.ATTACHMENT_TEXT_MAX_CHARS] or None
    except Exception as e:  # noqa: BLE001 — a failed transcript must not fail the upload
        logger.warning("attachment_transcribe_failed", error=str(e)[:200])
        return None


# --------------------------------------------------------------------------- CRUD


async def upload(
    db: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID | None,
    session_id: UUID,
    file: UploadFile,
) -> Attachment:
    """Store an upload for `session_id`; it is bound to a message by the next send that
    lists it in `attachment_ids`."""
    await get_session(db, user_id=user_id, session_id=session_id)
    name = (file.filename or "attachment")[:512]
    dest = storage_path_for(str(user_id), name)
    try:
        size, head = await _save_to_temp(file, dest)
        if size == 0:
            raise ValidationError("Empty file")
        kind, mime = classify(head, file.filename)
        if kind == AttachmentKind.FILE and Path(name).suffix.lower() == ".docx":
            if not await asyncio.to_thread(is_valid_docx, dest):
                raise ValidationError(
                    "File content does not match its extension", details={"extension": ".docx"}
                )
        width = height = None
        text: str | None = None
        if kind == AttachmentKind.IMAGE:
            async with aiofiles.open(dest, "rb") as f:
                dims = image_size(await f.read(1 << 20))
            if dims:
                width, height = dims
        elif kind == AttachmentKind.FILE:
            try:
                text = await asyncio.to_thread(_extract_text, dest)
            except Exception as e:  # noqa: BLE001
                logger.warning("attachment_text_extract_failed", error=str(e)[:200])
        elif kind == AttachmentKind.AUDIO:
            text = await _transcribe_file(dest, name=name, mime=mime)
        key = storage_key("attachments", str(user_id), name)
        await get_storage().put_file(key, dest, content_type=mime)
    except Exception:
        await asyncio.to_thread(lambda: dest.unlink(missing_ok=True))
        raise

    att = Attachment(
        user_id=user_id,
        org_id=org_id,
        session_id=session_id,
        kind=kind,
        name=name,
        mime_type=mime,
        size_bytes=size,
        storage_key=key,
        width=width,
        height=height,
        text_excerpt=text,
        meta={},
    )
    db.add(att)
    await db.flush()
    return att


async def store_bytes(
    db: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID | None,
    session_id: UUID,
    kind: AttachmentKind,
    name: str,
    data: bytes,
    mime_type: str,
    message_id: UUID | None = None,
    description: str | None = None,
    text: str | None = None,
    meta: dict[str, Any] | None = None,
) -> Attachment:
    """Persist bytes produced by the server (tool artifacts, synthesized speech)."""
    key = storage_key("attachments", str(user_id), name)
    await get_storage().put_bytes(key, data, content_type=mime_type)
    width = height = None
    if kind == AttachmentKind.IMAGE or mime_type.startswith("image/"):
        dims = image_size(data[: 1 << 20])
        if dims:
            width, height = dims
    att = Attachment(
        user_id=user_id,
        org_id=org_id,
        session_id=session_id,
        message_id=message_id,
        kind=kind,
        name=name[:512],
        mime_type=mime_type,
        size_bytes=len(data),
        storage_key=key,
        width=width,
        height=height,
        description=description,
        text_excerpt=text,
        meta=meta or {},
    )
    db.add(att)
    await db.flush()
    return att


async def get_attachment(
    db: AsyncSession, *, user_id: UUID, session_id: UUID, attachment_id: UUID
) -> Attachment:
    att = await db.get(Attachment, attachment_id)
    if att is None or att.deleted_at is not None or att.session_id != session_id:
        raise NotFoundError("Attachment not found")
    await get_session(db, user_id=user_id, session_id=session_id)
    return att


async def list_for_session(db: AsyncSession, *, user_id: UUID, session_id: UUID) -> list[Attachment]:
    await get_session(db, user_id=user_id, session_id=session_id)
    stmt = (
        select(Attachment)
        .where(Attachment.session_id == session_id, Attachment.deleted_at.is_(None))
        .order_by(Attachment.created_at.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def claim(db: AsyncSession, *, ids: list[UUID], user_id: UUID, session_id: UUID) -> list[Attachment]:
    """Resolve the attachments a send refers to: this session, this user, not yet bound."""
    if not ids:
        return []
    rows = (
        (await db.execute(select(Attachment).where(Attachment.id.in_(ids), Attachment.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    by_id = {r.id: r for r in rows}
    missing = [str(i) for i in ids if i not in by_id]
    if missing:
        raise NotFoundError("Attachment not found", details={"attachment_ids": missing})
    out = []
    for i in ids:
        att = by_id[i]
        if att.session_id != session_id or att.user_id != user_id:
            raise NotFoundError("Attachment not found", details={"attachment_ids": [str(i)]})
        if att.kind not in INPUT_KINDS:
            raise ValidationError("Only images, files and audio can be attached to a message")
        if att.message_id is not None:
            raise ValidationError(
                "Attachment already belongs to a message", details={"attachment_id": str(i)}
            )
        out.append(att)
    return out


async def bind(db: AsyncSession, rows: list[Attachment], *, message_id: UUID) -> None:
    for att in rows:
        att.message_id = message_id
    if rows:
        await db.flush()


async def bind_ids(db: AsyncSession, ids: list[UUID], *, message_id: UUID) -> None:
    if ids:
        await db.execute(update(Attachment).where(Attachment.id.in_(ids)).values(message_id=message_id))


async def soft_delete(db: AsyncSession, *, user_id: UUID, session_id: UUID, attachment_id: UUID) -> None:
    att = await get_attachment(db, user_id=user_id, session_id=session_id, attachment_id=attachment_id)
    att.deleted_at = datetime.now(UTC)
    await db.flush()
    try:
        await get_storage().delete(att.storage_key)
    except Exception:  # noqa: BLE001
        logger.warning("attachment_file_cleanup_failed", attachment_id=str(attachment_id))


# --------------------------------------------------------------------------- model inputs


async def load_inputs(db: AsyncSession, ids: list[UUID]) -> list[AttachmentInput]:
    """Build what the model sees: images as data URLs, files/audio as text."""
    if not ids:
        return []
    rows = (await db.execute(select(Attachment).where(Attachment.id.in_(ids)))).scalars().all()
    by_id = {r.id: r for r in rows}
    out: list[AttachmentInput] = []
    for i in ids:
        att = by_id.get(i)
        if att is None:
            continue
        item = AttachmentInput(id=str(att.id), kind=att.kind.value, name=att.name, mime_type=att.mime_type)
        if att.kind == AttachmentKind.IMAGE:
            data = await get_storage().get_bytes(att.storage_key)
            item.data_url = f"data:{att.mime_type};base64,{base64.b64encode(data).decode()}"
        else:
            item.text = att.text_excerpt
        out.append(item)
    return out


def has_images(inputs: list[AttachmentInput]) -> bool:
    return any(i.kind == "image" for i in inputs)
