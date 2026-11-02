from __future__ import annotations

from typing import Annotated, Optional

from fastapi import Depends, Header, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.db.session import get_db
from app.models.user import User
from app.services.auth_service import get_user_by_api_key, get_user_by_token

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_PREFIX}/auth/login",
    auto_error=False,
)


async def current_user(
    request: Request,
    token: Annotated[Optional[str], Depends(oauth2_scheme)] = None,
    api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Accepts either a Bearer JWT or an `X-API-Key` header.

    Sets `request.state.user_id` so rate limiting + audit middlewares can read it.
    """
    if token:
        user = await get_user_by_token(db, token)
    elif api_key:
        user = await get_user_by_api_key(db, api_key)
    else:
        raise UnauthorizedError("Missing credentials")
    request.state.user_id = str(user.id)
    return user


def require_role(*roles: str):
    async def _checker(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise ForbiddenError("Insufficient permissions")
        return user

    return _checker


# --------------------------------------------------------------------------- workspaces


from dataclasses import dataclass  # noqa: E402

from app.models.organization import Membership, Organization, OrgRole  # noqa: E402


@dataclass
class Principal:
    """The authenticated user acting inside one organization."""

    user: User
    org: Organization
    membership: Membership

    @property
    def user_id(self):
        return self.user.id

    @property
    def org_id(self):
        return self.org.id

    @property
    def role(self) -> OrgRole:
        return self.membership.role


async def current_principal(
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """Resolve the active organization from `X-Org-Id` (default: the personal workspace)."""
    from app.services.org_service import resolve_active_org

    org, membership = await resolve_active_org(db, user, request.headers.get(settings.ORG_HEADER))
    request.state.org_id = str(org.id)
    return Principal(user=user, org=org, membership=membership)


def require_org_role(minimum: OrgRole):
    async def _checker(principal: Principal = Depends(current_principal)) -> Principal:
        from app.services.org_service import require_role

        require_role(principal.membership, minimum)
        return principal

    return _checker


async def server_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise ForbiddenError("Server administrator role required")
    return user
