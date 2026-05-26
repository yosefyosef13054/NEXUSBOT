from __future__ import annotations

from typing import Optional

from redis.asyncio import Redis, from_url

from app.config import settings

_redis: Optional[Redis] = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
