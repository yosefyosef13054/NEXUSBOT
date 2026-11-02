from __future__ import annotations

import asyncio
from uuid import UUID

from celery.utils.log import get_task_logger

from app.ai.rag.pipeline import ingest_document
from app.db.session import worker_db_session
from app.workers.celery_app import celery

logger = get_task_logger(__name__)


@celery.task(
    name="app.workers.tasks.ingest_document_task",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def ingest_document_task(document_id: str) -> dict:
    """Background ingestion: load, split, embed, persist chunks.

    Each task runs on a fresh event loop; `worker_db_session` uses a NullPool engine so
    no asyncpg connection outlives the loop it was created on.
    """
    doc_uuid = UUID(document_id)

    async def _do() -> None:
        async with worker_db_session() as session:
            await ingest_document(session, document_id=doc_uuid)

    try:
        asyncio.run(_do())
        return {"document_id": document_id, "status": "ready"}
    except Exception:
        logger.exception("ingest_task_failed", extra={"document_id": document_id})
        raise
