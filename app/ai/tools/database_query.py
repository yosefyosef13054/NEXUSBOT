from __future__ import annotations

import json
from typing import Type
from uuid import UUID

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db.session import db_session
from app.models.chat import ChatSession


class UserSessionsInput(BaseModel):
    user_id: UUID = Field(description="The user whose sessions to list.")
    limit: int = Field(default=10, ge=1, le=50)


class UserSessionsTool(BaseTool):
    """Example domain-specific tool — lists a user's recent chat sessions.

    This is a template for safe, scoped DB tools. Real business logic tools
    should always re-check authorization against the calling user.
    """

    name: str = "list_user_sessions"
    description: str = "Returns the user's most recent chat sessions (id, title, created_at)."
    args_schema: Type[BaseModel] = UserSessionsInput

    def _run(self, **_: object) -> str:  # type: ignore[override]
        raise NotImplementedError("Use async invocation.")

    async def _arun(self, user_id: UUID, limit: int = 10) -> str:  # type: ignore[override]
        async with db_session() as session:
            stmt = (
                select(ChatSession.id, ChatSession.title, ChatSession.created_at)
                .where(ChatSession.user_id == user_id, ChatSession.deleted_at.is_(None))
                .order_by(ChatSession.created_at.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()
        return json.dumps(
            [{"id": str(r.id), "title": r.title, "created_at": r.created_at.isoformat()} for r in rows]
        )
