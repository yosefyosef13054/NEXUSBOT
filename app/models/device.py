from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Device(Base):
    """A push-notification endpoint (FCM / APNs / web push token) of one user."""

    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("platform", "token", name="uq_devices_platform_token"),)

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(16), nullable=False)  # ios | android | web
    token: Mapped[str] = mapped_column(String(1024), nullable=False)
    app_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
