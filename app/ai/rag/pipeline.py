from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import embedding_model_name
from app.ai.rag.embedder import embed_texts, embedding_tokens
from app.ai.rag.loader import load_file_as_documents
from app.ai.rag.splitter import split_documents
from app.config import settings
from app.core.logger import get_logger
from app.core.metrics import INGEST_DOCUMENTS
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.services.usage_service import log_ai_usage
from app.utils.tokens import count_tokens

logger = get_logger(__name__)


def _chunk_meta(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep only what the client may see; never leak server storage paths."""
    meta: dict[str, Any] = {}
    if "page" in metadata:
        meta["page"] = metadata["page"]
    if "start_index" in metadata:
        meta["start_index"] = metadata["start_index"]
    if metadata.get("source"):
        meta["source"] = os.path.basename(str(metadata["source"]))
    return meta


async def _set_progress(session: AsyncSession, doc: Document, done: int, total: int) -> None:
    pct = int(100 * done / total) if total else 100
    doc.meta = {**(doc.meta or {}), "progress": pct, "chunk_count": total}
    await session.flush()
    await session.commit()


async def ingest_document(session: AsyncSession, *, document_id: UUID, batch_size: int | None = None) -> None:
    """Load → split → embed → persist for a single document row.

    Designed for Celery: it's idempotent (clears prior chunks first), commits progress
    after every embedding batch so `GET /documents/{id}` can show a percentage, and
    records embedding usage so cost reports include ingestion.
    """
    doc = await session.get(Document, document_id)
    if doc is None or doc.deleted_at is not None:
        logger.warning("ingest_skip_missing", document_id=str(document_id))
        return

    doc.status = DocumentStatus.PROCESSING
    doc.error = None
    doc.meta = {**(doc.meta or {}), "progress": 0}
    await session.flush()
    await session.commit()

    try:
        path = Path(doc.storage_path)
        if not path.exists():
            raise FileNotFoundError(f"Storage path missing: {path}")

        # Loaders are synchronous and CPU/IO bound; keep the event loop responsive.
        docs = await asyncio.to_thread(load_file_as_documents, path)
        chunks = split_documents(docs)
        if not chunks:
            raise ValueError("Document produced no chunks (empty or unsupported content).")

        await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
        await session.flush()

        texts = [c.page_content for c in chunks]
        total = len(texts)
        size = max(1, batch_size or settings.EMBEDDING_BATCH_SIZE)
        total_tokens = 0

        for start in range(0, total, size):
            batch_texts = texts[start : start + size]
            vectors = await embed_texts(batch_texts, batch_size=size)
            for offset, (chunk, vector) in enumerate(
                zip(chunks[start : start + size], vectors, strict=False)
            ):
                session.add(
                    DocumentChunk(
                        document_id=doc.id,
                        chunk_index=start + offset,
                        content=chunk.page_content,
                        tokens=count_tokens(chunk.page_content),
                        embedding=vector,
                        meta=_chunk_meta(chunk.metadata),
                    )
                )
            total_tokens += embedding_tokens(batch_texts)
            await _set_progress(session, doc, min(start + len(batch_texts), total), total)

        await log_ai_usage(
            session,
            user_id=doc.user_id,
            session_id=doc.session_id,
            operation="embedding",
            model=embedding_model_name(),
            prompt_tokens=total_tokens,
            meta={"document_id": str(doc.id), "chunks": total},
        )
        doc.status = DocumentStatus.READY
        doc.meta = {
            **(doc.meta or {}),
            "progress": 100,
            "chunk_count": total,
            "embedding_tokens": total_tokens,
        }
        await session.flush()
        await session.commit()
        INGEST_DOCUMENTS.labels("ready").inc()
        logger.info("ingest_done", document_id=str(doc.id), chunks=total, tokens=total_tokens)
    except Exception as e:  # noqa: BLE001
        doc.status = DocumentStatus.FAILED
        doc.error = str(e)[:2000]
        await session.flush()
        await session.commit()
        INGEST_DOCUMENTS.labels("failed").inc()
        logger.exception("ingest_failed", document_id=str(doc.id))
        raise
