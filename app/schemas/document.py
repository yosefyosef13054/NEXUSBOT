from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.document import DocumentStatus
from app.schemas.common import ORMBase


class DocumentRead(ORMBase):
    id: UUID
    name: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    session_id: UUID | None = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class DocumentIngestResponse(BaseModel):
    document: DocumentRead
    queued: bool = True
