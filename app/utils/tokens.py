from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from app.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

# Rough fallback used when tiktoken cannot load an encoding (offline environments,
# unknown models). ~4 characters per token is the usual English heuristic.
_CHARS_PER_TOKEN = 4
# Per-message overhead for chat-formatted prompts (role markers, separators).
_MESSAGE_OVERHEAD_TOKENS = 4


@lru_cache
def _encoder(model: str):
    try:
        import tiktoken

        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")
    except Exception as e:  # noqa: BLE001 — network/IO failures loading the BPE file
        logger.warning("tiktoken_unavailable", model=model, error=str(e)[:200])
        return None


def _approx(text: str) -> int:
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def count_tokens(text: str, *, model: str | None = None) -> int:
    """Count tokens for `text`, never raising and never touching the network at request time
    beyond tiktoken's one-off encoding load."""
    if not text:
        return 0
    enc = _encoder(model or settings.OPENAI_CHAT_MODEL)
    if enc is None:
        return _approx(text)
    try:
        return len(enc.encode(text, disallowed_special=()))
    except Exception:  # noqa: BLE001
        return _approx(text)


def count_message_tokens(contents: Iterable[str], *, model: str | None = None) -> int:
    """Estimate the prompt size of a list of chat messages (role overhead included)."""
    total = 0
    for c in contents:
        total += count_tokens(c, model=model) + _MESSAGE_OVERHEAD_TOKENS
    return total


# USD per 1K tokens: (input, output). Overridable / extendable via MODEL_PRICING in .env.
_DEFAULT_PRICING_USD_PER_1K: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4.1": (0.002, 0.008),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1-nano": (0.0001, 0.0004),
    "o3-mini": (0.0011, 0.0044),
    "text-embedding-3-small": (0.00002, 0.0),
    "text-embedding-3-large": (0.00013, 0.0),
}


@lru_cache
def pricing_table() -> dict[str, tuple[float, float]]:
    table = dict(_DEFAULT_PRICING_USD_PER_1K)
    for model, rates in (settings.MODEL_PRICING or {}).items():
        try:
            table[model] = (float(rates[0]), float(rates[1]) if len(rates) > 1 else 0.0)
        except (TypeError, ValueError, IndexError):
            logger.warning("invalid_model_pricing_entry", model=model)
    return table


def lookup_pricing(model: str) -> tuple[float, float] | None:
    """Exact match first, then the longest key that prefixes the model name
    (so `gpt-4o-mini-2024-07-18` resolves to `gpt-4o-mini`)."""
    table = pricing_table()
    if model in table:
        return table[model]
    best: str | None = None
    for key in table:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return table[best] if best else None


def estimate_cost(
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int = 0,
) -> float:
    rates = lookup_pricing(model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    return round((prompt_tokens / 1000) * in_rate + (completion_tokens / 1000) * out_rate, 8)
