"""Registered MCP servers per organization and the tools they expose."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.mcp import client as mcp_client
from app.ai.mcp.client import MCPToolSpec, ServerConfig, split_full_name
from app.cache.redis_client import get_redis
from app.config import settings
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.logger import get_logger
from app.models.mcp_server import MCPServer, MCPTransport
from app.models.organization import Membership, OrgRole
from app.models.user import User
from app.schemas.mcp import MCPServerCreateRequest, MCPServerUpdateRequest
from app.services.org_service import require_role

logger = get_logger(__name__)


def config_of(row: MCPServer) -> ServerConfig:
    return ServerConfig(
        name=row.name,
        transport=row.transport.value,
        command=row.command,
        args=tuple(str(a) for a in (row.args or [])),
        env={str(k): str(v) for k, v in (row.env or {}).items()},
        url=row.url,
        headers={str(k): str(v) for k, v in (row.headers or {}).items()},
        requires_approval=row.requires_approval,
        timeout_seconds=float(settings.MCP_CALL_TIMEOUT_SECONDS),
    )


def read_payload(row: MCPServer) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "description": row.description,
        "transport": row.transport.value,
        "command": row.command,
        "args": list(row.args or []),
        "env_keys": sorted((row.env or {}).keys()),
        "url": row.url,
        "header_keys": sorted((row.headers or {}).keys()),
        "enabled": row.enabled,
        "requires_approval": row.requires_approval,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


# --------------------------------------------------------------------------- CRUD


def _check_transport(transport: str, *, user: User, command: str | None, url: str | None) -> None:
    if not settings.MCP_ENABLED:
        raise ForbiddenError("MCP integration is disabled on this server")
    if transport == "stdio":
        if not settings.MCP_ALLOW_STDIO:
            raise ForbiddenError("stdio MCP servers are disabled on this server (MCP_ALLOW_STDIO)")
        if user.role != "admin":
            raise ForbiddenError("Only server administrators can register stdio MCP servers")
        if not command:
            raise ValidationError("stdio transport requires a command")
    else:
        if not url or not url.lower().startswith(("http://", "https://")):
            raise ValidationError(f"{transport} transport requires an http(s) url")


async def list_servers(db: AsyncSession, *, org_id: UUID, enabled_only: bool = False) -> list[MCPServer]:
    stmt = select(MCPServer).where(MCPServer.org_id == org_id, MCPServer.deleted_at.is_(None))
    if enabled_only:
        stmt = stmt.where(MCPServer.enabled.is_(True))
    return list((await db.execute(stmt.order_by(MCPServer.name.asc()))).scalars().all())


async def get_server(db: AsyncSession, *, org_id: UUID, server_id: UUID) -> MCPServer:
    row = await db.get(MCPServer, server_id)
    if row is None or row.deleted_at is not None or row.org_id != org_id:
        raise NotFoundError("MCP server not found")
    return row


async def create_server(
    db: AsyncSession, *, membership: Membership, user: User, payload: MCPServerCreateRequest
) -> MCPServer:
    require_role(membership, OrgRole.ADMIN)
    _check_transport(payload.transport, user=user, command=payload.command, url=payload.url)
    existing = await db.execute(
        select(MCPServer.id).where(
            MCPServer.org_id == membership.org_id,
            MCPServer.name == payload.name,
            MCPServer.deleted_at.is_(None),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("An MCP server with this name already exists in the organization")
    row = MCPServer(
        org_id=membership.org_id,
        created_by=user.id,
        name=payload.name,
        description=payload.description,
        transport=MCPTransport(payload.transport),
        command=payload.command,
        args=list(payload.args),
        env=dict(payload.env),
        url=payload.url,
        headers=dict(payload.headers),
        enabled=payload.enabled,
        requires_approval=payload.requires_approval,
        meta={},
    )
    db.add(row)
    await db.flush()
    return row


async def update_server(
    db: AsyncSession, *, membership: Membership, user: User, server_id: UUID, payload: MCPServerUpdateRequest
) -> MCPServer:
    require_role(membership, OrgRole.ADMIN)
    row = await get_server(db, org_id=membership.org_id, server_id=server_id)
    data = payload.model_dump(exclude_unset=True)
    command = data.get("command", row.command)
    url = data.get("url", row.url)
    _check_transport(row.transport.value, user=user, command=command, url=url)
    for key, value in data.items():
        setattr(row, key, value)
    await db.flush()
    await invalidate_tools_cache(row.id)
    return row


async def delete_server(db: AsyncSession, *, membership: Membership, server_id: UUID) -> None:
    require_role(membership, OrgRole.ADMIN)
    row = await get_server(db, org_id=membership.org_id, server_id=server_id)
    row.deleted_at = datetime.now(UTC)
    await db.flush()
    await invalidate_tools_cache(row.id)


# --------------------------------------------------------------------------- discovery


def _cache_key(server_id: UUID) -> str:
    return f"mcp:tools:{server_id}"


async def invalidate_tools_cache(server_id: UUID) -> None:
    await get_redis().delete(_cache_key(server_id))


async def server_tools(row: MCPServer, *, use_cache: bool = True) -> list[MCPToolSpec]:
    r = get_redis()
    if use_cache:
        cached = await r.get(_cache_key(row.id))
        if cached:
            return [MCPToolSpec(**item) for item in json.loads(cached)]
    specs = await mcp_client.list_tools(config_of(row))
    await r.set(
        _cache_key(row.id),
        json.dumps([spec.__dict__ for spec in specs]),
        ex=settings.MCP_TOOLS_CACHE_SECONDS,
    )
    return specs


async def test_server(row: MCPServer) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        specs = await server_tools(row, use_cache=False)
    except Exception as e:  # noqa: BLE001 — reported to the admin, not raised
        return {
            "ok": False,
            "tools": [],
            "error": f"{type(e).__name__}: {str(e)[:300]}",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }
    return {
        "ok": True,
        "tools": [s.to_public() for s in specs],
        "error": None,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


async def discover_tools(db: AsyncSession, *, org_id: UUID | None) -> list[MCPToolSpec]:
    """Every tool of every enabled server in the org (unreachable servers are skipped)."""
    if org_id is None or not settings.MCP_ENABLED:
        return []
    specs: list[MCPToolSpec] = []
    for row in await list_servers(db, org_id=org_id, enabled_only=True):
        try:
            specs.extend(await server_tools(row))
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp_discovery_failed", server=row.name, error=str(e)[:200])
    return specs


async def resolve_tool_names(
    db: AsyncSession, *, org_id: UUID | None, names: list[str]
) -> tuple[list[MCPToolSpec], list[str]]:
    """(matching specs, unknown names) for `mcp:` tool names in the org."""
    wanted = [n for n in names if n.startswith(mcp_client.TOOL_PREFIX)]
    if not wanted:
        return [], []
    available = {s.full_name: s for s in await discover_tools(db, org_id=org_id)}
    found = [available[n] for n in wanted if n in available]
    unknown = [n for n in wanted if n not in available]
    return found, unknown


async def build_tools(db: AsyncSession, *, org_id: UUID | None, names: list[str]) -> list[Any]:
    """LangChain tools for the given `mcp:` names (names that no longer resolve are skipped)."""
    if org_id is None or not names:
        return []
    rows = {row.name: row for row in await list_servers(db, org_id=org_id, enabled_only=True)}
    tools: list[Any] = []
    for name in names:
        parts = split_full_name(name)
        if parts is None or parts[0] not in rows:
            continue
        row = rows[parts[0]]
        try:
            specs = await server_tools(row)
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp_tools_unavailable", server=row.name, error=str(e)[:200])
            continue
        spec = next((s for s in specs if s.name == parts[1]), None)
        if spec is not None:
            tools.append(mcp_client.build_tool(spec, config_of(row)))
    return tools
