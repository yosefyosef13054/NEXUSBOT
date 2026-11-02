from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class UsageBucket(BaseModel):
    tokens: int = 0
    cost_usd: float = 0.0
    messages: int = 0


class UsagePeriod(BaseModel):
    start: datetime
    end: datetime


class OrgUsage(UsageBucket):
    id: UUID
    budget_tokens_per_month: int | None = None
    budget_usd_per_month: float | None = None
    remaining_tokens: int | None = None
    remaining_usd: float | None = None


class UsageMeResponse(BaseModel):
    period: UsagePeriod
    user: UsageBucket
    org: OrgUsage


class UsageGroup(UsageBucket):
    key: str


class UsageReport(BaseModel):
    period: UsagePeriod
    group_by: Literal["user", "model", "day", "org"]
    totals: UsageBucket
    groups: list[UsageGroup] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackRead(BaseModel):
    message_id: UUID
    session_id: UUID
    user_id: UUID
    rating: int
    comment: str | None = None
    created_at: datetime


class FeedbackListResponse(BaseModel):
    items: list[FeedbackRead]
    total: int
    page: int
    page_size: int
