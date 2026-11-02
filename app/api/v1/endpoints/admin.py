from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import server_admin
from app.db.session import get_db
from app.models.user import User
from app.schemas.usage import FeedbackListResponse, FeedbackRead, UsageReport
from app.services import feedback_service, quota_service

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(server_admin)])


@router.get("/usage", response_model=UsageReport)
async def platform_usage(
    group_by: str = Query(default="org", pattern="^(org|user|model|day)$"),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide usage for server administrators (`users.role == "admin"`)."""
    period = quota_service.month_bounds()
    return await quota_service.report(
        db, start=start or period.start, end=end or period.end, group_by=group_by
    )


@router.get("/feedback", response_model=FeedbackListResponse)
async def platform_feedback(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(server_admin),
):
    rows, total = await feedback_service.list_feedback(db, org_id=None, page=page, page_size=page_size)
    return FeedbackListResponse(
        items=[
            FeedbackRead(
                message_id=r.message_id,
                session_id=r.session_id,
                user_id=r.user_id,
                rating=r.rating,
                comment=r.comment,
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
