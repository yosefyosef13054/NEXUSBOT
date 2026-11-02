from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.voice import stt, tts
from app.config import settings
from app.core import idempotency
from app.core.dependencies import Principal, current_principal
from app.core.exceptions import ServiceUnavailableError, ValidationError, request_id_of
from app.core.rate_limit import rate_limit_dependency
from app.db.session import get_db
from app.models.attachment import AttachmentKind
from app.schemas.attachment import (
    SpeechRequest,
    TranscriptionResponse,
    VoiceInfo,
    VoiceListResponse,
    VoiceTurnResponse,
)
from app.services import attachment_service, chat_service
from app.services.chat_service import TurnScope
from app.utils.files import safe_filename
from app.utils.media import sniff_audio

router = APIRouter(prefix="/audio", tags=["voice"])
voice_router = APIRouter(prefix="/sessions/{session_id}/messages", tags=["voice"])

MAX_AUDIO_BYTES = 25 * 1024 * 1024


async def _read_audio(file: UploadFile) -> tuple[bytes, str]:
    data = await file.read(MAX_AUDIO_BYTES + 1)
    if not data:
        raise ValidationError("Empty audio file")
    if len(data) > MAX_AUDIO_BYTES:
        raise ValidationError("Audio exceeds 25 MB")
    mime = sniff_audio(data[:64])
    if mime is None:
        raise ValidationError("Unsupported audio format", details={"allowed": settings.ALLOWED_AUDIO_TYPES})
    return data, mime


@router.get("/voices", response_model=VoiceListResponse)
async def list_voices(_: Principal = Depends(current_principal)):
    """What speech features this server offers."""
    return VoiceListResponse(
        provider=tts.provider_name() or stt.provider_name() or "none",
        stt_available=stt.available(),
        tts_available=tts.available(),
        default_voice=settings.TTS_VOICE if tts.available() else None,
        default_format=tts.default_format() if tts.available() else None,
        voices=[VoiceInfo(id=v, name=v.title()) for v in tts.voices()],
    )


@router.post(
    "/transcriptions",
    response_model=TranscriptionResponse,
    dependencies=[Depends(rate_limit_dependency("audio", requests=60))],
)
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(default=None, max_length=8),
    _: Principal = Depends(current_principal),
):
    """Speech-to-text (push-to-talk): upload a clip, get the text back."""
    if not stt.available():
        raise ServiceUnavailableError(
            "Speech-to-text is not configured", details={"code": "voice_unavailable"}
        )
    data, mime = await _read_audio(file)
    result = await stt.transcribe(data, filename=file.filename or "audio", mime_type=mime, language=language)
    return TranscriptionResponse(
        text=result.text,
        language=result.language,
        duration_seconds=result.duration_seconds,
        provider=result.provider,
    )


@router.post("/speech", dependencies=[Depends(rate_limit_dependency("audio", requests=60))])
async def speech(payload: SpeechRequest, _: Principal = Depends(current_principal)):
    """Text-to-speech: streams audio bytes (`Content-Type` from the format)."""
    if not tts.available():
        raise ServiceUnavailableError(
            "Text-to-speech is not configured", details={"code": "voice_unavailable"}
        )
    if payload.voice and payload.voice not in tts.voices():
        raise ValidationError(f"Unknown voice '{payload.voice}'", details={"voices": tts.voices()})
    fmt = payload.format or tts.default_format()
    return StreamingResponse(
        tts.synthesize(payload.text, voice=payload.voice, fmt=fmt),
        media_type=tts.mime_for(fmt),
        headers={"Content-Disposition": f'inline; filename="speech.{fmt}"', "Cache-Control": "no-store"},
    )


@voice_router.post(
    "/voice",
    response_model=VoiceTurnResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit_dependency("chat_stream"))],
)
async def voice_turn(
    session_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    language: str | None = Form(default=None, max_length=8),
    use_rag: bool = Form(default=True),
    use_tools: bool = Form(default=False),
    model: str | None = Form(default=None, max_length=64),
    reply_audio: bool = Form(default=True),
    voice: str | None = Form(default=None, max_length=64),
    audio_format: str | None = Form(default=None, max_length=8),
    idempotency_key: Annotated[str | None, Header(alias=idempotency.HEADER, max_length=128)] = None,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """One round trip of voice mode: transcribe the clip, start the turn in the background
    (with `audio_chunk` events when `reply_audio`), return the transcript and generation
    id. Attach with `GET /sessions/{id}/generations/{generation_id}/events`.
    """
    if not stt.available():
        raise ServiceUnavailableError(
            "Speech-to-text is not configured", details={"code": "voice_unavailable"}
        )
    data, mime = await _read_audio(file)
    result = await stt.transcribe(data, filename=file.filename or "voice", mime_type=mime, language=language)
    text = result.text.strip()
    if not text:
        raise ValidationError("Nothing was recognized in the audio")
    att = await attachment_service.store_bytes(
        db,
        user_id=principal.user_id,
        org_id=principal.org_id,
        session_id=session_id,
        kind=AttachmentKind.AUDIO,
        name=safe_filename(file.filename or f"voice.{mime.split('/')[-1]}"),
        data=data,
        mime_type=mime,
        text=text,
        meta={"language": result.language, "duration_seconds": result.duration_seconds},
    )
    await db.commit()
    handle = await chat_service.start_turn(
        db,
        scope=TurnScope.from_principal(principal),
        session_id=session_id,
        content=text,
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=None,
        temperature=None,
        max_tokens=None,
        model=model,
        explicit={"use_rag", "use_tools"},
        request_id=request_id_of(request),
        idempotency_key=idempotency_key,
        attachment_ids=[att.id],
        voice_reply=reply_audio and tts.available(),
        voice=voice,
        audio_format=audio_format,
        notify=True,
    )
    return VoiceTurnResponse(
        generation_id=handle.generation_id,
        transcript=text,
        user_message_id=handle.user_message.id if handle.user_message else att.message_id or att.id,
        attachment_id=att.id,
    )
