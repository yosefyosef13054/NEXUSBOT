from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

OrgRoleLiteral = Literal["owner", "admin", "member"]


class OrgSettings(BaseModel):
    """Workspace-level policy. Null means unlimited / server default."""

    budget_tokens_per_month: int | None = Field(default=None, ge=0)
    budget_usd_per_month: float | None = Field(default=None, ge=0)
    default_model: str | None = Field(default=None, max_length=64)
    allowed_models: list[str] | None = None
    model_config = {"extra": "ignore"}


class OrgRead(BaseModel):
    id: UUID
    name: str
    slug: str
    role: OrgRoleLiteral
    is_personal: bool
    plan: str
    member_count: int = 1
    settings: OrgSettings = Field(default_factory=OrgSettings)
    created_at: datetime


class OrgListResponse(BaseModel):
    items: list[OrgRead]


class OrgCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class OrgUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    settings: OrgSettings | None = None


class MemberRead(BaseModel):
    user_id: UUID
    email: str
    full_name: str | None = None
    role: OrgRoleLiteral
    joined_at: datetime


class MemberListResponse(BaseModel):
    items: list[MemberRead]


class MemberAddRequest(BaseModel):
    email: EmailStr
    role: Literal["admin", "member"] = "member"


class MemberUpdateRequest(BaseModel):
    role: Literal["admin", "member"]
