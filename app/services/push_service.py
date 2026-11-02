"""Push notifications for turns that finish while the client is away.

Devices register their platform token; `notify_generation_done` fans a small payload out
to every device of the user through the configured provider. The built-in provider is a
signed webhook (`PUSH_PROVIDER=webhook`) so deployments bridge to FCM, APNs, OneSignal or
anything else without the API holding those credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable
from uuid import UUID

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logger import get_logger
from app.db.session import db_session
from app.models.device import Device

logger = get_logger(__name__)

Sender = Callable[[dict[str, Any]], Awaitable[bool]]
PLATFORMS = ("ios", "android", "web")


async def register_device(
    db: AsyncSession,
    *,
    user_id: UUID,
    platform: str,
    token: str,
    app_version: str | None = None,
    locale: str | None = None,
) -> Device:
    """Upsert by (platform, token); a token moving to another account is re-owned."""
    stmt = select(Device).where(Device.platform == platform, Device.token == token)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = Device(user_id=user_id, platform=platform, token=token)
        db.add(row)
    row.user_id = user_id
    row.app_version = app_version
    row.locale = locale
    row.last_seen_at = datetime.now(UTC)
    row.deleted_at = None
    await db.flush()
    return row


async def remove_device(db: AsyncSession, *, user_id: UUID, token: str) -> bool:
    result = await db.execute(delete(Device).where(Device.user_id == user_id, Device.token == token))
    return bool(result.rowcount)


async def list_devices(db: AsyncSession, *, user_id: UUID) -> list[Device]:
    stmt = select(Device).where(Device.user_id == user_id).order_by(Device.created_at.asc())
    return list((await db.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------- sending


async def _webhook_sender(payload: dict[str, Any]) -> bool:
    body = json.dumps(payload, default=str).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "nexusbot-push/1"}
    if settings.PUSH_WEBHOOK_SECRET:
        signature = hmac.new(settings.PUSH_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Push-Signature"] = f"sha256={signature}"
    async with httpx.AsyncClient(timeout=10, trust_env=settings.HTTP_TRUST_ENV) as client:
        response = await client.post(settings.PUSH_WEBHOOK_URL, content=body, headers=headers)
    if response.status_code >= 400:
        logger.warning("push_webhook_failed", status=response.status_code)
        return False
    return True


async def _log_sender(payload: dict[str, Any]) -> bool:
    logger.info("push_notification", user_id=payload.get("user_id"), title=payload["notification"]["title"])
    return True


_override: Sender | None = None


def set_sender(sender: Sender | None) -> None:
    """Tests (and custom deployments) can swap the delivery function."""
    global _override
    _override = sender


def _sender() -> Sender | None:
    if _override is not None:
        return _override
    provider = settings.PUSH_PROVIDER.lower()
    if provider == "webhook" and settings.PUSH_WEBHOOK_URL:
        return _webhook_sender
    if provider == "log":
        return _log_sender
    return None


async def send_to_user(user_id: UUID, *, title: str, body: str, data: dict[str, Any] | None = None) -> int:
    """Deliver one notification to every device of the user; returns the device count."""
    sender = _sender()
    if sender is None:
        return 0
    async with db_session() as db:
        devices = await list_devices(db, user_id=user_id)
    if not devices:
        return 0
    payload = {
        "user_id": str(user_id),
        "sent_at": time.time(),
        "devices": [{"platform": d.platform, "token": d.token} for d in devices],
        "notification": {"title": title[:120], "body": body[:240]},
        "data": {k: str(v) for k, v in (data or {}).items()},
    }
    try:
        ok = await sender(payload)
    except Exception as e:  # noqa: BLE001 — notifications must never break a turn
        logger.warning("push_send_failed", error=str(e)[:200])
        return 0
    return len(devices) if ok else 0


async def notify_generation_done(*, user_id: UUID, session_id: UUID, generation_id: str, preview: str) -> int:
    text = " ".join(preview.split())
    return await send_to_user(
        user_id,
        title="Reply ready",
        body=(text[:200] + "…") if len(text) > 200 else (text or "Your assistant finished answering."),
        data={"type": "generation_done", "session_id": str(session_id), "generation_id": generation_id},
    )
