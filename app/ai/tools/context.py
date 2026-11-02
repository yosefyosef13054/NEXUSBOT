from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ToolContext(BaseModel):
    """Identity and scope a tool runs under.

    Injected by the orchestrator from the authenticated request — never taken from the
    model's arguments. Any tool that touches user data must scope its queries by
    `user_id` from this object, which is what closes the "pass any user_id" hole.
    """

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    session_id: UUID | None = None
    request_id: str | None = None
