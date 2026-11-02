from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ShareCreateRequest(BaseModel):
    expires_in_hours: int | None = Field(default=None, ge=1, le=24 * 365)


class ShareRead(BaseModel):
    id: UUID
    # Only returned when the link is created.
    token: str | None = None
    url: str | None = None
    expires_at: datetime
    views: int = 0


class ShareListResponse(BaseModel):
    items: list[ShareRead] = Field(default_factory=list)


class SharedTranscript(BaseModel):
    session: dict[str, Any]
    messages: list[dict[str, Any]]
    exported_at: str
    expires_at: datetime


class DeviceRegisterRequest(BaseModel):
    platform: str = Field(pattern="^(ios|android|web)$")
    token: str = Field(min_length=8, max_length=1024)
    app_version: str | None = Field(default=None, max_length=64)
    locale: str | None = Field(default=None, max_length=16)


class DeviceRead(BaseModel):
    id: UUID
    platform: str
    token_suffix: str
    app_version: str | None = None
    locale: str | None = None
    last_seen_at: datetime | None = None
    created_at: datetime


class DeviceListResponse(BaseModel):
    items: list[DeviceRead] = Field(default_factory=list)
    push_enabled: bool = False
