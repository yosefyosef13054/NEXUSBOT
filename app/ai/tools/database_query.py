from __future__ import annotations

import json
from typing import Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.ai.tools.context import ToolContext
from app.db.session import db_session
from app.models.chat import ChatSession


class UserSessionsInput(BaseModel):
    limit: int = Field(default=10, ge=1, le=50, description="How many recent sessions to return.")


class UserSessionsTool(BaseTool):
    """Lists the *calling* user's recent chat sessions.

    Template for safe, scoped DB tools: the user identity comes from the injected
    `ToolContext`, never from the model's arguments, so the model cannot ask for
    another user's data.
    """

    name: str = "list_user_sessions"
    description: str = "Returns the current user's most recent chat sessions (id, title, created_at)."
    args_schema: Type[BaseModel] = UserSessionsInput
    ctx: ToolContext

    def _run(self, **_: object) -> str:  # type: ignore[override]
        raise NotImplementedError("Use async invocation.")

    async def _arun(self, limit: int = 10) -> str:  # type: ignore[override]
        async with db_session() as session:
            stmt = (
                select(ChatSession.id, ChatSession.title, ChatSession.created_at)
                .where(ChatSession.user_id == self.ctx.user_id, ChatSession.deleted_at.is_(None))
                .order_by(ChatSession.created_at.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()
        return json.dumps(
            [{"id": str(r.id), "title": r.title, "created_at": r.created_at.isoformat()} for r in rows]
        )
