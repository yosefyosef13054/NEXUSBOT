"""Chat turns: validation, assistant defaults, quotas, the message tree, generation logs
(resumable / background streaming), approvals, feedback hooks and memory extraction."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.orchestrator import (
    STATUS_AWAITING_APPROVAL,
    GenerationProgress,
    GenerationRequest,
    GenerationResult,
    citations_payload,
    generate,
    maybe_summarize,
    resume_stream,
    stream,
)
from app.ai.providers.registry import get_registry
from app.ai.tools.registry import list_tool_names, unknown_tool_names
from app.cache.redis_client import get_redis
from app.config import settings
from app.core import idempotency
from app.core.background import run_in_background, spawn_detached
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logger import get_logger
from app.core.metrics import APPROVALS
from app.core.streams import GenerationLog
from app.db.session import db_session
from app.models.approval import Approval, ApprovalStatus
from app.models.assistant import Assistant
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.organization import Membership, Organization
from app.models.tool_call import ToolCall
from app.schemas.chat import SearchResult
from app.services import memory_service, quota_service
from app.services.collection_service import ensure_collections
from app.services.org_service import allowed_model, org_settings
from app.services.session_service import autogenerate_title_if_needed, get_session, session_collection_ids
from app.services.usage_service import log_ai_usage
from app.utils.tokens import count_tokens

logger = get_logger(__name__)

TOOL_MESSAGE_MAX_CHARS = 8000
TURN_ROLES = (MessageRole.USER, MessageRole.ASSISTANT)


# --------------------------------------------------------------------------- scope / settings


@dataclass
class TurnScope:
    """Who is acting, in which workspace, with which preferences."""

    user_id: UUID
    org: Organization | None = None
    membership: Membership | None = None
    user_default_model: str | None = None
    memory_enabled: bool = True

    @property
    def org_id(self) -> UUID | None:
        return self.org.id if self.org is not None else None

    @classmethod
    def from_principal(cls, principal: Any) -> TurnScope:
        return cls(
            user_id=principal.user_id,
            org=principal.org,
            membership=principal.membership,
            user_default_model=principal.user.default_model,
            memory_enabled=bool(getattr(principal.user, "memory_enabled", True)),
        )


@dataclass
class TurnSettings:
    use_rag: bool
    use_tools: bool
    tool_names: list[str] | None
    model: str | None
    collection_ids: list[UUID] | None
    temperature: float | None
    max_tokens: int | None
    system_prompt: str | None = None
    assistant_model: str | None = None
    org_default_model: str | None = None
    use_memory: bool = False

    def generation_meta(self, model_used: str | None) -> dict[str, Any]:
        return {
            "use_rag": self.use_rag,
            "use_tools": self.use_tools,
            "tool_names": self.tool_names,
            "model": model_used,
            "collection_ids": [str(c) for c in self.collection_ids] if self.collection_ids else None,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    @classmethod
    def from_generation_meta(cls, gen: dict[str, Any], *, use_memory: bool = False) -> TurnSettings:
        raw = gen.get("collection_ids")
        return cls(
            use_rag=bool(gen.get("use_rag", True)),
            use_tools=bool(gen.get("use_tools", False)),
            tool_names=gen.get("tool_names"),
            model=gen.get("model") if get_registry().is_available(gen.get("model")) else None,
            collection_ids=[UUID(str(c)) for c in raw] if raw else None,
            temperature=gen.get("temperature"),
            max_tokens=gen.get("max_tokens"),
            system_prompt=gen.get("system_prompt"),
            use_memory=use_memory,
        )


def _validate_tools(use_tools: bool, tool_names: list[str] | None) -> None:
    if not use_tools:
        return
    unknown = unknown_tool_names(tool_names)
    if unknown:
        raise ValidationError(
            "Unknown tools requested", details={"unknown": unknown, "available": list_tool_names()}
        )


async def _assistant_for(db: AsyncSession, session_obj: ChatSession) -> Assistant | None:
    if session_obj.assistant_id is None:
        return None
    obj = await db.get(Assistant, session_obj.assistant_id)
    if obj is None or obj.deleted_at is not None:
        return None
    return obj


async def resolve_turn_settings(
    db: AsyncSession,
    *,
    scope: TurnScope,
    session_obj: ChatSession,
    use_rag: bool,
    use_tools: bool,
    tool_names: list[str] | None,
    model: str | None,
    collection_ids: list[UUID] | None,
    temperature: float | None,
    max_tokens: int | None,
    explicit: set[str] | None = None,
) -> TurnSettings:
    """Request fields win; then the session's assistant; then the session; then defaults."""
    explicit = explicit or set()
    assistant = await _assistant_for(db, session_obj)
    system_prompt = None
    assistant_model = None
    assistant_memory = True
    if assistant is not None:
        if "use_rag" not in explicit:
            use_rag = assistant.use_rag
        if "use_tools" not in explicit:
            use_tools = assistant.use_tools
        if tool_names is None and assistant.tool_names:
            tool_names = list(assistant.tool_names)
        if (
            collection_ids is None
            and session_collection_ids(session_obj) is None
            and assistant.collection_ids
        ):
            collection_ids = [UUID(str(c)) for c in assistant.collection_ids]
        if temperature is None:
            temperature = assistant.temperature
        system_prompt = assistant.system_prompt
        assistant_model = assistant.model if get_registry().is_available(assistant.model) else None
        assistant_memory = assistant.memory_enabled

    _validate_tools(use_tools, tool_names)
    get_registry().validate(model)

    org_default_model = None
    if scope.org is not None:
        for candidate in (model, session_obj.model, assistant_model):
            if candidate and not allowed_model(scope.org, candidate):
                raise ValidationError(
                    f"Model '{candidate}' is not allowed in this organization",
                    details={"allowed_models": org_settings(scope.org).allowed_models},
                )
        org_default_model = org_settings(scope.org).default_model

    if collection_ids is not None:
        await ensure_collections(db, ids=collection_ids, org_id=scope.org_id, user_id=scope.user_id)
        scoped: list[UUID] | None = list(collection_ids) or None
    else:
        scoped = session_collection_ids(session_obj)

    return TurnSettings(
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        model=model,
        collection_ids=scoped,
        temperature=temperature,
        max_tokens=max_tokens,
        system_prompt=system_prompt,
        assistant_model=assistant_model,
        org_default_model=org_default_model,
        use_memory=settings.MEMORY_ENABLED and scope.memory_enabled and assistant_memory,
    )


def _build_request(
    scope: TurnScope,
    session_obj: ChatSession,
    turn: TurnSettings,
    *,
    user_input: str,
    streaming: bool,
    request_id: str | None,
    stop_event: asyncio.Event | None = None,
    thread_id: str | None = None,
) -> GenerationRequest:
    req = GenerationRequest(
        user_id=scope.user_id,
        session=session_obj,
        user_input=user_input,
        use_rag=turn.use_rag,
        use_tools=turn.use_tools,
        tool_names=turn.tool_names,
        model=turn.model,
        user_default_model=scope.user_default_model,
        collection_ids=turn.collection_ids,
        org_id=scope.org_id,
        use_memory=turn.use_memory,
        system_prompt=turn.system_prompt,
        assistant_model=turn.assistant_model,
        org_default_model=turn.org_default_model,
        temperature=turn.temperature,
        max_tokens=turn.max_tokens,
        streaming=streaming,
        request_id=request_id,
        stop_event=stop_event,
    )
    if thread_id:
        req.thread_id = thread_id
    return req


async def _check_quota(db: AsyncSession, scope: TurnScope) -> None:
    if scope.org is not None:
        await quota_service.check_quota(db, scope.org)


# --------------------------------------------------------------------------- message tree


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class _Node:
    id: UUID
    parent_id: UUID | None
    role: MessageRole
    created_at: datetime


async def _load_tree(db: AsyncSession, session_id: UUID) -> list[_Node]:
    rows = (
        await db.execute(
            select(ChatMessage.id, ChatMessage.parent_id, ChatMessage.role, ChatMessage.created_at)
            .where(ChatMessage.session_id == session_id, ChatMessage.deleted_at.is_(None))
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        )
    ).all()
    return [_Node(id=r[0], parent_id=r[1], role=r[2], created_at=r[3]) for r in rows]


def _latest_turn(nodes: list[_Node]) -> _Node | None:
    for n in reversed(nodes):
        if n.role in TURN_ROLES:
            return n
    return None


def _active_path(nodes: list[_Node], leaf_id: UUID | None) -> set[UUID]:
    """Ids of the user/assistant messages on the branch ending at `leaf_id`, plus the tool
    messages attached to assistant messages on it."""
    by_id = {n.id: n for n in nodes}
    if leaf_id is None or leaf_id not in by_id:
        leaf = _latest_turn(nodes)
        leaf_id = leaf.id if leaf else None
    path: set[UUID] = set()
    cursor = leaf_id
    guard = 0
    while cursor is not None and cursor in by_id and guard < 100_000:
        path.add(cursor)
        cursor = by_id[cursor].parent_id
        guard += 1
    for n in nodes:
        if n.role == MessageRole.TOOL and n.parent_id in path:
            path.add(n.id)
    return path


def _siblings_of(nodes: list[_Node], node: _Node) -> list[_Node]:
    return [
        n for n in nodes if n.role == node.role and n.parent_id == node.parent_id and n.role in TURN_ROLES
    ]


def _sibling_info(nodes: list[_Node], message_id: UUID) -> dict[str, int] | None:
    by_id = {n.id: n for n in nodes}
    node = by_id.get(message_id)
    if node is None or node.role not in TURN_ROLES:
        return None
    sibs = _siblings_of(nodes, node)
    if len(sibs) <= 1:
        return None
    return {"index": next(i for i, s in enumerate(sibs) if s.id == message_id), "count": len(sibs)}


def _deepest_descendant(nodes: list[_Node], start_id: UUID) -> UUID:
    """Follow the newest child at every level — the natural leaf of a branch."""
    children: dict[UUID | None, list[_Node]] = {}
    for n in nodes:
        if n.role in TURN_ROLES:
            children.setdefault(n.parent_id, []).append(n)
    cursor = start_id
    guard = 0
    while guard < 100_000:
        kids = children.get(cursor)
        if not kids:
            return cursor
        cursor = kids[-1].id
        guard += 1
    return cursor


async def active_leaf_id(db: AsyncSession, session_obj: ChatSession) -> UUID | None:
    if session_obj.active_leaf_id is not None:
        return session_obj.active_leaf_id
    leaf = _latest_turn(await _load_tree(db, session_obj.id))
    return leaf.id if leaf else None


async def _set_active_leaf(db: AsyncSession, session_id: UUID, leaf_id: UUID | None) -> None:
    session_obj = await db.get(ChatSession, session_id)
    if session_obj is not None:
        session_obj.active_leaf_id = leaf_id
        await db.flush()


# --------------------------------------------------------------------------- persistence


async def _persist_user_message(
    db: AsyncSession, *, session_id: UUID, content: str, parent_id: UUID | None
) -> ChatMessage:
    msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.USER,
        content=content,
        tokens=count_tokens(content),
        parent_id=parent_id,
        created_at=_now(),
    )
    db.add(msg)
    await db.flush()
    await _set_active_leaf(db, session_id, msg.id)
    return msg


def _usage_payload(prompt: int, completion: int, embedding: int = 0) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "embedding_tokens": embedding,
        "total_tokens": prompt + completion + embedding,
    }


def _merge_usage(existing: dict[str, Any] | None, more: dict[str, int]) -> dict[str, int]:
    base = dict(existing or {})
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "embedding_tokens"):
        out[key] = int(base.get(key) or 0) + int(more.get(key) or 0)
    out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"] + out["embedding_tokens"]
    return out


async def _persist_tool_messages(
    db: AsyncSession, *, session_id: UUID, tool_runs: list[dict[str, Any]], existing_call_ids: set[str]
) -> list[ChatMessage]:
    """One `role=tool` message per executed tool call; re-parented to the reply once it exists."""
    out: list[ChatMessage] = []
    for run in tool_runs:
        call_id = str(run.get("call_id") or "")
        if call_id and call_id in existing_call_ids:
            continue
        output = str(run.get("output") or "")[:TOOL_MESSAGE_MAX_CHARS]
        msg = ChatMessage(
            session_id=session_id,
            role=MessageRole.TOOL,
            content=output,
            tokens=count_tokens(output),
            created_at=_now(),
            meta={
                "tool": run.get("tool"),
                "call_id": call_id or None,
                "input": run.get("input") or {},
                "status": run.get("status") or "ok",
                "latency_ms": int(run.get("latency_ms") or 0),
            },
        )
        db.add(msg)
        out.append(msg)
        existing_call_ids.add(call_id)
    if out:
        await db.flush()
    return out


async def _existing_tool_call_ids(db: AsyncSession, *, session_id: UUID, since: datetime | None) -> set[str]:
    stmt = select(ChatMessage.meta).where(
        ChatMessage.session_id == session_id, ChatMessage.role == MessageRole.TOOL
    )
    if since is not None:
        stmt = stmt.where(ChatMessage.created_at >= since)
    rows = (await db.execute(stmt)).scalars().all()
    return {str(m.get("call_id")) for m in rows if m and m.get("call_id")}


async def _log_tool_calls(
    db: AsyncSession, *, user_id: UUID, session_id: UUID, message_id: UUID, tool_runs: list[dict[str, Any]]
) -> None:
    for run in tool_runs:
        raw_input = run.get("input")
        status = run.get("status") or "ok"
        db.add(
            ToolCall(
                user_id=user_id,
                session_id=session_id,
                message_id=message_id,
                tool_name=str(run.get("tool") or "unknown")[:64],
                arguments=raw_input if isinstance(raw_input, dict) else {"raw": raw_input},
                result=str(run.get("output", ""))[:5000],
                status=status if status in ("ok", "error", "denied") else "ok",
                error=str(run.get("output"))[:2000] if status == "error" else None,
                latency_ms=int(run.get("latency_ms") or 0),
            )
        )
    if tool_runs:
        await db.flush()


async def _persist_assistant_message(
    db: AsyncSession,
    *,
    session_id: UUID,
    content: str,
    citations: list[dict[str, Any]],
    tool_runs: list[dict[str, Any]],
    model: str,
    provider: str,
    latency_ms: int,
    usage: dict[str, int],
    generation: dict[str, Any],
    status: str = "ok",
    interrupt_reason: str | None = None,
    approval: dict[str, Any] | None = None,
    placeholder: ChatMessage | None = None,
    parent_id: UUID | None = None,
    memories_used: list[str] | None = None,
) -> ChatMessage:
    meta: dict[str, Any] = {
        **((placeholder.meta if placeholder else None) or {}),
        "model": model,
        "provider": provider,
        "latency_ms": latency_ms,
        "citations": citations,
        "tool_calls": tool_runs,
        "usage": usage,
        "generation": generation,
        "status": status,
    }
    meta.pop("interrupt_reason", None)
    if interrupt_reason:
        meta["interrupt_reason"] = interrupt_reason
    if approval is not None:
        meta["approval"] = approval
    else:
        meta.pop("approval", None)
    meta["memories_used"] = list(memories_used or meta.get("memories_used") or [])
    if placeholder is not None:
        placeholder.content = content
        placeholder.tokens = usage.get("completion_tokens") or count_tokens(content)
        placeholder.meta = meta
        if status != STATUS_AWAITING_APPROVAL:
            placeholder.created_at = _now()
        await db.flush()
        return placeholder
    msg = ChatMessage(
        session_id=session_id,
        role=MessageRole.ASSISTANT,
        content=content,
        tokens=usage.get("completion_tokens") or count_tokens(content),
        meta=meta,
        parent_id=parent_id,
        created_at=_now(),
    )
    db.add(msg)
    await db.flush()
    return msg


def approval_read(row: Approval) -> dict[str, Any]:
    return {
        "approval_id": str(row.id),
        "session_id": str(row.session_id),
        "message_id": str(row.message_id) if row.message_id else None,
        "status": row.status.value if isinstance(row.status, ApprovalStatus) else str(row.status),
        "expires_at": row.expires_at.isoformat(),
        "tool_calls": row.tool_calls or [],
    }


async def _create_approval(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    message_id: UUID,
    thread_id: str,
    payload: dict[str, Any],
    generation: dict[str, Any],
) -> Approval:
    row = Approval(
        user_id=user_id,
        session_id=session_id,
        message_id=message_id,
        thread_id=thread_id,
        status=ApprovalStatus.PENDING,
        tool_calls=list(payload.get("tool_calls") or []),
        generation=generation,
        expires_at=_now() + timedelta(minutes=settings.APPROVAL_TTL_MINUTES),
    )
    db.add(row)
    await db.flush()
    return row


async def post_turn_maintenance(session_id: UUID) -> None:
    """Title generation + summary compaction, after the client already has its answer."""
    async with db_session() as db:
        session_obj = await db.get(ChatSession, session_id)
        if session_obj is None or session_obj.deleted_at is not None:
            return
        await autogenerate_title_if_needed(db, session=session_obj)
        await maybe_summarize(db, session_id=session_id)


async def _schedule_maintenance(session_id: UUID) -> None:
    await run_in_background(f"post_turn_maintenance:{session_id}", post_turn_maintenance(session_id))


async def _schedule_memory_extraction(
    scope: TurnScope, *, user_text: str, assistant_text: str, source_message_id: UUID | None, use_memory: bool
) -> None:
    if not use_memory or not assistant_text.strip():
        return
    await run_in_background(
        f"memory_extract:{source_message_id}",
        memory_service.extract_and_store(
            user_id=scope.user_id,
            org_id=scope.org_id,
            user_text=user_text,
            assistant_text=assistant_text,
            source_message_id=source_message_id,
        ),
    )


async def _finish_turn(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    text: str,
    citations: list[dict[str, Any]],
    tool_runs: list[dict[str, Any]],
    model: str,
    provider: str,
    latency_ms: int,
    prompt_tokens: int,
    completion_tokens: int,
    embedding_tokens: int,
    generation: dict[str, Any],
    operation: str,
    status: str = "ok",
    interrupt_reason: str | None = None,
    approval: dict[str, Any] | None = None,
    placeholder: ChatMessage | None = None,
    parent_id: UUID | None = None,
    org_id: UUID | None = None,
    memories_used: list[str] | None = None,
) -> tuple[ChatMessage, dict[str, int], list[ChatMessage]]:
    """Persist everything a turn produced: tool messages, the assistant message (new or the
    approval placeholder updated in place), the audit rows and the usage row; move the
    active branch to the reply."""
    usage = _usage_payload(prompt_tokens, completion_tokens, embedding_tokens)
    total_usage = _merge_usage((placeholder.meta or {}).get("usage") if placeholder else None, usage)

    tool_msgs: list[ChatMessage] = []
    if status != STATUS_AWAITING_APPROVAL:
        existing = await _existing_tool_call_ids(
            db, session_id=session_id, since=placeholder.created_at if placeholder else None
        )
        tool_msgs = await _persist_tool_messages(
            db, session_id=session_id, tool_runs=tool_runs, existing_call_ids=existing
        )

    assistant_msg = await _persist_assistant_message(
        db,
        session_id=session_id,
        content=text,
        citations=citations,
        tool_runs=tool_runs,
        model=model,
        provider=provider,
        latency_ms=latency_ms,
        usage=total_usage,
        generation=generation,
        status=status,
        interrupt_reason=interrupt_reason,
        approval=approval,
        placeholder=placeholder,
        parent_id=parent_id,
        memories_used=memories_used,
    )
    for m in tool_msgs:
        m.parent_id = assistant_msg.id
    if tool_msgs:
        await db.flush()
    if status != STATUS_AWAITING_APPROVAL:
        await _log_tool_calls(
            db, user_id=user_id, session_id=session_id, message_id=assistant_msg.id, tool_runs=tool_runs
        )
    usage_row = await log_ai_usage(
        db,
        user_id=user_id,
        session_id=session_id,
        operation=operation,
        model=model,
        provider=provider or "openai",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        org_id=org_id,
        meta={
            "message_id": str(assistant_msg.id),
            "embedding_tokens": embedding_tokens,
            "status": status,
            "tool_calls": len(tool_runs),
        },
    )
    await quota_service.note_usage(
        org_id, tokens=usage_row.total_tokens, cost_usd=float(usage_row.cost_usd or 0)
    )
    await _set_active_leaf(db, session_id, assistant_msg.id)
    return assistant_msg, total_usage, tool_msgs


# --------------------------------------------------------------------------- blocking


async def _stored_turn_response(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        user_msg = await db.get(ChatMessage, UUID(payload["user_message_id"]))
        assistant_msg = await db.get(ChatMessage, UUID(payload["assistant_message_id"]))
    except (KeyError, ValueError):
        return None
    if user_msg is None or assistant_msg is None:
        return None
    tool_rows = (
        (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.parent_id == assistant_msg.id, ChatMessage.role == MessageRole.TOOL
                )
            )
        )
        .scalars()
        .all()
    )
    meta = assistant_msg.meta or {}
    return {
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        "tool_messages": list(tool_rows),
        "citations": meta.get("citations", []),
        "tool_calls": meta.get("tool_calls", []),
        "usage": meta.get("usage", {}),
        "approval": meta.get("approval"),
        "replayed": True,
    }


async def send_message(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    content: str,
    use_rag: bool,
    use_tools: bool,
    tool_names: list[str] | None,
    temperature: float | None,
    max_tokens: int | None,
    model: str | None = None,
    collection_ids: list[UUID] | None = None,
    user_default_model: str | None = None,
    request_id: str | None = None,
    scope: TurnScope | None = None,
    explicit: set[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    scope = scope or TurnScope(user_id=user_id, user_default_model=user_default_model)
    if idempotency_key:
        state, stored = await idempotency.begin(scope.user_id, idempotency_key)
        if state == "done" and stored is not None:
            replay = await _stored_turn_response(db, stored)
            if replay is not None:
                return replay
        elif state == "in_progress":
            raise ConflictError("A request with this Idempotency-Key is still in progress")

    try:
        await _check_quota(db, scope)
        session_obj = await get_session(db, user_id=scope.user_id, session_id=session_id)
        turn = await resolve_turn_settings(
            db,
            scope=scope,
            session_obj=session_obj,
            use_rag=use_rag,
            use_tools=use_tools,
            tool_names=tool_names,
            model=model,
            collection_ids=collection_ids,
            temperature=temperature,
            max_tokens=max_tokens,
            explicit=explicit,
        )
        parent_id = await active_leaf_id(db, session_obj)
        user_msg = await _persist_user_message(
            db, session_id=session_id, content=content, parent_id=parent_id
        )
        req = _build_request(
            scope, session_obj, turn, user_input=content, streaming=False, request_id=request_id
        )
        result: GenerationResult = await generate(db, req)
        generation = turn.generation_meta(result.model)
        citation_payload = citations_payload(result.citations)
        status = STATUS_AWAITING_APPROVAL if result.approval else "ok"
        assistant_msg, usage, tool_msgs = await _finish_turn(
            db,
            user_id=scope.user_id,
            session_id=session_id,
            text=result.text,
            citations=citation_payload,
            tool_runs=result.tool_runs,
            model=result.model,
            provider=result.provider,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            embedding_tokens=result.embedding_tokens,
            generation=generation,
            operation="chat",
            status=status,
            approval=result.approval,
            parent_id=user_msg.id,
            org_id=scope.org_id,
            memories_used=req.progress.memories_used,
        )
        approval_payload: dict[str, Any] | None = None
        if result.approval:
            row = await _create_approval(
                db,
                user_id=scope.user_id,
                session_id=session_id,
                message_id=assistant_msg.id,
                thread_id=result.thread_id,
                payload=result.approval,
                generation=generation,
            )
            approval_payload = approval_read(row)
            assistant_msg.meta = {**assistant_msg.meta, "approval": approval_payload}
            await db.flush()
        await db.commit()
    except Exception:
        if idempotency_key:
            await idempotency.release(scope.user_id, idempotency_key)
        raise

    if approval_payload is None:
        await _schedule_maintenance(session_id)
        await _schedule_memory_extraction(
            scope,
            user_text=content,
            assistant_text=result.text,
            source_message_id=assistant_msg.id,
            use_memory=turn.use_memory,
        )
    if idempotency_key:
        await idempotency.complete(
            scope.user_id,
            idempotency_key,
            {"user_message_id": str(user_msg.id), "assistant_message_id": str(assistant_msg.id)},
        )
    return {
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        "tool_messages": tool_msgs,
        "citations": citation_payload,
        "tool_calls": result.tool_runs,
        "usage": usage,
        "approval": approval_payload,
    }


# --------------------------------------------------------------------------- streaming core


async def _persist_interrupted_turn(
    *,
    user_id: UUID,
    session_id: UUID,
    progress: GenerationProgress,
    generation: dict[str, Any],
    placeholder_id: UUID | None = None,
    parent_id: UUID | None = None,
    org_id: UUID | None = None,
) -> None:
    """Persist whatever a cancelled generation produced, on a fresh DB session."""
    async with db_session() as db:
        placeholder = await db.get(ChatMessage, placeholder_id) if placeholder_id else None
        await _finish_turn(
            db,
            user_id=user_id,
            session_id=session_id,
            text=progress.text,
            citations=progress.citations,
            tool_runs=progress.tool_runs or progress.tool_calls,
            model=progress.model,
            provider=progress.provider,
            latency_ms=progress.latency_ms,
            prompt_tokens=progress.prompt_tokens or count_tokens(progress.text),
            completion_tokens=progress.completion_tokens or count_tokens(progress.text),
            embedding_tokens=progress.embedding_tokens,
            generation=generation,
            operation="chat_stream",
            status="interrupted",
            interrupt_reason=progress.interrupt_reason or "disconnect",
            placeholder=placeholder,
            parent_id=parent_id,
            org_id=org_id,
        )
    logger.info(
        "generation_interrupted_persisted", session_id=str(session_id), reason=progress.interrupt_reason
    )


def _assistant_event(
    msg: ChatMessage, p: GenerationProgress, usage: dict[str, int], status: str
) -> dict[str, Any]:
    return {
        "type": "assistant_message",
        "data": {
            "id": str(msg.id),
            "session_id": str(msg.session_id),
            "parent_id": str(msg.parent_id) if msg.parent_id else None,
            "role": "assistant",
            "content": msg.content,
            "created_at": msg.created_at.isoformat(),
            "citations": (msg.meta or {}).get("citations", []),
            "tool_calls": (msg.meta or {}).get("tool_calls", []),
            "usage": usage,
            "model": p.model,
            "interrupted": p.interrupted,
            "status": status,
            "approval": (msg.meta or {}).get("approval"),
            "memories_used": (msg.meta or {}).get("memories_used", []),
        },
    }


async def _run_streaming_turn(
    db: AsyncSession,
    *,
    req: GenerationRequest,
    events: AsyncIterator[dict[str, Any]],
    generation: dict[str, Any],
    operation: str,
    placeholder: ChatMessage | None,
    parent_id: UUID | None,
    scope: TurnScope,
    user_text: str,
    use_memory: bool,
) -> AsyncGenerator[dict[str, Any], None]:
    """Republish orchestrator events, turning `approval_required` into a persisted
    approval and finishing the turn (new message or placeholder updated in place)."""
    p = req.progress
    user_id, session_id = scope.user_id, req.session.id

    try:
        async for evt in events:
            if not generation.get("model") and p.model:
                generation["model"] = p.model
            if evt["type"] == "approval_required":
                placeholder, _usage, _ = await _finish_turn(
                    db,
                    user_id=user_id,
                    session_id=session_id,
                    text=p.text,
                    citations=p.citations,
                    tool_runs=p.tool_runs,
                    model=p.model,
                    provider=p.provider,
                    latency_ms=p.latency_ms,
                    prompt_tokens=p.prompt_tokens,
                    completion_tokens=p.completion_tokens,
                    embedding_tokens=p.embedding_tokens,
                    generation=generation,
                    operation=operation,
                    status=STATUS_AWAITING_APPROVAL,
                    approval=evt["data"],
                    placeholder=placeholder,
                    parent_id=parent_id,
                    org_id=scope.org_id,
                    memories_used=p.memories_used,
                )
                row = await _create_approval(
                    db,
                    user_id=user_id,
                    session_id=session_id,
                    message_id=placeholder.id,
                    thread_id=req.thread_id,
                    payload=evt["data"],
                    generation=generation,
                )
                placeholder.meta = {**placeholder.meta, "approval": approval_read(row)}
                await db.commit()
                evt = {"type": "approval_required", "data": approval_read(row)}
            yield evt
    except (asyncio.CancelledError, GeneratorExit) as exc:
        task = None
        if not p.finished and (p.text or p.tool_calls or p.tool_runs):
            task = spawn_detached(
                f"persist_interrupted:{session_id}",
                _persist_interrupted_turn(
                    user_id=user_id,
                    session_id=session_id,
                    progress=p,
                    generation=generation,
                    placeholder_id=placeholder.id if placeholder else None,
                    parent_id=parent_id,
                    org_id=scope.org_id,
                ),
            )
        if isinstance(exc, GeneratorExit) and task is not None:
            await task
        raise

    if p.approval and placeholder is not None:
        await db.refresh(placeholder)
        await db.commit()
        yield _assistant_event(
            placeholder, p, (placeholder.meta or {}).get("usage", {}), STATUS_AWAITING_APPROVAL
        )
        return

    status = p.status
    assistant_msg, usage, _tool_msgs = await _finish_turn(
        db,
        user_id=user_id,
        session_id=session_id,
        text=p.text,
        citations=p.citations
        if not placeholder
        else ((placeholder.meta or {}).get("citations") or p.citations),
        tool_runs=p.tool_runs or p.tool_calls,
        model=p.model,
        provider=p.provider,
        latency_ms=p.latency_ms,
        prompt_tokens=p.prompt_tokens,
        completion_tokens=p.completion_tokens,
        embedding_tokens=p.embedding_tokens,
        generation=generation,
        operation=operation,
        status=status,
        interrupt_reason=p.interrupt_reason,
        placeholder=placeholder,
        parent_id=parent_id,
        org_id=scope.org_id,
        memories_used=p.memories_used,
    )
    await db.commit()
    await _schedule_maintenance(session_id)
    if status == "ok":
        await _schedule_memory_extraction(
            scope,
            user_text=user_text,
            assistant_text=p.text,
            source_message_id=assistant_msg.id,
            use_memory=use_memory,
        )
    yield _assistant_event(assistant_msg, p, usage, status)


# --------------------------------------------------------------------------- generation logs


_STOP_EVENTS: dict[str, asyncio.Event] = {}


def _stop_key(generation_id: str) -> str:
    return f"gen:{generation_id}:stop"


async def request_stop(generation_id: str) -> bool:
    """Ask a running generation (in any worker) to stop; the partial reply is persisted."""
    evt = _STOP_EVENTS.get(generation_id)
    if evt is not None:
        evt.set()
    await get_redis().set(_stop_key(generation_id), "1", ex=settings.STREAM_EVENTS_TTL_SECONDS)
    return evt is not None


async def _watch_stop_flag(generation_id: str, stop_event: asyncio.Event) -> None:
    r = get_redis()
    try:
        while not stop_event.is_set():
            if await r.get(_stop_key(generation_id)):
                stop_event.set()
                return
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        return


@dataclass
class TurnHandle:
    generation_id: str
    user_message: ChatMessage | None
    replayed: bool = False


def _user_message_event(user_msg: ChatMessage, generation_id: str) -> dict[str, Any]:
    return {
        "type": "user_message",
        "data": {
            "id": str(user_msg.id),
            "session_id": str(user_msg.session_id),
            "parent_id": str(user_msg.parent_id) if user_msg.parent_id else None,
            "content": user_msg.content,
            "role": "user",
            "created_at": user_msg.created_at.isoformat(),
            "generation_id": generation_id,
        },
    }


@dataclass
class _GenerationJob:
    scope: TurnScope
    session_id: UUID
    user_message_id: UUID | None
    user_text: str
    turn: TurnSettings
    generation_id: str
    request_id: str | None
    stop_event: asyncio.Event
    operation: str = "chat_stream"
    parent_id: UUID | None = None
    # Approval continuation
    placeholder_id: UUID | None = None
    resume_decision: dict[str, Any] | None = None
    thread_id: str | None = None
    generation_meta: dict[str, Any] = field(default_factory=dict)


_TAGGED_EVENT_TYPES = {"done", "assistant_message", "approval_required", "error", "user_message"}


def _tag_generation(evt: dict[str, Any], generation_id: str) -> dict[str, Any]:
    """Stamp the generation id on the event (and inside the payload of terminal events, so
    clients keying on `data` alone can correlate)."""
    out = {**evt, "generation_id": generation_id}
    if evt.get("type") in _TAGGED_EVENT_TYPES and isinstance(evt.get("data"), dict):
        out["data"] = {**evt["data"], "generation_id": generation_id}
    return out


async def _run_generation_job(job: _GenerationJob) -> None:
    """Produce a turn on its own DB session, writing every event to the generation log.

    Runs detached from the HTTP/WS request: a client that disconnects can re-attach and
    a background turn completes even if nobody is watching. `stop_event` (set by a stop
    frame/endpoint, possibly from another worker via Redis) interrupts it cooperatively.
    """
    log = GenerationLog(job.generation_id)
    _STOP_EVENTS[job.generation_id] = job.stop_event
    watcher = asyncio.create_task(_watch_stop_flag(job.generation_id, job.stop_event))
    status = "done"
    try:
        async with db_session() as db:
            session_obj = await get_session(db, user_id=job.scope.user_id, session_id=job.session_id)
            placeholder = await db.get(ChatMessage, job.placeholder_id) if job.placeholder_id else None
            req = _build_request(
                job.scope,
                session_obj,
                job.turn,
                user_input=job.user_text,
                streaming=True,
                request_id=job.request_id,
                stop_event=job.stop_event,
                thread_id=job.thread_id or job.generation_id,
            )
            generation = dict(job.generation_meta) or job.turn.generation_meta(None)
            if job.resume_decision is not None:
                events = resume_stream(db, req, job.resume_decision)
            else:
                events = stream(db, req)
            turn = _run_streaming_turn(
                db,
                req=req,
                events=events,
                generation=generation,
                operation=job.operation,
                placeholder=placeholder,
                parent_id=job.parent_id,
                scope=job.scope,
                user_text=job.user_text,
                use_memory=job.turn.use_memory,
            )
            try:
                async for evt in turn:
                    await log.append(_tag_generation(evt, job.generation_id))
            finally:
                await turn.aclose()
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001
        status = "failed"
        logger.exception("generation_job_failed", generation_id=job.generation_id)
        code = getattr(e, "code", "internal_error")
        message = getattr(e, "message", str(e))[:300]
        with contextlib.suppress(Exception):
            await log.append(
                _tag_generation(
                    {"type": "error", "data": {"code": code, "message": message}}, job.generation_id
                )
            )
            await log.append(
                {
                    "type": "assistant_message",
                    "data": {
                        "id": None,
                        "session_id": str(job.session_id),
                        "role": "assistant",
                        "content": "",
                        "status": "error",
                        "interrupted": False,
                        "generation_id": job.generation_id,
                    },
                    "generation_id": job.generation_id,
                }
            )
    finally:
        watcher.cancel()
        _STOP_EVENTS.pop(job.generation_id, None)
        with contextlib.suppress(Exception):
            await log.finish(status)


async def follow_generation(generation_id: str, *, after: int = 0) -> AsyncIterator[dict[str, Any]]:
    """Replay a generation's events after `after` and follow it live until it ends."""
    log = GenerationLog(generation_id)
    if await log.info() is None:
        raise NotFoundError("Generation not found or expired")
    async for seq, evt in log.follow(after):
        yield {**evt, "seq": seq}


async def start_turn(
    db: AsyncSession,
    *,
    scope: TurnScope,
    session_id: UUID,
    content: str,
    use_rag: bool,
    use_tools: bool,
    tool_names: list[str] | None,
    temperature: float | None,
    max_tokens: int | None,
    model: str | None = None,
    collection_ids: list[UUID] | None = None,
    explicit: set[str] | None = None,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    stop_event: asyncio.Event | None = None,
    edit_of: UUID | None = None,
) -> TurnHandle:
    """Validate, persist the user message and launch the generation in the background.

    `edit_of` creates the new user message as a sibling of that message (a branch).
    """
    if idempotency_key:
        state, stored = await idempotency.begin(scope.user_id, idempotency_key)
        if state in ("done", "in_progress") and stored and stored.get("generation_id"):
            gid = str(stored["generation_id"])
            user_msg = (
                await db.get(ChatMessage, UUID(stored["user_message_id"]))
                if stored.get("user_message_id")
                else None
            )
            return TurnHandle(generation_id=gid, user_message=user_msg, replayed=True)
    try:
        await _check_quota(db, scope)
        session_obj = await get_session(db, user_id=scope.user_id, session_id=session_id)
        turn = await resolve_turn_settings(
            db,
            scope=scope,
            session_obj=session_obj,
            use_rag=use_rag,
            use_tools=use_tools,
            tool_names=tool_names,
            model=model,
            collection_ids=collection_ids,
            temperature=temperature,
            max_tokens=max_tokens,
            explicit=explicit,
        )
        if edit_of is not None:
            edited = await db.get(ChatMessage, edit_of)
            if edited is None or edited.session_id != session_id or edited.deleted_at is not None:
                raise NotFoundError("Message not found")
            if edited.role != MessageRole.USER:
                raise ValidationError("Only user messages can be edited")
            parent_id = edited.parent_id
        else:
            parent_id = await active_leaf_id(db, session_obj)
        user_msg = await _persist_user_message(
            db, session_id=session_id, content=content, parent_id=parent_id
        )
        await db.commit()
    except Exception:
        if idempotency_key:
            await idempotency.release(scope.user_id, idempotency_key)
        raise

    generation_id = uuid4().hex
    log = GenerationLog(generation_id)
    await log.start(session_id=session_id, user_id=scope.user_id, user_message_id=user_msg.id)
    await log.append({**_user_message_event(user_msg, generation_id), "generation_id": generation_id})
    if idempotency_key:
        await idempotency.complete(
            scope.user_id,
            idempotency_key,
            {"generation_id": generation_id, "user_message_id": str(user_msg.id)},
        )
    job = _GenerationJob(
        scope=scope,
        session_id=session_id,
        user_message_id=user_msg.id,
        user_text=content,
        turn=turn,
        generation_id=generation_id,
        request_id=request_id,
        stop_event=stop_event or asyncio.Event(),
        parent_id=user_msg.id,
    )
    spawn_detached(f"generation:{generation_id}", _run_generation_job(job))
    return TurnHandle(generation_id=generation_id, user_message=user_msg)


async def stream_message(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    content: str,
    use_rag: bool,
    use_tools: bool,
    tool_names: list[str] | None,
    temperature: float | None,
    max_tokens: int | None,
    model: str | None = None,
    collection_ids: list[UUID] | None = None,
    user_default_model: str | None = None,
    request_id: str | None = None,
    stop_event: asyncio.Event | None = None,
    scope: TurnScope | None = None,
    explicit: set[str] | None = None,
    idempotency_key: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Start a turn and stream its events (`user_message`, `token`…, `done`, `assistant_message`).

    The generation itself runs detached and is logged, so a dropped connection can resume
    with `follow_generation`; stopping is explicit (stop frame / endpoint), not implied by
    disconnecting.
    """
    scope = scope or TurnScope(user_id=user_id, user_default_model=user_default_model)
    handle = await start_turn(
        db,
        scope=scope,
        session_id=session_id,
        content=content,
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
        collection_ids=collection_ids,
        explicit=explicit,
        request_id=request_id,
        idempotency_key=idempotency_key,
        stop_event=stop_event,
    )
    async for evt in follow_generation(handle.generation_id):
        yield evt


async def edit_message(
    db: AsyncSession,
    *,
    scope: TurnScope,
    session_id: UUID,
    message_id: UUID,
    content: str,
    use_rag: bool,
    use_tools: bool,
    tool_names: list[str] | None,
    temperature: float | None,
    max_tokens: int | None,
    model: str | None = None,
    collection_ids: list[UUID] | None = None,
    explicit: set[str] | None = None,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Edit a user message: a sibling user message starts a new branch and gets a reply."""
    handle = await start_turn(
        db,
        scope=scope,
        session_id=session_id,
        content=content,
        use_rag=use_rag,
        use_tools=use_tools,
        tool_names=tool_names,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
        collection_ids=collection_ids,
        explicit=explicit,
        request_id=request_id,
        idempotency_key=idempotency_key,
        stop_event=stop_event,
        edit_of=message_id,
    )
    async for evt in follow_generation(handle.generation_id):
        yield evt


# --------------------------------------------------------------------------- approvals


async def _expire_if_needed(row: Approval) -> None:
    if row.status == ApprovalStatus.PENDING and row.expires_at <= _now():
        row.status = ApprovalStatus.EXPIRED
        APPROVALS.labels("expired").inc()


async def list_approvals(
    db: AsyncSession, *, user_id: UUID, session_id: UUID, status: str | None = None
) -> list[Approval]:
    await get_session(db, user_id=user_id, session_id=session_id)
    stmt = select(Approval).where(Approval.user_id == user_id, Approval.session_id == session_id)
    if status:
        stmt = stmt.where(Approval.status == ApprovalStatus(status))
    rows = (await db.execute(stmt.order_by(Approval.created_at.desc()))).scalars().all()
    changed = False
    for row in rows:
        before = row.status
        await _expire_if_needed(row)
        changed = changed or before != row.status
    if changed:
        await db.commit()
    if status:
        rows = [r for r in rows if r.status == ApprovalStatus(status)]
    return list(rows)


async def get_pending_approval(
    db: AsyncSession, *, user_id: UUID, session_id: UUID, approval_id: UUID
) -> Approval:
    """404 for unknown/foreign approvals, 409 when already resolved or expired."""
    row = await db.get(Approval, approval_id)
    if row is None or row.user_id != user_id or row.session_id != session_id:
        raise NotFoundError("Approval not found")
    await _expire_if_needed(row)
    if row.status != ApprovalStatus.PENDING:
        await db.commit()
        raise ConflictError(f"Approval already {row.status.value}", details={"status": row.status.value})
    return row


async def resolve_approval(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    approval_id: UUID,
    decision: str,
    tool_call_ids: list[str] | None = None,
    request_id: str | None = None,
    stop_event: asyncio.Event | None = None,
    scope: TurnScope | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Apply the user's decision and stream the rest of the agent turn (logged, resumable)."""
    scope = scope or TurnScope(user_id=user_id)
    await _check_quota(db, scope)
    await get_session(db, user_id=scope.user_id, session_id=session_id)
    row = await get_pending_approval(
        db, user_id=scope.user_id, session_id=session_id, approval_id=approval_id
    )
    placeholder = await db.get(ChatMessage, row.message_id) if row.message_id else None
    gen = dict(row.generation or {})
    turn = TurnSettings.from_generation_meta(gen, use_memory=False)
    turn.use_tools = True
    row.status = ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.DENIED
    row.decided_at = _now()
    row.decision = {"decision": decision, "tool_call_ids": tool_call_ids}
    await db.commit()
    APPROVALS.labels(row.status.value).inc()

    generation_id = uuid4().hex
    log = GenerationLog(generation_id)
    await log.start(session_id=session_id, user_id=scope.user_id, user_message_id=None)
    user_text = ""
    if placeholder is not None and placeholder.parent_id is not None:
        parent = await db.get(ChatMessage, placeholder.parent_id)
        user_text = parent.content if parent else ""
    job = _GenerationJob(
        scope=scope,
        session_id=session_id,
        user_message_id=placeholder.parent_id if placeholder else None,
        user_text=user_text,
        turn=turn,
        generation_id=generation_id,
        request_id=request_id,
        stop_event=stop_event or asyncio.Event(),
        parent_id=placeholder.parent_id if placeholder else None,
        placeholder_id=placeholder.id if placeholder else None,
        resume_decision={"decision": decision, "tool_call_ids": tool_call_ids},
        thread_id=row.thread_id,
        generation_meta=gen,
    )
    spawn_detached(f"generation:{generation_id}", _run_generation_job(job))
    async for evt in follow_generation(generation_id):
        yield evt


# --------------------------------------------------------------------------- history / tree


async def list_history(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    cursor: datetime | None,
    limit: int,
) -> tuple[list[ChatMessage], datetime | None]:
    """The active branch, oldest first, with sibling counts for branched messages."""
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)
    nodes = await _load_tree(db, session_id)
    path = _active_path(nodes, session_obj.active_leaf_id)
    if not path:
        return [], None
    stmt = select(ChatMessage).where(ChatMessage.id.in_(path), ChatMessage.deleted_at.is_(None))
    if cursor is not None:
        stmt = stmt.where(ChatMessage.created_at < cursor)
    stmt = stmt.order_by(desc(ChatMessage.created_at)).limit(limit + 1)
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].created_at if has_more and rows else None
    ordered = list(reversed(rows))
    for m in ordered:
        m.siblings = _sibling_info(nodes, m.id)  # type: ignore[attr-defined]
    return ordered, next_cursor


async def siblings(
    db: AsyncSession, *, user_id: UUID, session_id: UUID, message_id: UUID
) -> tuple[list[ChatMessage], int]:
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)
    nodes = await _load_tree(db, session_id)
    by_id = {n.id: n for n in nodes}
    node = by_id.get(message_id)
    if node is None:
        raise NotFoundError("Message not found")
    sibs = _siblings_of(nodes, node) if node.role in TURN_ROLES else [node]
    ids = [s.id for s in sibs]
    rows = (await db.execute(select(ChatMessage).where(ChatMessage.id.in_(ids)))).scalars().all()
    ordered = sorted(rows, key=lambda m: ids.index(m.id))
    path = _active_path(nodes, session_obj.active_leaf_id)
    active_index = next((i for i, m in enumerate(ordered) if m.id in path), 0)
    for m in ordered:
        m.siblings = _sibling_info(nodes, m.id)  # type: ignore[attr-defined]
    return ordered, active_index


async def switch_branch(db: AsyncSession, *, user_id: UUID, session_id: UUID, leaf_message_id: UUID) -> None:
    """Make the branch containing `leaf_message_id` active (its newest descendant becomes the leaf)."""
    session_obj = await get_session(db, user_id=user_id, session_id=session_id)
    nodes = await _load_tree(db, session_id)
    if leaf_message_id not in {n.id for n in nodes}:
        raise NotFoundError("Message not found")
    session_obj.active_leaf_id = _deepest_descendant(nodes, leaf_message_id)
    await db.flush()


async def _last_turn_pair(db: AsyncSession, session_obj: ChatSession) -> tuple[ChatMessage, ChatMessage]:
    """(last user message, last assistant message) on the active branch."""
    nodes = await _load_tree(db, session_obj.id)
    path = _active_path(nodes, session_obj.active_leaf_id)
    by_id = {n.id: n for n in nodes}
    latest = _latest_turn(nodes)
    leaf_id = (
        session_obj.active_leaf_id if session_obj.active_leaf_id in by_id else (latest.id if latest else None)
    )
    assistant_node = None
    cursor = leaf_id
    while cursor is not None and cursor in by_id:
        if by_id[cursor].role == MessageRole.ASSISTANT:
            assistant_node = by_id[cursor]
            break
        cursor = by_id[cursor].parent_id
    if assistant_node is None:
        raise NotFoundError("Nothing to regenerate")
    assistant_msg = await db.get(ChatMessage, assistant_node.id)
    if assistant_msg is None:
        raise NotFoundError("Nothing to regenerate")
    user_msg = await db.get(ChatMessage, assistant_node.parent_id) if assistant_node.parent_id else None
    if user_msg is None or user_msg.role != MessageRole.USER:
        # Legacy data without parent links: fall back to time ordering.
        user_msg = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == session_obj.id,
                    ChatMessage.role == MessageRole.USER,
                    ChatMessage.deleted_at.is_(None),
                    ChatMessage.created_at <= assistant_msg.created_at,
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if user_msg is None:
        raise NotFoundError("No user turn found")
    assert path  # the branch exists
    return user_msg, assistant_msg


async def regenerate_last(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID,
    temperature: float | None,
    max_tokens: int | None,
    user_default_model: str | None = None,
    request_id: str | None = None,
    scope: TurnScope | None = None,
) -> dict[str, Any]:
    """Blocking regeneration: a new sibling reply to the same user message (the previous
    reply is kept as a branch)."""
    scope = scope or TurnScope(user_id=user_id, user_default_model=user_default_model)
    await _check_quota(db, scope)
    session_obj = await get_session(db, user_id=scope.user_id, session_id=session_id)
    last_user, last_assistant = await _last_turn_pair(db, session_obj)
    previous = (last_assistant.meta or {}).get("generation") or {}
    turn = TurnSettings.from_generation_meta(previous, use_memory=False)
    turn.temperature = temperature if temperature is not None else turn.temperature
    turn.max_tokens = max_tokens if max_tokens is not None else turn.max_tokens
    _validate_tools(turn.use_tools, turn.tool_names)
    assistant = await _assistant_for(db, session_obj)
    if assistant is not None:
        turn.system_prompt = assistant.system_prompt
        turn.assistant_model = assistant.model if get_registry().is_available(assistant.model) else None

    req = _build_request(
        scope, session_obj, turn, user_input=last_user.content, streaming=False, request_id=request_id
    )
    result = await generate(db, req)
    generation = turn.generation_meta(result.model)
    citation_payload = citations_payload(result.citations)
    status = STATUS_AWAITING_APPROVAL if result.approval else "ok"
    new_msg, usage, tool_msgs = await _finish_turn(
        db,
        user_id=scope.user_id,
        session_id=session_id,
        text=result.text,
        citations=citation_payload,
        tool_runs=result.tool_runs,
        model=result.model,
        provider=result.provider,
        latency_ms=result.latency_ms,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        embedding_tokens=result.embedding_tokens,
        generation=generation,
        operation="chat_regenerate",
        status=status,
        approval=result.approval,
        parent_id=last_user.id,
        org_id=scope.org_id,
    )
    if result.approval:
        row = await _create_approval(
            db,
            user_id=scope.user_id,
            session_id=session_id,
            message_id=new_msg.id,
            thread_id=result.thread_id,
            payload=result.approval,
            generation=generation,
        )
        new_msg.meta = {**new_msg.meta, "approval": approval_read(row)}
        await db.flush()
    await db.commit()
    if not result.approval:
        await _schedule_maintenance(session_id)
    return {
        "assistant_message": new_msg,
        "tool_messages": tool_msgs,
        "citations": citation_payload,
        "tool_calls": result.tool_runs,
        "usage": usage,
    }


async def regenerate_stream(
    db: AsyncSession,
    *,
    scope: TurnScope,
    session_id: UUID,
    temperature: float | None,
    max_tokens: int | None,
    request_id: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Streaming regeneration (logged, resumable); the previous reply becomes a sibling."""
    await _check_quota(db, scope)
    session_obj = await get_session(db, user_id=scope.user_id, session_id=session_id)
    last_user, last_assistant = await _last_turn_pair(db, session_obj)
    previous = (last_assistant.meta or {}).get("generation") or {}
    turn = TurnSettings.from_generation_meta(previous, use_memory=False)
    turn.temperature = temperature if temperature is not None else turn.temperature
    turn.max_tokens = max_tokens if max_tokens is not None else turn.max_tokens
    _validate_tools(turn.use_tools, turn.tool_names)
    assistant = await _assistant_for(db, session_obj)
    if assistant is not None:
        turn.system_prompt = assistant.system_prompt
        turn.assistant_model = assistant.model if get_registry().is_available(assistant.model) else None
    # Generate against the branch ending at the user message (not the old reply).
    session_obj.active_leaf_id = last_user.id
    await db.commit()

    generation_id = uuid4().hex
    log = GenerationLog(generation_id)
    await log.start(session_id=session_id, user_id=scope.user_id, user_message_id=last_user.id)
    await log.append({**_user_message_event(last_user, generation_id), "generation_id": generation_id})
    job = _GenerationJob(
        scope=scope,
        session_id=session_id,
        user_message_id=last_user.id,
        user_text=last_user.content,
        turn=turn,
        generation_id=generation_id,
        request_id=request_id,
        stop_event=stop_event or asyncio.Event(),
        operation="chat_regenerate",
        parent_id=last_user.id,
    )
    spawn_detached(f"generation:{generation_id}", _run_generation_job(job))
    async for evt in follow_generation(generation_id):
        yield evt


async def active_generation(session_id: UUID) -> dict[str, Any] | None:
    log = await GenerationLog.active_for_session(session_id)
    if log is None:
        return None
    info = await log.info()
    if info is None:
        return None
    return {
        "generation_id": log.id,
        "started_at": datetime.fromtimestamp(float(info.get("started_at") or 0), tz=UTC),
        "status": "running" if info.get("status") == "running" else "done",
    }


async def generation_info(session_id: UUID, generation_id: str) -> dict[str, str]:
    info = await GenerationLog(generation_id).info()
    if info is None or info.get("session_id") != str(session_id):
        raise NotFoundError("Generation not found or expired")
    return info


# --------------------------------------------------------------------------- search


def _snippet(text: str) -> str:
    return (text[:240] + "…") if len(text) > 240 else text


async def search(
    db: AsyncSession, *, user_id: UUID, query: str, limit: int, org_id: UUID | None = None
) -> list[SearchResult]:
    """Full-text search over the user's messages (stemmed, ranked, highlighted), falling
    back to substring matching for queries the text parser cannot use."""
    tsquery = func.plainto_tsquery("english", query)
    rank = func.ts_rank_cd(ChatMessage.content_tsv, tsquery).label("rank")
    headline = func.ts_headline(
        "english",
        ChatMessage.content,
        tsquery,
        "StartSel=<mark>, StopSel=</mark>, MaxWords=35, MinWords=15, MaxFragments=2",
    ).label("highlight")
    base_cols = (
        ChatSession.id.label("session_id"),
        ChatSession.title.label("session_title"),
        ChatMessage.id.label("message_id"),
        ChatMessage.role.label("role"),
        ChatMessage.content.label("snippet"),
        ChatMessage.created_at.label("created_at"),
    )
    filters = [
        ChatSession.user_id == user_id,
        ChatSession.deleted_at.is_(None),
        ChatMessage.deleted_at.is_(None),
    ]
    if org_id is not None:
        filters.append(ChatSession.org_id == org_id)
    stmt = (
        select(*base_cols, rank, headline)
        .join(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .where(*filters, ChatMessage.content_tsv.op("@@")(tsquery))
        .order_by(desc(rank), desc(ChatMessage.created_at))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    results = [
        SearchResult(
            session_id=r.session_id,
            session_title=r.session_title,
            message_id=r.message_id,
            role=r.role,
            snippet=_snippet(r.snippet),
            highlight=r.highlight,
            rank=float(r.rank or 0.0),
            created_at=r.created_at,
        )
        for r in rows
    ]
    if results:
        return results

    pattern = f"%{query}%"
    stmt = (
        select(*base_cols)
        .join(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .where(*filters, or_(ChatMessage.content.ilike(pattern), ChatSession.title.ilike(pattern)))
        .order_by(desc(ChatMessage.created_at))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        SearchResult(
            session_id=r.session_id,
            session_title=r.session_title,
            message_id=r.message_id,
            role=r.role,
            snippet=_snippet(r.snippet),
            created_at=r.created_at,
        )
        for r in rows
    ]
