from __future__ import annotations

from functools import lru_cache

import tiktoken

from app.config import settings


@lru_cache
def _encoder(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str, *, model: str | None = None) -> int:
    if not text:
        return 0
    return len(_encoder(model or settings.OPENAI_CHAT_MODEL).encode(text))


# Approximate USD/1K tokens — keep tunable from env / pricing service later.
_PRICING_USD_PER_1K = {
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "text-embedding-3-small": (0.00002, 0.0),
    "text-embedding-3-large": (0.00013, 0.0),
}


def estimate_cost(
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int = 0,
) -> float:
    in_rate, out_rate = _PRICING_USD_PER_1K.get(model, (0.0, 0.0))
    return (prompt_tokens / 1000) * in_rate + (completion_tokens / 1000) * out_rate
