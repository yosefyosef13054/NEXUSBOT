from __future__ import annotations

from pydantic import BaseModel, Field


class ModelRead(BaseModel):
    id: str
    provider: str
    display_name: str
    context_window: int
    max_output_tokens: int
    supports_tools: bool
    supports_vision: bool
    supports_streaming: bool
    input_usd_per_1m: float
    output_usd_per_1m: float
    tags: list[str] = Field(default_factory=list)
    is_default: bool = False


class ModelListResponse(BaseModel):
    default_model: str | None
    items: list[ModelRead]


class ToolRead(BaseModel):
    name: str
    description: str
    requires_approval: bool
    # "builtin" or "mcp:<server>" for tools served by a Model Context Protocol server.
    source: str = "builtin"


class ToolListResponse(BaseModel):
    items: list[ToolRead]
