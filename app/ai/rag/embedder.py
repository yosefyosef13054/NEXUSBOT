from __future__ import annotations

from typing import Awaitable, Callable, Sequence

from app.ai.llm import embedding_model_name, get_embeddings
from app.config import settings
from app.utils.tokens import count_tokens

ProgressCallback = Callable[[int, int], Awaitable[None]]


def embedding_tokens(texts: Sequence[str]) -> int:
    """Local token estimate for embedding calls (providers don't report usage via LangChain)."""
    model = embedding_model_name()
    return sum(count_tokens(t, model=model) for t in texts)


async def embed_texts(
    texts: Sequence[str],
    *,
    batch_size: int | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[list[float]]:
    """Batch-embed a sequence of texts.

    Batches keep a single large document under provider request limits and let the
    caller report progress (`on_progress(done, total)`) between batches.
    """
    if not texts:
        return []
    embeddings = get_embeddings()
    size = max(1, batch_size or settings.EMBEDDING_BATCH_SIZE)
    out: list[list[float]] = []
    total = len(texts)
    for start in range(0, total, size):
        batch = list(texts[start : start + size])
        out.extend(await embeddings.aembed_documents(batch))
        if on_progress is not None:
            await on_progress(min(start + len(batch), total), total)
    return out


async def embed_query(text: str) -> list[float]:
    embeddings = get_embeddings()
    return await embeddings.aembed_query(text)
