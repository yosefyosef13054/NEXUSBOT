from __future__ import annotations

import re
import secrets
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.organization import ROLE_RANK, Membership, Organization, OrgRole
from app.models.user import User
from app.schemas.org import OrgSettings

_SLUG = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    base = _SLUG.sub("-", name.lower()).strip("-")[:40] or "org"
    return f"{base}-{secrets.token_hex(3)}"


def org_settings(org: Organization) -> OrgSettings:
    return OrgSettings.model_validate(org.settings or {})


def has_role(membership: Membership | None, minimum: OrgRole) -> bool:
    return membership is not None and ROLE_RANK[membership.role] >= ROLE_RANK[minimum]


def require_role(membership: Membership | None, minimum: OrgRole) -> Membership:
    if not has_role(membership, minimum):
        raise ForbiddenError(f"Requires the {minimum.value} role in this organization")
    assert membership is not None
    return membership


async def personal_org(db: AsyncSession, user_id: UUID) -> Organization | None:
    stmt = select(Organization).where(
        Organization.owner_id == user_id,
        Organization.is_personal.is_(True),
        Organization.deleted_at.is_(None),
    )
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


async def ensure_personal_org(db: AsyncSession, user: User) -> Organization:
    """Every user owns a personal workspace; created on registration (and lazily for
    accounts that predate workspaces)."""
    existing = await personal_org(db, user.id)
    if existing is not None:
        return existing
    org = Organization(
        name="Personal",
        slug=f"personal-{user.id.hex[:12]}",
        owner_id=user.id,
        is_personal=True,
        plan="free",
        settings={},
    )
    db.add(org)
    await db.flush()
    db.add(Membership(org_id=org.id, user_id=user.id, role=OrgRole.OWNER))
    await db.flush()
    return org


async def create_org(db: AsyncSession, *, owner: User, name: str) -> Organization:
    org = Organization(
        name=name.strip(), slug=_slugify(name), owner_id=owner.id, is_personal=False, settings={}
    )
    db.add(org)
    await db.flush()
    db.add(Membership(org_id=org.id, user_id=owner.id, role=OrgRole.OWNER))
    await db.flush()
    return org


async def get_org(db: AsyncSession, org_id: UUID) -> Organization:
    org = await db.get(Organization, org_id)
    if org is None or org.deleted_at is not None:
        raise NotFoundError("Organization not found")
    return org


async def get_membership(db: AsyncSession, *, org_id: UUID, user_id: UUID) -> Membership | None:
    stmt = select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def member_counts(db: AsyncSession, org_ids: list[UUID]) -> dict[UUID, int]:
    if not org_ids:
        return {}
    rows = (
        await db.execute(
            select(Membership.org_id, func.count())
            .where(Membership.org_id.in_(org_ids))
            .group_by(Membership.org_id)
        )
    ).all()
    return {oid: int(n) for oid, n in rows}


async def list_user_orgs(db: AsyncSession, user_id: UUID) -> list[tuple[Organization, Membership]]:
    stmt = (
        select(Organization, Membership)
        .join(Membership, Membership.org_id == Organization.id)
        .where(Membership.user_id == user_id, Organization.deleted_at.is_(None))
        .order_by(Organization.is_personal.desc(), Organization.created_at.asc())
    )
    return [(org, m) for org, m in (await db.execute(stmt)).all()]


async def resolve_active_org(
    db: AsyncSession, user: User, header_value: str | None
) -> tuple[Organization, Membership]:
    """The org a request acts in: the `X-Org-Id` header, else the user's personal org."""
    if header_value:
        try:
            org_id = UUID(header_value.strip())
        except ValueError:
            raise ValidationError("Invalid organization id header")
        org = await get_org(db, org_id)
        membership = await get_membership(db, org_id=org.id, user_id=user.id)
        if membership is None:
            raise ForbiddenError("You are not a member of this organization")
        return org, membership
    org = await ensure_personal_org(db, user)
    membership = await get_membership(db, org_id=org.id, user_id=user.id)
    assert membership is not None
    return org, membership


async def update_org(
    db: AsyncSession, *, org: Organization, name: str | None, settings_update: OrgSettings | None
) -> Organization:
    if name is not None:
        org.name = name.strip()
    if settings_update is not None:
        merged = {**(org.settings or {}), **settings_update.model_dump(exclude_unset=True)}
        org.settings = OrgSettings.model_validate(merged).model_dump()
    await db.flush()
    await db.refresh(org)
    return org


async def list_members(db: AsyncSession, org_id: UUID) -> list[tuple[Membership, User]]:
    stmt = (
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.org_id == org_id)
        .order_by(Membership.joined_at.asc())
    )
    return [(m, u) for m, u in (await db.execute(stmt)).all()]


async def add_member(
    db: AsyncSession, *, org: Organization, email: str, role: str
) -> tuple[Membership, User]:
    if org.is_personal:
        raise ValidationError("Personal workspaces cannot have other members; create an organization")
    user = (await db.execute(select(User).where(User.email == email.strip().lower()))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise NotFoundError("No account with that e-mail")
    if await get_membership(db, org_id=org.id, user_id=user.id) is not None:
        raise ConflictError("Already a member")
    membership = Membership(org_id=org.id, user_id=user.id, role=OrgRole(role))
    db.add(membership)
    await db.flush()
    return membership, user


async def update_member_role(db: AsyncSession, *, org: Organization, user_id: UUID, role: str) -> Membership:
    membership = await get_membership(db, org_id=org.id, user_id=user_id)
    if membership is None:
        raise NotFoundError("Member not found")
    if membership.role == OrgRole.OWNER:
        raise ValidationError("The owner's role cannot be changed")
    membership.role = OrgRole(role)
    await db.flush()
    return membership


async def remove_member(db: AsyncSession, *, org: Organization, user_id: UUID, actor: Membership) -> None:
    membership = await get_membership(db, org_id=org.id, user_id=user_id)
    if membership is None:
        raise NotFoundError("Member not found")
    if membership.role == OrgRole.OWNER:
        raise ValidationError("The owner cannot be removed")
    if actor.user_id != user_id:
        require_role(actor, OrgRole.ADMIN)
    await db.delete(membership)
    await db.flush()


def allowed_model(org: Organization, model_id: str | None) -> bool:
    allowed = org_settings(org).allowed_models
    return not allowed or not model_id or model_id in allowed
