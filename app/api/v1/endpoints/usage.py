from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import Principal, current_principal
from app.db.session import get_db
from app.schemas.usage import UsageMeResponse
from app.services import quota_service

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/me", response_model=UsageMeResponse)
async def my_usage(principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    """Month-to-date usage for the caller and the active organization, with budgets."""
    period = quota_service.month_bounds()
    user_bucket = await quota_service.usage_bucket(
        db, start=period.start, end=period.end, org_id=principal.org_id, user_id=principal.user_id
    )
    org = await quota_service.org_usage(db, principal.org, period)
    return UsageMeResponse(period=period, user=user_bucket, org=org)
