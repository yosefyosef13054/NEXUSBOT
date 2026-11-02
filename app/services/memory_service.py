"""User-level long-term memory: CRUD, recall for prompt injection, background extraction."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import build_chat_llm
from app.ai.providers.catalog import ModelSpec
from app.ai.rag.embedder import embed_query
from app.config import settings
from app.core.exceptions import NotFoundError
from app.core.logger import get_logger
from app.db.session import db_session
from app.models.memory import Memory, MemoryKind

logger = get_logger(__name__)

EXTRACT_SYSTEM = (
    "You maintain long-term memory about a user for an assistant. From the exchange below, "
    "extract only durable, reusable information about the user: stable facts (name, role, "
    "location, projects), preferences (formats, languages, tone), and standing instructions. "
    "Skip anything transient, speculative, or about the assistant's answer itself. "
    'Reply with JSON only: a list of {"content": "...", "kind": "fact|preference|instruction", '
    '"importance": 1-5}. Reply [] when there is nothing worth remembering.'
)


async def list_memories(
    db: AsyncSession, *, user_id: UUID, org_id: UUID | None, page: int, page_size: int
) -> tuple[list[Memory], int]:
    base = select(Memory).where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
    if org_id is not None:
        base = base.where((Memory.org_id == org_id) | (Memory.org_id.is_(None)))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                base.order_by(Memory.importance.desc(), Memory.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def count_memories(db: AsyncSession, *, user_id: UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
            )
        ).scalar_one()
    )


async def _is_duplicate(db: AsyncSession, *, user_id: UUID, embedding: list[float]) -> bool:
    distance = Memory.embedding.cosine_distance(embedding)
    stmt = (
        select(distance)
        .where(Memory.user_id == user_id, Memory.deleted_at.is_(None), Memory.embedding.is_not(None))
        .order_by(distance.asc())
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    return row is not None and (1.0 - float(row[0])) >= settings.MEMORY_DEDUP_SIMILARITY


async def has_similar(db: AsyncSession, *, user_id: UUID, content: str) -> bool:
    content = " ".join(content.split())[:2000]
    if not content:
        return False
    return await _is_duplicate(db, user_id=user_id, embedding=await embed_query(content))


async def create_memory(
    db: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID | None,
    content: str,
    kind: str = "fact",
    importance: int = 3,
    source_message_id: UUID | None = None,
    skip_duplicates: bool = False,
) -> Memory | None:
    content = " ".join(content.split())[:2000]
    if not content:
        return None
    if await count_memories(db, user_id=user_id) >= settings.MEMORY_MAX_PER_USER:
        logger.info("memory_cap_reached", user_id=str(user_id))
        return None
    embedding = await embed_query(content)
    if skip_duplicates and await _is_duplicate(db, user_id=user_id, embedding=embedding):
        return None
    obj = Memory(
        user_id=user_id,
        org_id=org_id,
        content=content,
        kind=MemoryKind(kind) if kind in MemoryKind._value2member_map_ else MemoryKind.FACT,
        importance=max(1, min(5, int(importance or 3))),
        embedding=embedding,
        source_message_id=source_message_id,
    )
    db.add(obj)
    await db.flush()
    return obj


async def get_memory(db: AsyncSession, *, user_id: UUID, memory_id: UUID) -> Memory:
    obj = await db.get(Memory, memory_id)
    if obj is None or obj.user_id != user_id or obj.deleted_at is not None:
        raise NotFoundError("Memory not found")
    return obj


async def update_memory(
    db: AsyncSession,
    *,
    user_id: UUID,
    memory_id: UUID,
    content: str | None,
    kind: str | None,
    importance: int | None,
) -> Memory:
    obj = await get_memory(db, user_id=user_id, memory_id=memory_id)
    if content is not None:
        obj.content = " ".join(content.split())[:2000]
        obj.embedding = await embed_query(obj.content)
    if kind is not None:
        obj.kind = MemoryKind(kind)
    if importance is not None:
        obj.importance = max(1, min(5, importance))
    await db.flush()
    await db.refresh(obj)
    return obj


async def delete_memory(db: AsyncSession, *, user_id: UUID, memory_id: UUID) -> None:
    obj = await get_memory(db, user_id=user_id, memory_id=memory_id)
    obj.deleted_at = datetime.now(UTC)
    await db.flush()


async def clear_memories(db: AsyncSession, *, user_id: UUID) -> int:
    result = await db.execute(
        update(Memory)
        .where(Memory.user_id == user_id, Memory.deleted_at.is_(None))
        .values(deleted_at=datetime.now(UTC))
    )
    return int(result.rowcount or 0)


async def recall(
    db: AsyncSession, *, user_id: UUID, org_id: UUID | None, query: str, k: int | None = None
) -> list[Memory]:
    """Most relevant memories for `query` (instructions always included), marking them used."""
    k = k or settings.MEMORY_TOP_K
    if await count_memories(db, user_id=user_id) == 0:
        return []
    embedding = await embed_query(query)
    distance = Memory.embedding.cosine_distance(embedding).label("distance")
    base = select(Memory).where(
        Memory.user_id == user_id, Memory.deleted_at.is_(None), Memory.embedding.is_not(None)
    )
    if org_id is not None:
        base = base.where((Memory.org_id == org_id) | (Memory.org_id.is_(None)))
    rows = (await db.execute(base.order_by(distance.asc()).limit(k))).scalars().all()
    instructions = (
        (
            await db.execute(
                base.where(Memory.kind == MemoryKind.INSTRUCTION).order_by(Memory.importance.desc()).limit(k)
            )
        )
        .scalars()
        .all()
    )
    seen: set[UUID] = set()
    out: list[Memory] = []
    for m in [*instructions, *rows]:
        if m.id not in seen:
            seen.add(m.id)
            out.append(m)
    now = datetime.now(UTC)
    for m in out:
        m.last_used_at = now
    await db.flush()
    return out[: max(k, len(instructions))]


def format_memories(memories: list[Memory]) -> str:
    if not memories:
        return ""
    lines = []
    for m in memories:
        tag = {"fact": "Fact", "preference": "Preference", "instruction": "Standing instruction"}[
            m.kind.value
        ]
        lines.append(f"- ({tag}) {m.content}")
    return "What you remember about this user (use when relevant, never recite unprompted):\n" + "\n".join(
        lines
    )


def _parse_items(text: str) -> list[dict[str, Any]]:
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict) and d.get("content")]


async def extract_and_store(
    *,
    user_id: UUID,
    org_id: UUID | None,
    user_text: str,
    assistant_text: str,
    source_message_id: UUID | None,
    spec: ModelSpec | None = None,
) -> int:
    """Background job: ask the model what is worth remembering from a turn and store it."""
    if not user_text.strip():
        return 0
    try:
        llm = build_chat_llm(spec=spec, streaming=False, temperature=0, max_tokens=400)
        out = await llm.ainvoke(
            [
                SystemMessage(content=EXTRACT_SYSTEM),
                HumanMessage(content=f"User: {user_text[:4000]}\n\nAssistant: {assistant_text[:2000]}"),
            ]
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("memory_extract_failed", error=str(e)[:200])
        return 0
    items = _parse_items(out.content if isinstance(out.content, str) else str(out.content))
    if not items:
        return 0
    stored = 0
    async with db_session() as db:
        for item in items[:5]:
            created = await create_memory(
                db,
                user_id=user_id,
                org_id=org_id,
                content=str(item.get("content", "")),
                kind=str(item.get("kind", "fact")),
                importance=int(item.get("importance", 3) or 3),
                source_message_id=source_message_id,
                skip_duplicates=True,
            )
            if created is not None:
                stored += 1
    if stored:
        logger.info("memories_stored", user_id=str(user_id), count=stored)
    return stored
