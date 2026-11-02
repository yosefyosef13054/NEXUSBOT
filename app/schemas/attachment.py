from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

AttachmentKindLiteral = Literal["image", "file", "audio", "artifact"]


class AttachmentRead(BaseModel):
    id: UUID
    session_id: UUID
    message_id: UUID | None = None
    kind: AttachmentKindLiteral
    name: str
    mime_type: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    # Download endpoint (always valid; may redirect to a presigned URL on S3).
    url: str
    description: str | None = None
    # True when the model will see extracted text / a transcript for this attachment.
    has_text: bool = False
    text_excerpt: str | None = None
    created_at: datetime


class AttachmentListResponse(BaseModel):
    items: list[AttachmentRead] = Field(default_factory=list)


class TranscriptionResponse(BaseModel):
    text: str
    language: str | None = None
    duration_seconds: float | None = None
    provider: str


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str | None = Field(default=None, max_length=64)
    format: Literal["mp3", "wav", "opus", "aac", "flac"] | None = None


class VoiceInfo(BaseModel):
    id: str
    name: str


class VoiceListResponse(BaseModel):
    provider: str
    stt_available: bool
    tts_available: bool
    default_voice: str | None = None
    default_format: str | None = None
    voices: list[VoiceInfo] = Field(default_factory=list)


class VoiceTurnResponse(BaseModel):
    generation_id: str
    transcript: str
    user_message_id: UUID
    attachment_id: UUID | None = None
