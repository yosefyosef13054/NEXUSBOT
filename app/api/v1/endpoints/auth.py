from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyRead,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserRead,
)
from app.schemas.common import OkResponse
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)):
    user = await auth_service.register_user(db, payload)
    await db.commit()
    return user


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    tokens = await auth_service.authenticate(db, payload.email, payload.password)
    await db.commit()
    return tokens


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    tokens = await auth_service.refresh(db, payload.refresh_token)
    await db.commit()
    return tokens


@router.get("/me", response_model=UserRead)
async def me(user: User = Depends(current_user)):
    return user


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
