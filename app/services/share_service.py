"""Read-only share links and transcript export (Markdown / JSON)."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import NotFoundError
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.share import SessionShare
from app.services import chat_service
from app.services.session_service import get_session


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def share_url(token: str) -> str:
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}{settings.API_V1_PREFIX}/shared/{token}"


async def create_share(
    db: AsyncSession, *, user_id: UUID, session_id: UUID, expires_in_hours: int | None = None
) -> tuple[SessionShare, str]:
    await get_session(db, user_id=user_id, session_id=session_id)
    token = secrets.token_urlsafe(32)
    hours = expires_in_hours or settings.SHARE_LINK_DEFAULT_HOURS
    row = SessionShare(
        session_id=session_id,
        user_id=user_id,
        token_hash=_hash(token),
        expires_at=datetime.now(UTC) + timedelta(hours=hours),
    )
    db.add(row)
    await db.flush()
    return row, token


async def list_shares(db: AsyncSession, *, user_id: UUID, session_id: UUID) -> list[SessionShare]:
    await get_session(db, user_id=user_id, session_id=session_id)
    now = datetime.now(UTC)
    stmt = (
        select(SessionShare)
        .where(
            SessionShare.session_id == session_id,
            SessionShare.user_id == user_id,
            SessionShare.revoked_at.is_(None),
            SessionShare.expires_at > now,
        )
        .order_by(SessionShare.created_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def revoke_shares(
    db: AsyncSession, *, user_id: UUID, session_id: UUID, share_id: UUID | None = None
) -> int:
    rows = await list_shares(db, user_id=user_id, session_id=session_id)
    count = 0
    for row in rows:
        if share_id is None or row.id == share_id:
            row.revoked_at = datetime.now(UTC)
            count += 1
    await db.flush()
    return count


async def resolve_share(db: AsyncSession, token: str) -> tuple[SessionShare, ChatSession]:
    stmt = select(SessionShare).where(SessionShare.token_hash == _hash(token))
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at <= datetime.now(UTC):
        raise NotFoundError("Share link not found or expired")
    session_obj = await db.get(ChatSession, row.session_id)
    if session_obj is None or session_obj.deleted_at is not None:
        raise NotFoundError("Share link not found or expired")
    row.views += 1
    await db.flush()
    return row, session_obj


# --------------------------------------------------------------------------- transcript


async def transcript(db: AsyncSession, *, user_id: UUID, session_id: UUID) -> list[ChatMessage]:
    """The active branch, oldest first, without pagination."""
    items, _ = await chat_service.list_history(
        db, user_id=user_id, session_id=session_id, cursor=None, limit=200
    )
    out = list(items)
    while len(items) == 200:
        items, _ = await chat_service.list_history(
            db, user_id=user_id, session_id=session_id, cursor=out[0].created_at, limit=200
        )
        out = list(items) + out
    return out


def _public_message(m: ChatMessage) -> dict[str, Any]:
    meta = m.meta or {}
    return {
        "id": str(m.id),
        "role": m.role.value,
        "content": m.content,
        "created_at": m.created_at.isoformat(),
        "citations": [
            {k: c.get(k) for k in ("document_name", "page", "section", "excerpt")}
            for c in meta.get("citations", [])
        ],
        "attachments": [
            {k: a.get(k) for k in ("name", "kind", "mime_type", "size_bytes")}
            for a in meta.get("attachments", [])
        ],
        "artifacts": [
            {k: a.get(k) for k in ("name", "mime_type", "size_bytes")} for a in meta.get("artifacts", [])
        ],
        "tool": meta.get("tool") if m.role == MessageRole.TOOL else None,
        "model": meta.get("model") if m.role == MessageRole.ASSISTANT else None,
    }


def to_json(
    session_obj: ChatSession, messages: list[ChatMessage], *, include_tools: bool = True
) -> dict[str, Any]:
    return {
        "session": {
            "id": str(session_obj.id),
            "title": session_obj.title,
            "created_at": session_obj.created_at.isoformat(),
            "model": session_obj.model,
        },
        "messages": [_public_message(m) for m in messages if include_tools or m.role != MessageRole.TOOL],
        "exported_at": datetime.now(UTC).isoformat(),
    }


def to_markdown(session_obj: ChatSession, messages: list[ChatMessage], *, include_tools: bool = True) -> str:
    lines = [
        f"# {session_obj.title}",
        "",
        f"_Exported {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
    ]
    for m in messages:
        meta = m.meta or {}
        if m.role == MessageRole.TOOL:
            if not include_tools:
                continue
            lines.append(f"> **Tool `{meta.get('tool', 'tool')}`** ({meta.get('status', 'ok')})")
            for line in (m.content or "").splitlines()[:40]:
                lines.append(f"> {line}")
            lines.append("")
            continue
        who = "You" if m.role == MessageRole.USER else "Assistant"
        stamp = m.created_at.strftime("%Y-%m-%d %H:%M")
        lines.append(f"## {who} · {stamp}")
        lines.append("")
        lines.append(m.content or "")
        attachments = meta.get("attachments") or []
        if attachments:
            lines.append("")
            lines.append("Attachments: " + ", ".join(f"`{a.get('name')}`" for a in attachments))
        artifacts = meta.get("artifacts") or []
        if artifacts:
            lines.append("")
            lines.append("Artifacts: " + ", ".join(f"`{a.get('name')}`" for a in artifacts))
        citations = meta.get("citations") or []
        if citations:
            lines.append("")
            lines.append("Sources:")
            for i, c in enumerate(citations, 1):
                where = f", p. {c.get('page')}" if c.get("page") else ""
                lines.append(f"{i}. {c.get('document_name')}{where}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)
