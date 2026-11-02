from __future__ import annotations

import time
from typing import Callable

from fastapi import Depends, Request

from app.cache.redis_client import get_redis
from app.config import settings
from app.core.dependencies import current_user
from app.core.exceptions import RateLimitError
from app.models.user import User


class FixedWindowRateLimiter:
    """Per-key fixed-window rate limiter backed by Redis.

    Used as a FastAPI dependency. For more sophisticated needs (sliding window,
    bursty tokens), swap the implementation here without touching call sites.
    """

    def __init__(self, *, requests: int | None = None, window_seconds: int = 60):
        self.requests = requests or settings.RATE_LIMIT_PER_MINUTE
        self.window_seconds = window_seconds

    async def hit(self, key: str) -> None:
        redis = get_redis()
        window = int(time.time() // self.window_seconds)
        bucket = f"rl:{key}:{window}"
        pipe = redis.pipeline()
        pipe.incr(bucket, 1)
        pipe.expire(bucket, self.window_seconds)
        count, _ = await pipe.execute()
        if int(count) > self.requests:
            raise RateLimitError(
                "Too many requests",
                details={
                    "limit": self.requests,
                    "window_seconds": self.window_seconds,
                    "retry_after_seconds": self.window_seconds - int(time.time() % self.window_seconds),
                },
            )


# Backwards-compatible alias.
TokenBucketRateLimiter = FixedWindowRateLimiter


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit_dependency(scope: str = "default", *, requests: int | None = None) -> Callable:
    """Rate limit keyed on the authenticated user.

    Depends on `current_user` explicitly so the identity is resolved *before* the
    limiter runs; a bare route-level dependency would execute first and only ever see
    the client IP.
    """
    limiter = FixedWindowRateLimiter(requests=requests)

    async def _dep(request: Request, user: User = Depends(current_user)) -> None:
        await limiter.hit(f"{scope}:user:{user.id}")

    return _dep


def ip_rate_limit_dependency(scope: str, *, requests: int | None = None) -> Callable:
    """Rate limit keyed on the client IP, for unauthenticated endpoints (login, register)."""
    limiter = FixedWindowRateLimiter(requests=requests or settings.AUTH_RATE_LIMIT_PER_MINUTE)

    async def _dep(request: Request) -> None:
        await limiter.hit(f"{scope}:ip:{client_ip(request)}")

    return _dep
