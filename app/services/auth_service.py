from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    ACCESS,
    REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import ApiKey, User
from app.schemas.auth import RegisterRequest, TokenPair


def _api_key_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _token_pair(user_id: UUID) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def register_user(db: AsyncSession, payload: RegisterRequest) -> User:
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("Email already registered")
    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add(user)
    await db.flush()
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> TokenPair:
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.hashed_password):
        raise UnauthorizedError("Invalid credentials")
    user.last_login_at = datetime.now(timezone.utc)
    await db.flush()
    return _token_pair(user.id)


async def refresh(db: AsyncSession, refresh_token: str) -> TokenPair:
    payload = decode_token(refresh_token, expected_type=REFRESH)
    user_id = UUID(payload["sub"])
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User no longer active")
    return _token_pair(user_id)


async def get_user_by_token(db: AsyncSession, token: str) -> User:
    payload = decode_token(token, expected_type=ACCESS)
    user_id = UUID(payload["sub"])
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User not found or inactive")
    return user


async def get_user_by_api_key(db: AsyncSession, raw_key: str) -> User:
    h = _api_key_hash(raw_key)
    stmt = select(ApiKey).where(ApiKey.key_hash == h)
    key = (await db.execute(stmt)).scalar_one_or_none()
    if key is None or key.revoked_at is not None:
        raise UnauthorizedError("Invalid API key")
    key.last_used_at = datetime.now(timezone.utc)
    await db.flush()
    user = await db.get(User, key.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User inactive")
    return user


async def create_api_key(db: AsyncSession, *, user_id: UUID, name: str) -> tuple[ApiKey, str]:
    plain = f"sk_live_{secrets.token_urlsafe(32)}"
    key = ApiKey(user_id=user_id, name=name, key_hash=_api_key_hash(plain))
    db.add(key)
    await db.flush()
    return key, plain


async def revoke_api_key(db: AsyncSession, *, user_id: UUID, key_id: UUID) -> None:
    key = await db.get(ApiKey, key_id)
    if key is None or key.user_id != user_id:
        raise UnauthorizedError("API key not found")
    key.revoked_at = datetime.now(timezone.utc)
    await db.flush()
