from __future__ import annotations

import asyncio
from uuid import UUID

from celery.utils.log import get_task_logger

from app.ai.rag.pipeline import ingest_document
from app.db.session import db_session
from app.workers.celery_app import celery

logger = get_task_logger(__name__)


def _run(coro):
    """Run an async coroutine from a sync Celery task body."""
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio.get_event_loop().is_running() is False else asyncio.run(coro)


@celery.task(
    name="app.workers.tasks.ingest_document_task",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def ingest_document_task(document_id: str) -> dict:
    """Background ingestion: load, split, embed, persist chunks."""
    doc_uuid = UUID(document_id)

    async def _do():
        async with db_session() as session:
            await ingest_document(session, document_id=doc_uuid)

    try:
        asyncio.run(_do())
        return {"document_id": document_id, "status": "ready"}
    except Exception as e:  # noqa: BLE001
        logger.exception("ingest_task_failed", extra={"document_id": document_id})
        raise
