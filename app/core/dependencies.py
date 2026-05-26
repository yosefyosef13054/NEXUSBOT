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
