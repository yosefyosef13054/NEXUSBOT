"""Idempotency keys for message sends.

A client that retries after a timeout sends the same `Idempotency-Key`; the first attempt
claims the key, later attempts get the stored outcome instead of creating a duplicate turn.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID

from app.cache.redis_client import get_redis
from app.config import settings

HEADER = "Idempotency-Key"


def _key(user_id: UUID, key: str) -> str:
    return f"idem:{user_id}:{key[:128]}"


async def begin(user_id: UUID, key: str) -> tuple[str, dict[str, Any] | None]:
    """Claim the key. Returns ('new', None), ('in_progress', meta) or ('done', payload)."""
    r = get_redis()
    claimed = await r.set(
        _key(user_id, key),
        json.dumps({"status": "in_progress", "started_at": time.time()}),
        nx=True,
        ex=settings.IDEMPOTENCY_TTL_SECONDS,
    )
    if claimed:
        return "new", None
    raw = await r.get(_key(user_id, key))
    if not raw:
        return "new", None
    data = json.loads(raw)
    return data.get("status", "done"), data


async def complete(user_id: UUID, key: str, payload: dict[str, Any]) -> None:
    await get_redis().set(
        _key(user_id, key),
        json.dumps({"status": "done", **payload}, default=str),
        ex=settings.IDEMPOTENCY_TTL_SECONDS,
    )


async def release(user_id: UUID, key: str) -> None:
    """Drop a claim after a failed attempt so the client can retry."""
    await get_redis().delete(_key(user_id, key))
