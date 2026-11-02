from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

TransportLiteral = Literal["stdio", "http", "sse"]


class MCPServerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str | None = Field(default=None, max_length=2000)
    transport: TransportLiteral
    command: str | None = Field(default=None, max_length=512)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = Field(default=None, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    requires_approval: bool = True


class MCPServerUpdateRequest(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    command: str | None = Field(default=None, max_length=512)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = Field(default=None, max_length=2048)
    headers: dict[str, str] | None = None
    enabled: bool | None = None
    requires_approval: bool | None = None
    model_config = {"extra": "forbid"}


class MCPServerRead(BaseModel):
    id: UUID
    org_id: UUID
    name: str
    description: str | None = None
    transport: TransportLiteral
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env_keys: list[str] = Field(default_factory=list)
    url: str | None = None
    header_keys: list[str] = Field(default_factory=list)
    enabled: bool
    requires_approval: bool
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime


class MCPToolRead(BaseModel):
    name: str
    server: str
    tool: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = True


class MCPServerTestResponse(BaseModel):
    ok: bool
    tools: list[MCPToolRead] = Field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0


class MCPServerListResponse(BaseModel):
    items: list[MCPServerRead]
