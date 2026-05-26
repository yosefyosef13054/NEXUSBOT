from __future__ import annotations

from datetime import datetime
from typing import Any, List, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.chat import MessageRole
from app.schemas.common import ORMBase


class SessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    system_prompt: str | None = None
    model: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SessionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    system_prompt: str | None = None
    meta: dict[str, Any] | None = None


class SessionRead(ORMBase):
    id: UUID
    title: str
    model: str | None = None
    system_prompt: str | None = None
    summary: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class MessageRead(ORMBase):
    id: UUID
    session_id: UUID
    role: MessageRole
    content: str
    tokens: int
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    # Toggle the RAG pipeline; defaults to true so document-aware chats just work.
    use_rag: bool = True
    # Enable tool calling for this turn.
    use_tools: bool = False
    tool_names: list[str] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=16, le=8192)


class Citation(BaseModel):
    document_id: UUID
    document_name: str
    chunk_index: int
    score: float
    excerpt: str


class SendMessageResponse(BaseModel):
    user_message: MessageRead
    assistant_message: MessageRead
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)


class StreamEvent(BaseModel):
    """Wire format for SSE / WebSocket streaming events."""

    type: Literal[
        "session", "user_message", "token", "tool_call", "citations", "usage", "done", "error"
    ]
    data: Any


class RegenerateRequest(BaseModel):
    message_id: UUID | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=16, le=8192)


class RegenerateResponse(BaseModel):
    assistant_message: MessageRead
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)


class SearchResult(BaseModel):
    session_id: UUID
    session_title: str
    message_id: UUID
    role: MessageRole
    snippet: str
    created_at: datetime


class HistoryQuery(BaseModel):
    cursor: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)


class HistoryResponse(BaseModel):
    items: List[MessageRead]
    next_cursor: datetime | None = None
