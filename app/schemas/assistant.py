from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AssistantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    avatar: str | None = Field(default=None, max_length=16)
    system_prompt: str | None = Field(default=None, max_length=20000)
    model: str | None = Field(default=None, max_length=64)
    tool_names: list[str] = Field(default_factory=list)
    collection_ids: list[UUID] = Field(default_factory=list)
    use_rag: bool = True
    use_tools: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    memory_enabled: bool = True
    is_default: bool = False


class AssistantUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    avatar: str | None = Field(default=None, max_length=16)
    system_prompt: str | None = Field(default=None, max_length=20000)
    model: str | None = Field(default=None, max_length=64)
    tool_names: list[str] | None = None
    collection_ids: list[UUID] | None = None
    use_rag: bool | None = None
    use_tools: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    memory_enabled: bool | None = None
    is_default: bool | None = None
    model_config = {"extra": "forbid"}


class AssistantRead(BaseModel):
    id: UUID
    org_id: UUID
    name: str
    description: str | None = None
    avatar: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    collection_ids: list[UUID] = Field(default_factory=list)
    use_rag: bool
    use_tools: bool
    temperature: float | None = None
    memory_enabled: bool
    is_default: bool
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime
