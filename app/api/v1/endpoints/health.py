from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import get_redis
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(db: AsyncSession = Depends(get_db)):
    await db.execute(text("SELECT 1"))
    redis = get_redis()
    await redis.ping()
    return {"status": "ready"}
