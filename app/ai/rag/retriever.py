"""Hybrid retrieval: pgvector cosine search + Postgres full-text search, fused with
reciprocal rank fusion, optionally reranked, scoped to the user / session / collections."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.ai.llm import embedding_model_name
from app.ai.providers.catalog import ModelSpec
from app.ai.rag.embedder import embed_query
from app.ai.rag.rerank import rerank_chunks
from app.config import settings
from app.core.metrics import RAG_RETRIEVALS
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.utils.tokens import count_tokens

RRF_K = 60
# Relevance shown for chunks found only by keyword search (no vector similarity available).
KEYWORD_ONLY_SCORE = 0.5


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: UUID
    document_id: UUID
    document_name: str
    chunk_index: int
    content: str
    score: float  # cosine similarity when known, else KEYWORD_ONLY_SCORE
    page: int | None = None
    section: str | None = None
    start_index: int | None = None
    end_index: int | None = None
    collection_id: UUID | None = None
    vector_hit: bool = False
    text_hit: bool = False
    fused: float = 0.0
    rerank_score: float | None = None

    @property
    def match(self) -> str:
        if self.vector_hit and self.text_hit:
            return "hybrid"
        return "vector" if self.vector_hit else "keyword"

    def to_citation(self) -> dict[str, Any]:
        return {
            "document_id": str(self.document_id),
            "document_name": self.document_name,
            "chunk_id": str(self.chunk_id),
            "chunk_index": self.chunk_index,
            "page": self.page,
            "section": self.section,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "collection_id": str(self.collection_id) if self.collection_id else None,
            "score": round(self.score, 4),
            "match": self.match,
            "excerpt": self.content[:300],
        }


@dataclass(slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    # False when retrieval was skipped (no ready documents) and no embedding call was made.
    embedded: bool = False
    query_tokens: int = 0
    queries: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Scope:
    user_id: UUID
    session_id: UUID | None = None
    collection_ids: list[UUID] | None = None

    def apply(self, stmt: Select) -> Select:
        stmt = stmt.where(
            Document.user_id == self.user_id,
            Document.deleted_at.is_(None),
            Document.status == DocumentStatus.READY,
        )
        if self.collection_ids:
            in_collections = Document.collection_id.in_(self.collection_ids)
            if self.session_id is not None:
                stmt = stmt.where(or_(in_collections, Document.session_id == self.session_id))
            else:
                stmt = stmt.where(in_collections)
        elif self.session_id is not None:
            stmt = stmt.where(or_(Document.session_id == self.session_id, Document.session_id.is_(None)))
        return stmt


async def has_ready_documents(
    session: AsyncSession,
    *,
    user_id: UUID,
    session_id: UUID | None = None,
    collection_ids: list[UUID] | None = None,
) -> bool:
    """Cheap existence check so users without documents never pay for a query embedding."""
    scope = Scope(user_id=user_id, session_id=session_id, collection_ids=collection_ids)
    stmt = scope.apply(select(Document.id)).limit(1)
    return (await session.execute(stmt)).first() is not None


def _make(chunk: DocumentChunk, doc: Document, similarity: float | None) -> RetrievedChunk:
    meta = chunk.meta or {}
    return RetrievedChunk(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        document_name=doc.name,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        score=similarity if similarity is not None else KEYWORD_ONLY_SCORE,
        page=meta.get("page"),
        section=meta.get("section"),
        start_index=meta.get("start_index"),
        end_index=meta.get("end_index"),
        collection_id=doc.collection_id,
        vector_hit=similarity is not None,
    )


async def _vector_search(session: AsyncSession, *, embedding: list[float], k: int, scope: Scope):
    distance = DocumentChunk.embedding.cosine_distance(embedding).label("distance")
    stmt = (
        select(DocumentChunk, Document, distance)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.embedding.is_not(None))
    )
    stmt = scope.apply(stmt).order_by(distance.asc()).limit(k)
    return [(chunk, doc, 1.0 - float(dist)) for chunk, doc, dist in (await session.execute(stmt)).all()]


async def _text_search(session: AsyncSession, *, query: str, k: int, scope: Scope):
    tsquery = func.plainto_tsquery("english", query)
    rank = func.ts_rank_cd(DocumentChunk.content_tsv, tsquery).label("rank")
    stmt = (
        select(DocumentChunk, Document, rank)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.content_tsv.op("@@")(tsquery))
    )
    stmt = scope.apply(stmt).order_by(rank.desc()).limit(k)
    return [(chunk, doc, float(r)) for chunk, doc, r in (await session.execute(stmt)).all()]


def reciprocal_rank_fusion(rankings: list[list[UUID]], *, k: int = RRF_K) -> dict[UUID, float]:
    fused: dict[UUID, float] = {}
    for ranking in rankings:
        for position, chunk_id in enumerate(ranking):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + position + 1)
    return fused


async def retrieve_chunks(
    session: AsyncSession,
    *,
    user_id: UUID,
    query: str,
    session_id: UUID | None = None,
    top_k: int | None = None,
    collection_ids: list[UUID] | None = None,
    queries: list[str] | None = None,
    spec: ModelSpec | None = None,
) -> RetrievalResult:
    """Return the best chunks for `query` (and optional expanded `queries`).

    Vector candidates must clear `RAG_SIMILARITY_THRESHOLD`; keyword hits always qualify.
    Switch to Pinecone/Weaviate by adding alternate implementations behind the
    `VECTOR_BACKEND` setting and dispatching here.
    """
    if settings.VECTOR_BACKEND != "pgvector":
        raise NotImplementedError(
            f"Vector backend '{settings.VECTOR_BACKEND}' adapter not implemented in this build."
        )

    scope = Scope(user_id=user_id, session_id=session_id, collection_ids=collection_ids)
    if not await has_ready_documents(
        session, user_id=user_id, session_id=session_id, collection_ids=collection_ids
    ):
        RAG_RETRIEVALS.labels("skipped").inc()
        return RetrievalResult()

    k = top_k or settings.RAG_TOP_K
    candidates_per_list = max(k, k * settings.RAG_CANDIDATES_MULTIPLIER)
    all_queries = [q for q in (queries or [query]) if q and q.strip()] or [query]
    registry: dict[UUID, RetrievedChunk] = {}
    rankings: list[list[UUID]] = []
    query_tokens = 0
    emb_model = embedding_model_name()

    for q in all_queries:
        embedding = await embed_query(q)
        query_tokens += count_tokens(q, model=emb_model)
        ranking: list[UUID] = []
        for chunk, doc, sim in await _vector_search(
            session, embedding=embedding, k=candidates_per_list, scope=scope
        ):
            rc = registry.get(chunk.id)
            if rc is None:
                rc = registry[chunk.id] = _make(chunk, doc, sim)
            else:
                rc.score = max(rc.score, sim) if rc.vector_hit else sim
                rc.vector_hit = True
            ranking.append(chunk.id)
        rankings.append(ranking)

        if settings.RAG_HYBRID_SEARCH:
            ranking = []
            for chunk, doc, _rank in await _text_search(session, query=q, k=candidates_per_list, scope=scope):
                rc = registry.get(chunk.id)
                if rc is None:
                    rc = registry[chunk.id] = _make(chunk, doc, None)
                rc.text_hit = True
                ranking.append(chunk.id)
            rankings.append(ranking)

    fused = reciprocal_rank_fusion(rankings)
    candidates = []
    for rc in registry.values():
        rc.fused = fused.get(rc.chunk_id, 0.0)
        if rc.text_hit or rc.score >= settings.RAG_SIMILARITY_THRESHOLD:
            candidates.append(rc)
    candidates.sort(key=lambda c: (-c.fused, -c.score))

    chunks = await rerank_chunks(query, candidates[: max(k * 2, k)], top_k=k, spec=spec)
    RAG_RETRIEVALS.labels("hit" if chunks else "empty").inc()
    return RetrievalResult(chunks=chunks, embedded=True, query_tokens=query_tokens, queries=all_queries)


def format_context(chunks: list[RetrievedChunk]) -> str:
    if not chunks:
        return "(no relevant documents found)"
    parts = []
    for c in chunks:
        where = []
        if c.page is not None:
            where.append(f"page {c.page}")
        if c.section:
            where.append(f"section: {c.section}")
        location = f" ({', '.join(where)})" if where else ""
        parts.append(f"[doc:{c.document_name}#chunk{c.chunk_index}]{location}\n{c.content}")
    return "\n\n---\n\n".join(parts)
