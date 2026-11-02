"""Files produced by tools during a turn.

A tool receives the sink through its `ToolContext` and calls `add()`; the bytes go to
object storage as an `artifact` attachment, an `artifact` event reaches the client while the
turn is still streaming, and the service layer binds the rows to the assistant message.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from app.db.session import db_session
from app.models.attachment import AttachmentKind
from app.services import attachment_service


class ArtifactSink:
    def __init__(
        self,
        *,
        user_id: UUID,
        org_id: UUID | None,
        session_id: UUID,
        queue: asyncio.Queue | None = None,
    ) -> None:
        self.user_id = user_id
        self.org_id = org_id
        self.session_id = session_id
        self.queue = queue
        self.records: list[dict[str, Any]] = []

    async def add(
        self, *, name: str, data: bytes, mime_type: str, description: str | None = None
    ) -> dict[str, Any]:
        async with db_session() as db:
            att = await attachment_service.store_bytes(
                db,
                user_id=self.user_id,
                org_id=self.org_id,
                session_id=self.session_id,
                kind=AttachmentKind.ARTIFACT,
                name=name,
                data=data,
                mime_type=mime_type,
                description=description,
            )
            record = attachment_service.summary(att)
        self.records.append(record)
        if self.queue is not None:
            await self.queue.put({"type": "artifact", "data": record})
        return record

    @property
    def ids(self) -> list[UUID]:
        return [UUID(r["id"]) for r in self.records]
