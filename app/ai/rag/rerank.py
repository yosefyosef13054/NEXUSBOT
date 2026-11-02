"""Second-stage reranking of retrieval candidates.

`none` keeps the fused order. `llm` asks the chat model to grade each candidate's
relevance (one call per turn); a hosted reranker (Cohere, Voyage, a cross-encoder
service) plugs in the same way — implement `_score_*` and add it to `RERANKERS`.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Awaitable, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from app.ai.llm import build_chat_llm
from app.ai.providers.catalog import ModelSpec
from app.config import settings
from app.core.logger import get_logger

if TYPE_CHECKING:
    from app.ai.rag.retriever import RetrievedChunk

logger = get_logger(__name__)

RERANK_SYSTEM = (
    "You grade search results. For each passage, output how useful it is for answering the "
    'query on a 0-10 scale. Reply with JSON only: a list of objects {"index": int, "score": number}.'
)

Scorer = Callable[[str, list["RetrievedChunk"], ModelSpec | None], Awaitable[list[float] | None]]


async def _score_llm(query: str, chunks: list[RetrievedChunk], spec: ModelSpec | None) -> list[float] | None:
    passages = "\n\n".join(f"[{i}] {c.content[:1200]}" for i, c in enumerate(chunks))
    llm = build_chat_llm(spec=spec, streaming=False, temperature=0, max_tokens=400)
    out = await llm.ainvoke(
        [
            SystemMessage(content=RERANK_SYSTEM),
            HumanMessage(content=f"Query: {query}\n\nPassages:\n{passages}"),
        ]
    )
    text = out.content if isinstance(out.content, str) else str(out.content)
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    scores = [0.0] * len(chunks)
    for item in data:
        try:
            idx = int(item["index"])
            if 0 <= idx < len(chunks):
                scores[idx] = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
    return scores


RERANKERS: dict[str, Scorer] = {"llm": _score_llm}


async def rerank_chunks(
    query: str,
    chunks: list[RetrievedChunk],
    *,
    top_k: int,
    spec: ModelSpec | None = None,
    method: str | None = None,
) -> list[RetrievedChunk]:
    method = method or settings.RAG_RERANKER
    scorer = RERANKERS.get(method)
    if scorer is None or len(chunks) <= 1:
        return chunks[:top_k]
    try:
        scores = await scorer(query, chunks, spec)
    except Exception as e:  # noqa: BLE001
        logger.warning("rerank_failed", method=method, error=str(e)[:200])
        return chunks[:top_k]
    if not scores:
        return chunks[:top_k]
    order = sorted(range(len(chunks)), key=lambda i: (-scores[i], i))
    reranked = [chunks[i] for i in order][:top_k]
    for i, c in zip(order, reranked, strict=False):
        c.rerank_score = scores[i]
    return reranked
