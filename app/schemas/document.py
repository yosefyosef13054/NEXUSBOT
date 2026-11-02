from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl

from app.models.document import DocumentStatus
from app.schemas.common import ORMBase


class DocumentRead(ORMBase):
    id: UUID
    name: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    org_id: UUID | None = None
    session_id: UUID | None = None
    collection_id: UUID | None = None
    source_url: str | None = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class DocumentIngestResponse(BaseModel):
    document: DocumentRead
    queued: bool = True


class DocumentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=512)
    # Explicit null moves the document out of its collection.
    collection_id: UUID | None = None
    model_config = {"extra": "forbid"}


class UrlIngestRequest(BaseModel):
    url: HttpUrl
    session_id: UUID | None = None
    collection_id: UUID | None = None


class ChunkRead(BaseModel):
    id: UUID
    chunk_index: int
    content: str
    tokens: int = 0
    page: int | None = None
    section: str | None = None
    start_index: int | None = None
    end_index: int | None = None
