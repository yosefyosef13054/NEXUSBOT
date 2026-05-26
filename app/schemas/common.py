from __future__ import annotations

from datetime import datetime
from typing import Generic, List, Optional, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

T = TypeVar("T")


class ORMBase(BaseModel):
    """Base for schemas read straight from ORM rows."""

    model_config = {"from_attributes": True}


class TimestampedRead(ORMBase):
    id: UUID
    created_at: datetime
    updated_at: datetime


class Page(BaseModel, Generic[T]):
    items: List[T]
    total: int
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)


class OkResponse(BaseModel):
    ok: bool = True
    message: Optional[str] = None
