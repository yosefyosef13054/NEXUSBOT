"""LangGraph checkpointer backed by the application's Postgres.

The agent graph needs durable state between an approval interrupt and the user's
decision (which may arrive minutes later, on another worker). The saver's tables are
created by alembic migration 0003 (same SQL the saver's own `setup()` would run).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy import text

from app.config import settings
from app.core.logger import get_logger
from app.db.session import engine

logger = get_logger(__name__)

_saver: Any = None
_pool: Any = None
_lock = asyncio.Lock()


def psycopg_conninfo() -> str:
    """psycopg wants plain `postgresql://…`; strip any SQLAlchemy `+driver` suffix."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", settings.SYNC_DATABASE_URL)


async def get_checkpointer() -> Any:
    """Process-wide `AsyncPostgresSaver` (bound to the running loop, created lazily)."""
    global _saver, _pool
    if _saver is not None:
        return _saver
    async with _lock:
        if _saver is not None:
            return _saver
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        _pool = AsyncConnectionPool(
            conninfo=psycopg_conninfo(),
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await _pool.open()
        _saver = AsyncPostgresSaver(_pool)
        logger.info("checkpointer_ready")
        return _saver


async def close_checkpointer() -> None:
    global _saver, _pool
    if _pool is not None:
        await _pool.close()
    _saver = None
    _pool = None


async def delete_thread(thread_id: str) -> None:
    """Drop a finished turn's checkpoints (best effort)."""
    async with engine.begin() as conn:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await conn.execute(text(f"DELETE FROM {table} WHERE thread_id = :t"), {"t": thread_id})
