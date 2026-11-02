from __future__ import annotations

from fastapi import APIRouter, Depends

from app.ai.providers.registry import get_registry
from app.ai.tools.registry import list_tools
from app.core.dependencies import current_user
from app.schemas.models import ModelListResponse, ModelRead, ToolListResponse, ToolRead

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


@router.get("/tools", response_model=ToolListResponse, dependencies=[Depends(current_user)])
async def list_available_tools():
    """Tools the agent may use, with whether each one needs the user's approval."""
    return ToolListResponse(
        items=[
            ToolRead(name=t.name, description=t.description, requires_approval=t.requires_approval)
            for t in list_tools()
        ]
    )
