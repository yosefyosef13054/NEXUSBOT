from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers.registry import get_registry
from app.ai.tools.registry import list_tool_names, unknown_tool_names
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.assistant import Assistant
from app.models.collection import Collection
from app.models.organization import Membership, OrgRole
from app.schemas.assistant import AssistantCreateRequest, AssistantUpdateRequest
from app.services.org_service import has_role


async def _validate(
    db: AsyncSession,
    *,
    org_id: UUID,
    model: str | None,
    tool_names: list[str] | None,
    collection_ids: list[UUID] | None,
) -> None:
    get_registry().validate(model)
    unknown = unknown_tool_names(tool_names)
    if not unknown:
        from app.services import mcp_service

        _, unknown = await mcp_service.resolve_tool_names(db, org_id=org_id, names=list(tool_names or []))
    if unknown:
        raise ValidationError("Unknown tools", details={"unknown": unknown, "available": list_tool_names()})
    if collection_ids:
        rows = (
            (
                await db.execute(
                    select(Collection.id).where(
                        Collection.org_id == org_id,
                        Collection.deleted_at.is_(None),
                        Collection.id.in_(collection_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        missing = [str(c) for c in collection_ids if c not in set(rows)]
        if missing:
            raise ValidationError("Unknown collections", details={"unknown": missing})


async def _clear_default(db: AsyncSession, org_id: UUID, except_id: UUID | None = None) -> None:
    stmt = (
        update(Assistant)
        .where(Assistant.org_id == org_id, Assistant.is_default.is_(True))
        .values(is_default=False)
    )
    if except_id is not None:
        stmt = stmt.where(Assistant.id != except_id)
    await db.execute(stmt)


async def create_assistant(
    db: AsyncSession, *, org_id: UUID, user_id: UUID, payload: AssistantCreateRequest
) -> Assistant:
    await _validate(
        db,
        org_id=org_id,
        model=payload.model,
        tool_names=payload.tool_names,
        collection_ids=payload.collection_ids,
    )
    if payload.is_default:
        await _clear_default(db, org_id)
    obj = Assistant(
        org_id=org_id,
        created_by=user_id,
        name=payload.name.strip(),
        description=payload.description,
        avatar=payload.avatar,
        system_prompt=payload.system_prompt,
        model=payload.model,
        tool_names=list(payload.tool_names),
        collection_ids=[str(c) for c in payload.collection_ids],
        use_rag=payload.use_rag,
        use_tools=payload.use_tools,
        temperature=payload.temperature,
        memory_enabled=payload.memory_enabled,
        is_default=payload.is_default,
    )
    db.add(obj)
    await db.flush()
    return obj


async def get_assistant(db: AsyncSession, *, org_id: UUID, assistant_id: UUID) -> Assistant:
    obj = await db.get(Assistant, assistant_id)
    if obj is None or obj.org_id != org_id or obj.deleted_at is not None:
        raise NotFoundError("Assistant not found")
    return obj


async def list_assistants(
    db: AsyncSession, *, org_id: UUID, page: int, page_size: int
) -> tuple[list[Assistant], int]:
    base = select(Assistant).where(Assistant.org_id == org_id, Assistant.deleted_at.is_(None))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                base.order_by(Assistant.is_default.desc(), Assistant.name.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def default_assistant(db: AsyncSession, *, org_id: UUID) -> Assistant | None:
    stmt = select(Assistant).where(
        Assistant.org_id == org_id, Assistant.is_default.is_(True), Assistant.deleted_at.is_(None)
    )
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


def can_edit(membership: Membership, assistant: Assistant) -> bool:
    return assistant.created_by == membership.user_id or has_role(membership, OrgRole.ADMIN)


async def update_assistant(
    db: AsyncSession, *, membership: Membership, assistant_id: UUID, payload: AssistantUpdateRequest
) -> Assistant:
    obj = await get_assistant(db, org_id=membership.org_id, assistant_id=assistant_id)
    if not can_edit(membership, obj):
        raise ForbiddenError("Only the creator or an organization admin can edit this assistant")
    changes = payload.model_dump(exclude_unset=True)
    await _validate(
        db,
        org_id=obj.org_id,
        model=changes.get("model", obj.model),
        tool_names=changes.get("tool_names", obj.tool_names),
        collection_ids=changes.get("collection_ids", [UUID(str(c)) for c in obj.collection_ids]),
    )
    if changes.get("is_default"):
        await _clear_default(db, obj.org_id, except_id=obj.id)
    for field, value in changes.items():
        if field == "collection_ids":
            value = [str(c) for c in value or []]
        elif field == "name":
            value = value.strip()
        setattr(obj, field, value)
    await db.flush()
    await db.refresh(obj)
    return obj


async def delete_assistant(db: AsyncSession, *, membership: Membership, assistant_id: UUID) -> None:
    obj = await get_assistant(db, org_id=membership.org_id, assistant_id=assistant_id)
    if not can_edit(membership, obj):
        raise ForbiddenError("Only the creator or an organization admin can delete this assistant")
    obj.deleted_at = datetime.now(UTC)
    obj.is_default = False
    await db.flush()
