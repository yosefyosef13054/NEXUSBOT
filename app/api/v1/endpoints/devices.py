from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.dependencies import current_user
from app.core.exceptions import NotFoundError
from app.db.session import get_db
from app.models.device import Device
from app.models.user import User
from app.schemas.common import OkResponse
from app.schemas.share import DeviceListResponse, DeviceRead, DeviceRegisterRequest
from app.services import push_service

router = APIRouter(prefix="/devices", tags=["push"])


def _read(d: Device) -> DeviceRead:
    return DeviceRead(
        id=d.id,
        platform=d.platform,
        token_suffix=d.token[-6:],
        app_version=d.app_version,
        locale=d.locale,
        last_seen_at=d.last_seen_at,
        created_at=d.created_at,
    )


@router.post("", response_model=DeviceRead, status_code=status.HTTP_201_CREATED)
async def register_device(
    payload: DeviceRegisterRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Register (or refresh) a push token for the caller; idempotent per (platform, token)."""
    row = await push_service.register_device(
        db,
        user_id=user.id,
        platform=payload.platform,
        token=payload.token,
        app_version=payload.app_version,
        locale=payload.locale,
    )
    await db.commit()
    await db.refresh(row)
    return _read(row)


@router.get("", response_model=DeviceListResponse)
async def list_devices(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = await push_service.list_devices(db, user_id=user.id)
    enabled = settings.PUSH_PROVIDER.lower() not in ("", "none")
    return DeviceListResponse(items=[_read(d) for d in rows], push_enabled=enabled)


@router.delete("/{token}", response_model=OkResponse)
async def remove_device(token: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    removed = await push_service.remove_device(db, user_id=user.id, token=token)
    await db.commit()
    if not removed:
        raise NotFoundError("Device not found")
    return OkResponse(message="Device removed")
