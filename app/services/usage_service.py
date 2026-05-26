from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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
    meta: dict | None = None,
) -> AIUsageLog:
    model_name = model or settings.OPENAI_CHAT_MODEL
    total = prompt_tokens + completion_tokens
    row = AIUsageLog(
        user_id=user_id,
        session_id=session_id,
        provider="openai",
        model=model_name,
        operation=operation,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
        cost_usd=estimate_cost(
            model_name, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
        latency_ms=latency_ms,
        meta=meta or {},
    )
    db.add(row)
    await db.flush()
    return row
