from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import settings


def make_engine(*, pooled: bool = True) -> AsyncEngine:
    """Create the async engine.

    The API process keeps a connection pool. Workers run each task under a fresh event
    loop (`asyncio.run`), and asyncpg connections are bound to the loop they were created
    on, so a pooled connection reused across tasks would blow up. Workers therefore use
    `NullPool`: one connection per session, closed on exit.
    """
    kwargs: dict = {"echo": False, "pool_pre_ping": True, "future": True}
    if pooled:
        kwargs.update(pool_size=20, max_overflow=10)
    else:
        kwargs.update(poolclass=NullPool)
    return create_async_engine(settings.DATABASE_URL, **kwargs)


engine: AsyncEngine = make_engine(pooled=True)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)

_worker_engine: AsyncEngine | None = None
_worker_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_worker_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _worker_engine, _worker_sessionmaker
    if _worker_sessionmaker is None:
        _worker_engine = make_engine(pooled=False)
        _worker_sessionmaker = async_sessionmaker(
            bind=_worker_engine, expire_on_commit=False, autoflush=False
        )
    return _worker_sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an AsyncSession."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def db_session() -> AsyncIterator[AsyncSession]:
    """Use outside the request scope inside the API process (background tasks, tools)."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def worker_db_session() -> AsyncIterator[AsyncSession]:
    """Use from Celery tasks (each task runs its own event loop)."""
    maker = get_worker_sessionmaker()
    async with maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
