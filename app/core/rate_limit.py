from __future__ import annotations

import time

from fastapi import Request

from app.cache.redis_client import get_redis
from app.config import settings
from app.core.exceptions import RateLimitError


class TokenBucketRateLimiter:
    """Simple per-key fixed-window rate limiter backed by Redis.

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
                details={"limit": self.requests, "window_seconds": self.window_seconds},
            )


def rate_limit_dependency(scope: str = "default"):
    limiter = TokenBucketRateLimiter()

    async def _dep(request: Request) -> None:
        client_id = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        user_id = getattr(request.state, "user_id", None)
        identity = str(user_id) if user_id else client_id
        await limiter.hit(f"{scope}:{identity}")

    return _dep
