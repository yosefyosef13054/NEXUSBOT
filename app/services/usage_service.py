from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import chat_model_name
from app.core.metrics import LLM_COST_USD, LLM_TOKENS
from app.models.usage import AIUsageLog
from app.utils.tokens import estimate_cost


async def log_ai_usage(
    db: AsyncSession,
    *,
    user_id: UUID | None,
    session_id: UUID | None,
    operation: str,
    model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
    provider: str = "openai",
    meta: dict | None = None,
) -> AIUsageLog:
    """Record one model call (chat, agent, embedding) with an estimated cost.

    Token counts should come from the provider's usage report whenever available; the
    caller falls back to local estimates otherwise (see `app.ai.orchestrator`).
    """
    model_name = model or chat_model_name()
    total = prompt_tokens + completion_tokens
    cost = estimate_cost(model_name, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    row = AIUsageLog(
        user_id=user_id,
        session_id=session_id,
        provider=provider,
        model=model_name,
        operation=operation,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
        cost_usd=cost,
        latency_ms=latency_ms,
        meta=meta or {},
    )
    db.add(row)
    await db.flush()

    kind = "embedding" if operation == "embedding" else "prompt"
    if prompt_tokens:
        LLM_TOKENS.labels(model_name, kind).inc(prompt_tokens)
    if completion_tokens:
        LLM_TOKENS.labels(model_name, "completion").inc(completion_tokens)
    if cost:
        LLM_COST_USD.labels(model_name).inc(cost)
    return row
