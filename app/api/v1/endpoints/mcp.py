from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import Principal, current_principal
from app.db.session import get_db
from app.schemas.common import OkResponse
from app.schemas.mcp import (
    MCPServerCreateRequest,
    MCPServerListResponse,
    MCPServerRead,
    MCPServerTestResponse,
    MCPServerUpdateRequest,
    MCPToolRead,
)
from app.services import mcp_service

router = APIRouter(prefix="/mcp/servers", tags=["mcp"])


def _read(row) -> MCPServerRead:
    return MCPServerRead(**mcp_service.read_payload(row))


@router.get("", response_model=MCPServerListResponse)
async def list_servers(principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    """MCP servers of the active organization (secrets are never returned)."""
    rows = await mcp_service.list_servers(db, org_id=principal.org_id)
    return MCPServerListResponse(items=[_read(r) for r in rows])


@router.post("", response_model=MCPServerRead, status_code=status.HTTP_201_CREATED)
async def create_server(
    payload: MCPServerCreateRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Register a server (org admin). `stdio` servers run a command on the API host and
    are limited to server administrators with `MCP_ALLOW_STDIO=true`."""
    row = await mcp_service.create_server(
        db, membership=principal.membership, user=principal.user, payload=payload
    )
    await db.commit()
    await db.refresh(row)
    return _read(row)


@router.get("/{server_id}", response_model=MCPServerRead)
async def get_server(
    server_id: UUID, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
):
    return _read(await mcp_service.get_server(db, org_id=principal.org_id, server_id=server_id))


@router.patch("/{server_id}", response_model=MCPServerRead)
async def update_server(
    server_id: UUID,
    payload: MCPServerUpdateRequest,
    principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    row = await mcp_service.update_server(
        db, membership=principal.membership, user=principal.user, server_id=server_id, payload=payload
    )
    await db.commit()
    await db.refresh(row)
    return _read(row)


@router.delete("/{server_id}", response_model=OkResponse)
async def delete_server(
    server_id: UUID, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
):
    await mcp_service.delete_server(db, membership=principal.membership, server_id=server_id)
    await db.commit()
    return OkResponse(message="MCP server removed")


@router.post("/{server_id}/test", response_model=MCPServerTestResponse)
async def test_server(
    server_id: UUID, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
):
    """Connect to the server and list its tools (refreshes the tool cache)."""
    row = await mcp_service.get_server(db, org_id=principal.org_id, server_id=server_id)
    result = await mcp_service.test_server(row)
    return MCPServerTestResponse(
        ok=result["ok"],
        tools=[MCPToolRead(**t) for t in result["tools"]],
        error=result["error"],
        latency_ms=result["latency_ms"],
    )


@router.get("/{server_id}/tools", response_model=list[MCPToolRead])
async def server_tools(
    server_id: UUID, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)
):
    row = await mcp_service.get_server(db, org_id=principal.org_id, server_id=server_id)
    specs = await mcp_service.server_tools(row)
    return [MCPToolRead(**s.to_public()) for s in specs]
