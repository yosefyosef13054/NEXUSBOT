from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import current_user
from app.core.exceptions import ForbiddenError
from app.db.session import get_db
from app.models.organization import Membership, Organization, OrgRole
from app.models.user import User
from app.schemas.common import OkResponse
from app.schemas.org import (
    MemberAddRequest,
    MemberListResponse,
    MemberRead,
    MemberUpdateRequest,
    OrgCreateRequest,
    OrgListResponse,
    OrgRead,
    OrgUpdateRequest,
)
from app.schemas.usage import UsageReport
from app.services import org_service, quota_service

router = APIRouter(prefix="/orgs", tags=["organizations"])


def _org_read(org: Organization, membership: Membership, member_count: int) -> OrgRead:
    return OrgRead(
        id=org.id,
        name=org.name,
        slug=org.slug,
        role=membership.role.value,  # type: ignore[arg-type]
        is_personal=org.is_personal,
        plan=org.plan,
        member_count=member_count,
        settings=org_service.org_settings(org),
        created_at=org.created_at,
    )


async def _member_of(db: AsyncSession, org_id: UUID, user: User, minimum: OrgRole = OrgRole.MEMBER):
    org = await org_service.get_org(db, org_id)
    membership = await org_service.get_membership(db, org_id=org.id, user_id=user.id)
    if membership is None:
        raise ForbiddenError("You are not a member of this organization")
    org_service.require_role(membership, minimum)
    return org, membership


@router.get("", response_model=OrgListResponse)
async def list_orgs(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await org_service.ensure_personal_org(db, user)
    await db.commit()
    pairs = await org_service.list_user_orgs(db, user.id)
    counts = await org_service.member_counts(db, [org.id for org, _ in pairs])
    return OrgListResponse(items=[_org_read(org, m, counts.get(org.id, 1)) for org, m in pairs])


@router.post("", response_model=OrgRead, status_code=status.HTTP_201_CREATED)
async def create_org(
    payload: OrgCreateRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    org = await org_service.create_org(db, owner=user, name=payload.name)
    await db.commit()
    await db.refresh(org)
    membership = await org_service.get_membership(db, org_id=org.id, user_id=user.id)
    assert membership is not None
    return _org_read(org, membership, 1)


@router.get("/{org_id}", response_model=OrgRead)
async def get_org(org_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    org, membership = await _member_of(db, org_id, user)
    counts = await org_service.member_counts(db, [org.id])
    return _org_read(org, membership, counts.get(org.id, 1))


@router.patch("/{org_id}", response_model=OrgRead)
async def update_org(
    org_id: UUID,
    payload: OrgUpdateRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    org, membership = await _member_of(db, org_id, user, OrgRole.ADMIN)
    org = await org_service.update_org(db, org=org, name=payload.name, settings_update=payload.settings)
    await db.commit()
    await quota_service.invalidate_quota_cache(org.id)
    counts = await org_service.member_counts(db, [org.id])
    return _org_read(org, membership, counts.get(org.id, 1))


@router.get("/{org_id}/members", response_model=MemberListResponse)
async def list_members(org_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    org, _ = await _member_of(db, org_id, user)
    rows = await org_service.list_members(db, org.id)
    return MemberListResponse(
        items=[
            MemberRead(
                user_id=u.id, email=u.email, full_name=u.full_name, role=m.role.value, joined_at=m.joined_at
            )  # type: ignore[arg-type]
            for m, u in rows
        ]
    )


@router.post("/{org_id}/members", response_model=MemberRead, status_code=status.HTTP_201_CREATED)
async def add_member(
    org_id: UUID,
    payload: MemberAddRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    org, _ = await _member_of(db, org_id, user, OrgRole.ADMIN)
    membership, member = await org_service.add_member(db, org=org, email=payload.email, role=payload.role)
    await db.commit()
    await db.refresh(membership)
    return MemberRead(
        user_id=member.id,
        email=member.email,
        full_name=member.full_name,
        role=membership.role.value,
        joined_at=membership.joined_at,  # type: ignore[arg-type]
    )


@router.patch("/{org_id}/members/{user_id}", response_model=MemberRead)
async def update_member(
    org_id: UUID,
    user_id: UUID,
    payload: MemberUpdateRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    org, _ = await _member_of(db, org_id, user, OrgRole.ADMIN)
    membership = await org_service.update_member_role(db, org=org, user_id=user_id, role=payload.role)
    await db.commit()
    member = await db.get(User, user_id)
    assert member is not None
    return MemberRead(
        user_id=member.id,
        email=member.email,
        full_name=member.full_name,
        role=membership.role.value,
        joined_at=membership.joined_at,  # type: ignore[arg-type]
    )


@router.delete("/{org_id}/members/{user_id}", response_model=OkResponse)
async def remove_member(
    org_id: UUID, user_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    org, actor = await _member_of(db, org_id, user)
    await org_service.remove_member(db, org=org, user_id=user_id, actor=actor)
    await db.commit()
    return OkResponse(message="Member removed")


@router.get("/{org_id}/usage", response_model=UsageReport)
async def org_usage_report(
    org_id: UUID,
    group_by: str = Query(default="user", pattern="^(user|model|day)$"),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    org, _ = await _member_of(db, org_id, user, OrgRole.ADMIN)
    period = quota_service.month_bounds()
    return await quota_service.report(
        db, start=start or period.start, end=end or period.end, group_by=group_by, org_id=org.id
    )
