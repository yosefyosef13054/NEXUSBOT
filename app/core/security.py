from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from passlib.context import CryptContext

from app.config import settings
from app.core.exceptions import UnauthorizedError

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ACCESS = "access"
REFRESH = "refresh"


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def new_jti() -> str:
    return secrets.token_urlsafe(24)


def create_access_token(user_id: UUID, *, extra: dict | None = None) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": ACCESS,
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    if extra:
        payload.update(extra)
    return _encode(payload)


def create_refresh_token(user_id: UUID, *, jti: str, family_id: str) -> tuple[str, datetime]:
    """Returns the signed refresh token and its expiry (persisted alongside the `jti`)."""
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "type": REFRESH,
        "jti": jti,
        "fam": family_id,
        "iat": now,
        "exp": expires_at,
    }
    return _encode(payload), expires_at


def decode_token(token: str, *, expected_type: str | None = None) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError as e:
        raise UnauthorizedError("Token expired") from e
    except jwt.InvalidTokenError as e:
        raise UnauthorizedError("Invalid token") from e

    if expected_type and payload.get("type") != expected_type:
        raise UnauthorizedError("Wrong token type")
    return payload
