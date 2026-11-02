from __future__ import annotations

import hashlib
import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.background import run_in_background
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.logger import get_logger
from app.core.security import (
    ACCESS,
    REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    new_jti,
    verify_password,
)
from app.models.chat import ChatSession
from app.models.document import Document
from app.models.user import ApiKey, RefreshToken, User
from app.schemas.auth import RegisterRequest, TokenPair

logger = get_logger(__name__)


def _api_key_hash(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def _issue_token_pair(
    db: AsyncSession,
    user_id: UUID,
    *,
    family_id: str | None = None,
    client_meta: dict | None = None,
) -> tuple[TokenPair, str]:
    """Mint an access + refresh pair and persist the refresh token's `jti`."""
    jti = new_jti()
    family = family_id or jti
    refresh_jwt, expires_at = create_refresh_token(user_id, jti=jti, family_id=family)
    db.add(
        RefreshToken(
            user_id=user_id, jti=jti, family_id=family, expires_at=expires_at, meta=client_meta or {}
        )
    )
    await db.flush()
    pair = TokenPair(
        access_token=create_access_token(user_id),
        refresh_token=refresh_jwt,
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return pair, jti


async def register_user(db: AsyncSession, payload: RegisterRequest) -> User:
    email = normalize_email(payload.email)
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("Email already registered")
    user = User(
        email=email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add(user)
    await db.flush()
    return user


async def authenticate(
    db: AsyncSession, email: str, password: str, *, client_meta: dict | None = None
) -> TokenPair:
    user = (await db.execute(select(User).where(User.email == normalize_email(email)))).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.hashed_password):
        raise UnauthorizedError("Invalid credentials")
    user.last_login_at = _now()
    await db.flush()
    pair, _ = await _issue_token_pair(db, user.id, client_meta=client_meta)
    return pair


async def _revoke_family(db: AsyncSession, family_id: str) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


async def refresh(db: AsyncSession, refresh_token: str, *, client_meta: dict | None = None) -> TokenPair:
    """Rotate a refresh token.

    - Unknown / revoked token → 401.
    - A rotated token presented again within `REFRESH_TOKEN_REUSE_GRACE_SECONDS` is a
      client retry: the orphaned replacement is revoked and a new pair is issued.
    - A rotated token presented again later means it leaked: the whole family is revoked
      and the user must log in again.
    """
    payload = decode_token(refresh_token, expected_type=REFRESH)
    jti = payload.get("jti")
    if not jti:
        raise UnauthorizedError("Refresh token is from an older version; please sign in again")

    row = (await db.execute(select(RefreshToken).where(RefreshToken.jti == jti))).scalar_one_or_none()
    if row is None:
        raise UnauthorizedError("Unknown refresh token")

    now = _now()
    if row.revoked_at is not None:
        grace = timedelta(seconds=settings.REFRESH_TOKEN_REUSE_GRACE_SECONDS)
        if row.replaced_by_jti and now - row.revoked_at <= grace:
            logger.info("refresh_token_retry_within_grace", user_id=str(row.user_id))
            await db.execute(
                update(RefreshToken)
                .where(RefreshToken.jti == row.replaced_by_jti, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
        else:
            logger.warning("refresh_token_reuse_detected", user_id=str(row.user_id), family=row.family_id)
            await _revoke_family(db, row.family_id)
            raise UnauthorizedError("Refresh token reuse detected; please sign in again")
    elif row.expires_at <= now:
        row.revoked_at = now
        raise UnauthorizedError("Refresh token expired")

    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("User no longer active")

    pair, new_id = await _issue_token_pair(db, user.id, family_id=row.family_id, client_meta=client_meta)
    row.revoked_at = row.revoked_at or now
    row.replaced_by_jti = new_id
    await db.flush()
    return pair


async def revoke_refresh_token(db: AsyncSession, refresh_token: str) -> bool:
    """Logout for one device. Returns False when the token is already unusable."""
    try:
        payload = decode_token(refresh_token, expected_type=REFRESH)
    except UnauthorizedError:
        return False
    jti = payload.get("jti")
    if not jti:
        return False
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    return bool(result.rowcount)


async def revoke_all_refresh_tokens(db: AsyncSession, *, user_id: UUID) -> int:
    result = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    return int(result.rowcount or 0)


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
    key.last_used_at = _now()
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
    key.revoked_at = _now()
    await db.flush()


_UNSET: object = object()


async def update_profile(
    db: AsyncSession,
    *,
    user: User,
    full_name: str | None = None,
    default_model: str | None | object = _UNSET,
) -> User:
    if full_name is not None:
        user.full_name = full_name.strip() or None
    if default_model is not _UNSET:
        from app.ai.providers.registry import get_registry

        user.default_model = get_registry().validate(
            default_model if isinstance(default_model, str) else None
        )
    await db.flush()
    return user


async def change_password(
    db: AsyncSession,
    *,
    user: User,
    current_password: str,
    new_password: str,
    client_meta: dict | None = None,
) -> TokenPair:
    """Rotate the password and sign every other device out."""
    if not verify_password(current_password, user.hashed_password):
        raise UnauthorizedError("Current password is incorrect")
    user.hashed_password = hash_password(new_password)
    await revoke_all_refresh_tokens(db, user_id=user.id)
    pair, _ = await _issue_token_pair(db, user.id, client_meta=client_meta)
    return pair


async def _remove_user_uploads(user_id: UUID) -> None:
    await _rmtree(Path(settings.UPLOAD_DIR) / str(user_id))


async def _rmtree(path: Path) -> None:
    import asyncio

    await asyncio.to_thread(shutil.rmtree, path, True)


async def delete_account(db: AsyncSession, *, user: User, password: str) -> None:
    """Deactivate the account and make its data unreachable.

    The row is kept (soft-delete) so usage logs and audit trails stay consistent, but the
    email is anonymized so the address can register again. Uploaded files are removed in
    the background.
    """
    if not verify_password(password, user.hashed_password):
        raise UnauthorizedError("Password is incorrect")
    now = _now()
    user.is_active = False
    user.deleted_at = now
    user.email = f"deleted-{user.id}@deleted.invalid"
    user.full_name = None
    await revoke_all_refresh_tokens(db, user_id=user.id)
    await db.execute(
        update(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None)).values(revoked_at=now)
    )
    await db.execute(
        update(ChatSession)
        .where(ChatSession.user_id == user.id, ChatSession.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    await db.execute(
        update(Document)
        .where(Document.user_id == user.id, Document.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    await db.flush()
    await run_in_background("remove_user_uploads", _remove_user_uploads(user.id))
    logger.info("account_deleted", user_id=str(user.id))
