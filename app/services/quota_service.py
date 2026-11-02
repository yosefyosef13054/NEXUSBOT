"""Usage aggregation, monthly budgets and admin reports built on `ai_usage_logs`."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import get_redis
from app.config import settings
from app.core.exceptions import QuotaExceededError
from app.models.organization import Organization
from app.models.usage import AIUsageLog
from app.models.user import User
from app.schemas.usage import OrgUsage, UsageBucket, UsageGroup, UsagePeriod, UsageReport
from app.services.org_service import org_settings

CHAT_OPERATIONS = ("chat", "chat_stream", "chat_regenerate")


def month_bounds(now: datetime | None = None) -> UsagePeriod:
    now = now or datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return UsagePeriod(start=start, end=end)


def _messages_expr():
    return func.count().filter(AIUsageLog.operation.in_(CHAT_OPERATIONS))


async def usage_bucket(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    org_id: UUID | None = None,
    user_id: UUID | None = None,
) -> UsageBucket:
    stmt = select(
        func.coalesce(func.sum(AIUsageLog.total_tokens), 0),
        func.coalesce(func.sum(AIUsageLog.cost_usd), 0),
        _messages_expr(),
    ).where(AIUsageLog.created_at >= start, AIUsageLog.created_at < end)
    if org_id is not None:
        stmt = stmt.where(AIUsageLog.org_id == org_id)
    if user_id is not None:
        stmt = stmt.where(AIUsageLog.user_id == user_id)
    tokens, cost, messages = (await db.execute(stmt)).one()
    return UsageBucket(tokens=int(tokens or 0), cost_usd=float(cost or 0), messages=int(messages or 0))


def _org_usage(org: Organization, bucket: UsageBucket) -> OrgUsage:
    cfg = org_settings(org)
    return OrgUsage(
        id=org.id,
        tokens=bucket.tokens,
        cost_usd=round(bucket.cost_usd, 6),
        messages=bucket.messages,
        budget_tokens_per_month=cfg.budget_tokens_per_month,
        budget_usd_per_month=cfg.budget_usd_per_month,
        remaining_tokens=(
            max(cfg.budget_tokens_per_month - bucket.tokens, 0)
            if cfg.budget_tokens_per_month is not None
            else None
        ),
        remaining_usd=(
            round(max(cfg.budget_usd_per_month - bucket.cost_usd, 0.0), 6)
            if cfg.budget_usd_per_month is not None
            else None
        ),
    )


async def org_usage(db: AsyncSession, org: Organization, period: UsagePeriod | None = None) -> OrgUsage:
    period = period or month_bounds()
    bucket = await usage_bucket(db, start=period.start, end=period.end, org_id=org.id)
    return _org_usage(org, bucket)


def _cache_key(org_id: UUID) -> str:
    return f"quota:{org_id}"


async def invalidate_quota_cache(org_id: UUID) -> None:
    await get_redis().delete(_cache_key(org_id))


async def note_usage(org_id: UUID | None, *, tokens: int, cost_usd: float, is_message: bool = True) -> None:
    """Fold a just-logged model call into the cached month-to-date bucket (if cached), so a
    budget trips on the very next turn instead of after the cache expires."""
    if org_id is None:
        return
    r = get_redis()
    key = _cache_key(org_id)
    cached = await r.get(key)
    if not cached:
        return
    data = json.loads(cached)
    data["tokens"] = int(data.get("tokens", 0)) + int(tokens)
    data["cost_usd"] = float(data.get("cost_usd", 0.0)) + float(cost_usd)
    if is_message:
        data["messages"] = int(data.get("messages", 0)) + 1
    ttl = await r.ttl(key)
    await r.set(key, json.dumps(data), ex=ttl if ttl and ttl > 0 else settings.QUOTA_CACHE_SECONDS)


async def check_quota(db: AsyncSession, org: Organization) -> None:
    """Raise `QuotaExceededError` (429) when the org's monthly budget is spent.

    Month-to-date usage is cached briefly so a chatty client does not scan the usage table
    on every turn; the cache is invalidated when budgets change.
    """
    cfg = org_settings(org)
    if cfg.budget_tokens_per_month is None and cfg.budget_usd_per_month is None:
        return
    period = month_bounds()
    r = get_redis()
    cached = await r.get(_cache_key(org.id))
    if cached:
        data = json.loads(cached)
        bucket = UsageBucket(**data)
    else:
        bucket = await usage_bucket(db, start=period.start, end=period.end, org_id=org.id)
        await r.set(_cache_key(org.id), json.dumps(bucket.model_dump()), ex=settings.QUOTA_CACHE_SECONDS)
    details: dict[str, Any] = {"resets_at": period.end.isoformat()}
    if cfg.budget_usd_per_month is not None and bucket.cost_usd >= cfg.budget_usd_per_month:
        details.update(
            {"budget_usd_per_month": cfg.budget_usd_per_month, "used_usd": round(bucket.cost_usd, 6)}
        )
        raise QuotaExceededError("Monthly spending budget exhausted for this organization", details=details)
    if cfg.budget_tokens_per_month is not None and bucket.tokens >= cfg.budget_tokens_per_month:
        details.update({"budget_tokens_per_month": cfg.budget_tokens_per_month, "used_tokens": bucket.tokens})
        raise QuotaExceededError("Monthly token budget exhausted for this organization", details=details)


async def report(
    db: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    group_by: str,
    org_id: UUID | None = None,
    user_id: UUID | None = None,
) -> UsageReport:
    key: Any
    joins: list[tuple[Any, Any]]
    if group_by == "user":
        key = func.coalesce(User.email, "unknown")
        joins = [(User, User.id == AIUsageLog.user_id)]
    elif group_by == "model":
        key = AIUsageLog.model
        joins = []
    elif group_by == "day":
        key = func.to_char(AIUsageLog.created_at, "YYYY-MM-DD")
        joins = []
    elif group_by == "org":
        key = func.coalesce(Organization.name, "unknown")
        joins = [(Organization, Organization.id == AIUsageLog.org_id)]
    else:
        raise ValueError(f"unsupported group_by {group_by}")

    stmt = select(
        key.label("key"),
        func.coalesce(func.sum(AIUsageLog.total_tokens), 0),
        func.coalesce(func.sum(AIUsageLog.cost_usd), 0),
        _messages_expr(),
    ).where(AIUsageLog.created_at >= start, AIUsageLog.created_at < end)
    for model, cond in joins:
        stmt = stmt.join(model, cond, isouter=True)
    if org_id is not None:
        stmt = stmt.where(AIUsageLog.org_id == org_id)
    if user_id is not None:
        stmt = stmt.where(AIUsageLog.user_id == user_id)
    stmt = stmt.group_by(key).order_by(func.sum(AIUsageLog.cost_usd).desc())
    rows = (await db.execute(stmt)).all()
    groups = [
        UsageGroup(key=str(k), tokens=int(t or 0), cost_usd=round(float(c or 0), 6), messages=int(m or 0))
        for k, t, c, m in rows
    ]
    totals = await usage_bucket(db, start=start, end=end, org_id=org_id, user_id=user_id)
    return UsageReport(
        period=UsagePeriod(start=start, end=end),
        group_by=group_by,  # type: ignore[arg-type]
        totals=totals,
        groups=groups,
    )
