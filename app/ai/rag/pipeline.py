from __future__ import annotations

from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.rag.embedder import embed_texts
from app.ai.rag.loader import load_file_as_documents
from app.ai.rag.splitter import split_documents
from app.core.logger import get_logger
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.utils.tokens import count_tokens

logger = get_logger(__name__)


async def ingest_document(session: AsyncSession, *, document_id: UUID) -> None:
    """Load → split → embed → persist for a single document row.

    Designed for Celery: it's idempotent (clears prior chunks first) and updates
    the document's status so the UI can poll or stream the state.
    """
    doc = await session.get(Document, document_id)
    if doc is None or doc.deleted_at is not None:
        logger.warning("ingest_skip_missing", document_id=str(document_id))
        return

    doc.status = DocumentStatus.PROCESSING
    doc.error = None
    await session.flush()

    try:
        path = Path(doc.storage_path)
        if not path.exists():
            raise FileNotFoundError(f"Storage path missing: {path}")

        docs = load_file_as_documents(path)
        chunks = split_documents(docs)
        if not chunks:
            raise ValueError("Document produced no chunks (empty or unsupported content).")

        await session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == doc.id)
        )
        await session.flush()

        texts = [c.page_content for c in chunks]
        embeddings = await embed_texts(texts)

        for idx, (chunk, vector) in enumerate(zip(chunks, embeddings)):
            session.add(
                DocumentChunk(
                    document_id=doc.id,
                    chunk_index=idx,
                    content=chunk.page_content,
                    tokens=count_tokens(chunk.page_content),
                    embedding=vector,
                    meta={"source": chunk.metadata.get("source"), **chunk.metadata},
                )
            )

        doc.status = DocumentStatus.READY
        doc.meta = {**(doc.meta or {}), "chunk_count": len(chunks)}
        await session.flush()
        logger.info("ingest_done", document_id=str(doc.id), chunks=len(chunks))
    except Exception as e:  # noqa: BLE001
        doc.status = DocumentStatus.FAILED
        doc.error = str(e)[:2000]
        await session.flush()
        logger.exception("ingest_failed", document_id=str(doc.id))
        raise
