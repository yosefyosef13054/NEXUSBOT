from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import embedding_model_name
from app.ai.rag.embedder import embed_query
from app.config import settings
from app.core.metrics import RAG_RETRIEVALS
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.utils.tokens import count_tokens


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: UUID
    document_id: UUID
    document_name: str
    chunk_index: int
    content: str
    score: float


@dataclass(slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    # False when retrieval was skipped (no ready documents) and no embedding call was made.
    embedded: bool = False
    query_tokens: int = 0


async def has_ready_documents(
    session: AsyncSession, *, user_id: UUID, session_id: UUID | None = None
) -> bool:
    """Cheap existence check so users without documents never pay for a query embedding."""
    stmt = select(Document.id).where(
        Document.user_id == user_id,
        Document.deleted_at.is_(None),
        Document.status == DocumentStatus.READY,
    )
    if session_id is not None:
        stmt = stmt.where((Document.session_id == session_id) | (Document.session_id.is_(None)))
    return (await session.execute(stmt.limit(1))).first() is not None


async def retrieve_chunks(
    session: AsyncSession,
    *,
    user_id: UUID,
    query: str,
    session_id: UUID | None = None,
    top_k: int | None = None,
) -> RetrievalResult:
    """pgvector cosine retrieval scoped to the user (and optionally session).

    Returns chunks ordered by similarity, filtered by `RAG_SIMILARITY_THRESHOLD`.
    Switch to Pinecone/Weaviate by adding alternate implementations behind the
    `VECTOR_BACKEND` setting and dispatching here.
    """
    if settings.VECTOR_BACKEND != "pgvector":
        raise NotImplementedError(
            f"Vector backend '{settings.VECTOR_BACKEND}' adapter not implemented in this build."
        )

    if not await has_ready_documents(session, user_id=user_id, session_id=session_id):
        RAG_RETRIEVALS.labels("skipped").inc()
        return RetrievalResult()

    embedding = await embed_query(query)
    k = top_k or settings.RAG_TOP_K
    distance = DocumentChunk.embedding.cosine_distance(embedding).label("distance")

    stmt = (
        select(DocumentChunk, Document.name, distance)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.user_id == user_id,
            Document.deleted_at.is_(None),
            Document.status == DocumentStatus.READY,
            DocumentChunk.embedding.is_not(None),
        )
        .order_by(distance.asc())
        .limit(k)
    )
    if session_id is not None:
        stmt = stmt.where((Document.session_id == session_id) | (Document.session_id.is_(None)))

    rows = (await session.execute(stmt)).all()
    out: list[RetrievedChunk] = []
    for chunk, doc_name, dist in rows:
        score = 1.0 - float(dist)
        if score < settings.RAG_SIMILARITY_THRESHOLD:
            continue
        out.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                document_name=doc_name,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                score=score,
            )
        )
    RAG_RETRIEVALS.labels("hit" if out else "empty").inc()
    return RetrievalResult(
        chunks=out, embedded=True, query_tokens=count_tokens(query, model=embedding_model_name())
    )


def format_context(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return "(no relevant documents found)"
    parts = []
    for c in chunks:
        parts.append(f"[doc:{c.document_name}#chunk{c.chunk_index}]\n{c.content}")
    return "\n\n---\n\n".join(parts)
