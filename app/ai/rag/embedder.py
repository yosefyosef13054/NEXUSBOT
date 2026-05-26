from __future__ import annotations

from typing import Sequence

from app.ai.llm import get_embeddings


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Batch-embed a sequence of texts.

    Centralizes the call so swapping the embedding provider only requires editing
    `get_embeddings` in `app.ai.llm`.
    """
    if not texts:
        return []
    embeddings = get_embeddings()
    return await embeddings.aembed_documents(list(texts))


async def embed_query(text: str) -> list[float]:
    embeddings = get_embeddings()
    return await embeddings.aembed_query(text)
