from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

MemoryKindLiteral = Literal["fact", "preference", "instruction"]


class MemoryRead(BaseModel):
    id: UUID
    content: str
    kind: MemoryKindLiteral
    importance: int
    source_message_id: UUID | None = None
    created_at: datetime
    last_used_at: datetime | None = None


class MemoryCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    kind: MemoryKindLiteral = "fact"
    importance: int = Field(default=3, ge=1, le=5)


class MemoryUpdateRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    kind: MemoryKindLiteral | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    model_config = {"extra": "forbid"}
