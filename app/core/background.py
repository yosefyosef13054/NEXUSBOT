"""In-process background work.

Used for cheap post-turn maintenance (title generation, summary compaction) that must
not sit in the request/stream hot path. Tasks are tracked so the lifespan can drain
them on shutdown. Heavier or durable jobs belong in Celery (`app.workers`).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

from app.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

_tasks: set[asyncio.Task] = set()


async def _runner(name: str, coro: Coroutine[Any, Any, Any]) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — background failures must never surface to users
        logger.warning("background_task_failed", task=name, error=str(e)[:300])


async def run_in_background(name: str, coro: Coroutine[Any, Any, Any]) -> asyncio.Task | None:
    """Schedule `coro` without awaiting it. With BACKGROUND_TASKS_INLINE the coroutine runs
    to completion first (deterministic tests)."""
    if settings.BACKGROUND_TASKS_INLINE:
        await _runner(name, coro)
        return None
    task = asyncio.create_task(_runner(name, coro), name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def spawn_detached(name: str, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Always start a tracked task, even when BACKGROUND_TASKS_INLINE is set.

    For cleanup that must not be awaited from a cancelled scope (e.g. persisting a
    partial reply after the client disconnected): the task runs to completion on the
    loop regardless of what happens to the caller. Tracked so it cannot be garbage
    collected mid-run and so shutdown drains it.
    """
    task = asyncio.create_task(_runner(name, coro), name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def drain(timeout: float = 10.0) -> None:
    """Wait for in-flight background tasks (called from the app lifespan on shutdown)."""
    pending = [t for t in _tasks if not t.done()]
    if not pending:
        return
    logger.info("background_drain", pending=len(pending))
    done, still = await asyncio.wait(pending, timeout=timeout)
    for t in still:
        t.cancel()


def pending_count() -> int:
    return sum(1 for t in _tasks if not t.done())


# Convenience alias for call sites that already have a coroutine function + args.
def task(
    fn: Callable[..., Coroutine[Any, Any, Any]],
) -> Callable[..., Coroutine[Any, Any, asyncio.Task | None]]:
    async def _wrapped(*args: Any, **kwargs: Any) -> asyncio.Task | None:
        return await run_in_background(fn.__name__, fn(*args, **kwargs))

    return _wrapped
