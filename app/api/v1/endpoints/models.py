from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers.registry import get_registry
from app.ai.tools.registry import list_tools
from app.core.dependencies import Principal, current_principal, current_user
from app.db.session import get_db
from app.schemas.models import ModelListResponse, ModelRead, ToolListResponse, ToolRead
from app.services import mcp_service

router = APIRouter(tags=["models"])


@router.get("/models", response_model=ModelListResponse, dependencies=[Depends(current_user)])
async def list_models():
    """Models the server can serve right now (providers with credentials configured)."""
    registry = get_registry()
    default = registry.default_model
    return ModelListResponse(
        default_model=default,
        items=[ModelRead(**spec.to_public(is_default=spec.id == default)) for spec in registry.available()],
    )


@router.get("/tools", response_model=ToolListResponse)
async def list_available_tools(
    principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
):
    """Tools the agent may use (built-in plus the active org's MCP servers), with whether
    each one needs the user's approval."""
    items = [
        ToolRead(
            name=t.name, description=t.description, requires_approval=t.requires_approval, source="builtin"
        )
        for t in list_tools()
    ]
    for spec in await mcp_service.discover_tools(db, org_id=principal.org_id):
        items.append(
            ToolRead(
                name=spec.full_name,
                description=spec.description,
                requires_approval=spec.requires_approval,
                source=f"mcp:{spec.server}",
            )
        )
    return ToolListResponse(items=items)
