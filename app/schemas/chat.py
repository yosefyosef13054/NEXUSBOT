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
    model: str | None = Field(default=None, max_length=64)
    assistant_id: UUID | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SessionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    system_prompt: str | None = None
    model: str | None = Field(default=None, max_length=64)
    assistant_id: UUID | None = None
    meta: dict[str, Any] | None = None


class SessionRead(ORMBase):
    id: UUID
    org_id: UUID | None = None
    assistant_id: UUID | None = None
    active_leaf_id: UUID | None = None
    title: str
    model: str | None = None
    system_prompt: str | None = None
    summary: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class SiblingInfo(BaseModel):
    index: int
    count: int


class MessageRead(ORMBase):
    id: UUID
    session_id: UUID
    parent_id: UUID | None = None
    role: MessageRole
    content: str
    tokens: int
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    # Set by history/sibling endpoints when the message has edit/regeneration siblings.
    siblings: SiblingInfo | None = None


class BranchRequest(BaseModel):
    leaf_message_id: UUID


class SiblingsResponse(BaseModel):
    items: List[MessageRead]
    active_index: int = 0


class BackgroundSendResponse(BaseModel):
    generation_id: str
    user_message: MessageRead


class GenerationActiveResponse(BaseModel):
    generation_id: str
    started_at: datetime
    status: str


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    # Toggle the RAG pipeline; defaults to true so document-aware chats just work.
    use_rag: bool = True
    # Enable tool calling for this turn.
    use_tools: bool = False
    tool_names: list[str] | None = None
    # Per-turn model override (validated against GET /models).
    model: str | None = Field(default=None, max_length=64)
    # Restrict retrieval to these collections (None = session default / all documents).
    collection_ids: list[UUID] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=16, le=8192)
    # Attachments uploaded to this session (POST /sessions/{id}/attachments) to bind to
    # this message: images for vision models, files/audio as extracted text.
    attachment_ids: list[UUID] | None = None
    # Speak the reply: `audio_chunk` events (base64) before `assistant_message`.
    voice_reply: bool = False
    voice: str | None = Field(default=None, max_length=64)
    audio_format: Literal["mp3", "wav", "opus", "aac", "flac"] | None = None


class Citation(BaseModel):
    document_id: UUID
    document_name: str
    chunk_id: UUID | None = None
    chunk_index: int
    page: int | None = None
    section: str | None = None
    start_index: int | None = None
    end_index: int | None = None
    collection_id: UUID | None = None
    score: float
    excerpt: str


class SendMessageResponse(BaseModel):
    user_message: MessageRead
    assistant_message: MessageRead
    tool_messages: list[MessageRead] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    approval: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class StreamEvent(BaseModel):
    """Wire format for SSE / WebSocket streaming events."""

    type: Literal[
        "auth_ok",
        "pong",
        "session",
        "user_message",
        "token",
        "tool_call",
        "citations",
        "usage",
        "approval_required",
        "artifact",
        "audio_chunk",
        "audio_done",
        "done",
        "assistant_message",
        "error",
    ]
    data: Any = None


class ApprovalToolCall(BaseModel):
    call_id: str | None = None
    tool: str
    input: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class ApprovalRead(BaseModel):
    approval_id: UUID
    session_id: UUID
    message_id: UUID | None = None
    status: str
    expires_at: datetime
    tool_calls: list[ApprovalToolCall] = Field(default_factory=list)


class ApprovalListResponse(BaseModel):
    items: list[ApprovalRead]


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "deny"]
    # Approve/deny only these calls (others are denied). None = all pending calls.
    tool_call_ids: list[str] | None = None


class RegenerateRequest(BaseModel):
    message_id: UUID | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=16, le=8192)


class RegenerateResponse(BaseModel):
    assistant_message: MessageRead
    tool_messages: list[MessageRead] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)


class SearchResult(BaseModel):
    session_id: UUID
    session_title: str
    message_id: UUID
    role: MessageRole
    snippet: str
    highlight: str | None = None
    rank: float = 0.0
    created_at: datetime


class HistoryQuery(BaseModel):
    cursor: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)


class HistoryResponse(BaseModel):
    items: List[MessageRead]
    next_cursor: datetime | None = None
