from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import current_user
from app.core.rate_limit import client_ip, ip_rate_limit_dependency, rate_limit_dependency
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ChangePasswordRequest,
    DeleteAccountRequest,
    LoginRequest,
    LogoutRequest,
    ProfileUpdateRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserRead,
)
from app.schemas.common import OkResponse
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_meta(request: Request) -> dict:
    return {
        "ip": client_ip(request),
        "user_agent": request.headers.get("user-agent", "")[:200],
    }


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ip_rate_limit_dependency("auth_register"))],
)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    user = await auth_service.register_user(db, payload)
    await db.commit()
    return user


@router.post(
    "/login",
    response_model=TokenPair,
    dependencies=[Depends(ip_rate_limit_dependency("auth_login"))],
)
async def login(payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    tokens = await auth_service.authenticate(
        db, payload.email, payload.password, client_meta=_client_meta(request)
    )
    await db.commit()
    return tokens


@router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[Depends(ip_rate_limit_dependency("auth_refresh", requests=60))],
)
async def refresh(payload: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        tokens = await auth_service.refresh(db, payload.refresh_token, client_meta=_client_meta(request))
    except Exception:
        # Reuse detection revokes the whole family; that write must survive the 401.
        await db.commit()
        raise
    await db.commit()
    return tokens


@router.post("/logout", response_model=OkResponse)
async def logout(payload: LogoutRequest, db: AsyncSession = Depends(get_db)):
    """Revoke one refresh token (sign out this device). Always succeeds."""
    await auth_service.revoke_refresh_token(db, payload.refresh_token)
    await db.commit()
    return OkResponse(message="Signed out")


@router.post("/logout-all", response_model=OkResponse)
async def logout_all(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    count = await auth_service.revoke_all_refresh_tokens(db, user_id=user.id)
    await db.commit()
    return OkResponse(message=f"Signed out of {count} session(s)")


@router.get("/me", response_model=UserRead)
async def me(user: User = Depends(current_user)):
    return user


@router.patch("/me", response_model=UserRead)
async def update_me(
    payload: ProfileUpdateRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    kwargs: dict = {"full_name": payload.full_name}
    if "default_model" in payload.model_fields_set:
        kwargs["default_model"] = payload.default_model
    user = await auth_service.update_profile(db, user=user, **kwargs)
    await db.commit()
    return user


@router.post(
    "/change-password",
    response_model=TokenPair,
    dependencies=[Depends(rate_limit_dependency("auth_change_password", requests=10))],
)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    tokens = await auth_service.change_password(
        db,
        user=user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        client_meta=_client_meta(request),
    )
    await db.commit()
    return tokens


@router.delete(
    "/me",
    response_model=OkResponse,
    dependencies=[Depends(rate_limit_dependency("auth_delete_account", requests=5))],
)
async def delete_me(
    payload: DeleteAccountRequest,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete the account (required for app-store compliance). Password confirmation required."""
    await auth_service.delete_account(db, user=user, password=payload.password)
    await db.commit()
    return OkResponse(message="Account deleted")


@router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    payload: ApiKeyCreate,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    key, plain = await auth_service.create_api_key(db, user_id=user.id, name=payload.name)
    await db.commit()
    return ApiKeyCreateResponse(
        id=key.id,
        name=key.name,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        plaintext_key=plain,
    )


@router.delete("/api-keys/{key_id}", response_model=OkResponse)
async def revoke_api_key(
    key_id: UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    await auth_service.revoke_api_key(db, user_id=user.id, key_id=key_id)
    await db.commit()
    return OkResponse(message="API key revoked")
