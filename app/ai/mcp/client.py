from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, create_model

from app.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

TOOL_PREFIX = "mcp:"


@dataclass(frozen=True)
class ServerConfig:
    name: str
    transport: str  # stdio | http | sse
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    requires_approval: bool = True
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class MCPToolSpec:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    requires_approval: bool = True

    @property
    def full_name(self) -> str:
        return f"{TOOL_PREFIX}{self.server}:{self.name}"

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.full_name,
            "server": self.server,
            "tool": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "requires_approval": self.requires_approval,
        }


def split_full_name(full_name: str) -> tuple[str, str] | None:
    if not full_name.startswith(TOOL_PREFIX):
        return None
    rest = full_name[len(TOOL_PREFIX) :]
    server, _, tool = rest.partition(":")
    if not server or not tool:
        return None
    return server, tool


@contextlib.asynccontextmanager
async def connect(cfg: ServerConfig) -> AsyncIterator[Any]:
    """An initialized `ClientSession` for the server (one connection per call: simple,
    stateless across workers, and MCP servers are expected to be cheap to connect to)."""
    from mcp import ClientSession

    if cfg.transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import get_default_environment, stdio_client

        if not cfg.command:
            raise ValueError("stdio transport needs a command")
        # The server inherits a minimal safe environment (PATH, HOME, …) plus its own vars.
        env = {**get_default_environment(), **dict(cfg.env)}
        params = StdioServerParameters(command=cfg.command, args=list(cfg.args), env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif cfg.transport == "http":
        from mcp.client.streamable_http import streamablehttp_client

        if not cfg.url:
            raise ValueError("http transport needs a url")
        async with streamablehttp_client(cfg.url, headers=dict(cfg.headers) or None) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif cfg.transport == "sse":
        from mcp.client.sse import sse_client

        if not cfg.url:
            raise ValueError("sse transport needs a url")
        async with sse_client(cfg.url, headers=dict(cfg.headers) or None) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:
        raise ValueError(f"unknown MCP transport {cfg.transport}")


async def list_tools(cfg: ServerConfig) -> list[MCPToolSpec]:
    async with asyncio.timeout(cfg.timeout_seconds):
        async with connect(cfg) as session:
            result = await session.list_tools()
    specs: list[MCPToolSpec] = []
    for tool in result.tools:
        specs.append(
            MCPToolSpec(
                server=cfg.name,
                name=tool.name,
                description=(tool.description or "")[:2000],
                input_schema=dict(tool.inputSchema or {"type": "object", "properties": {}}),
                requires_approval=cfg.requires_approval,
            )
        )
    return specs


def _content_text(content: Any) -> str:
    parts: list[str] = []
    for item in content or []:
        kind = getattr(item, "type", None)
        if kind == "text":
            parts.append(str(getattr(item, "text", "")))
        elif kind == "image":
            parts.append(f"[image {getattr(item, 'mimeType', '')}]")
        elif kind == "resource":
            res = getattr(item, "resource", None)
            text = getattr(res, "text", None)
            parts.append(text if text else f"[resource {getattr(res, 'uri', '')}]")
        else:
            parts.append(str(item))
    return "\n".join(p for p in parts if p)


async def call_tool(cfg: ServerConfig, name: str, arguments: dict[str, Any]) -> str:
    async with asyncio.timeout(cfg.timeout_seconds):
        async with connect(cfg) as session:
            result = await session.call_tool(name, arguments=arguments)
    text = _content_text(getattr(result, "content", None))
    if getattr(result, "isError", False):
        return f"Error: {text or 'tool call failed'}"
    return text or "(no output)"


# --------------------------------------------------------------------------- LangChain adapter

_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def json_schema_to_model(name: str, schema: dict[str, Any]) -> Type[BaseModel]:
    """A pydantic model from a (flat) JSON schema so LangChain can validate tool calls."""
    props: dict[str, Any] = dict(schema.get("properties") or {})
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        raw_type = spec.get("type")
        if isinstance(raw_type, list):
            raw_type = next((t for t in raw_type if t != "null"), "string")
        py_type: Any = _JSON_TYPES.get(str(raw_type), Any) if raw_type else Any
        description = str(spec.get("description") or "")
        if key in required:
            fields[key] = (py_type, Field(..., description=description))
        else:
            fields[key] = (py_type | None, Field(default=spec.get("default"), description=description))
    safe = "".join(c if c.isalnum() else "_" for c in name) or "Args"
    return create_model(f"{safe}_Args", **fields)  # type: ignore[call-overload]


class MCPTool(BaseTool):
    """One remote tool, callable like any built-in tool."""

    name: str
    description: str
    args_schema: Type[BaseModel]
    server_config: ServerConfig
    remote_name: str
    nexusbot_requires_approval: bool = True

    def _run(self, **kwargs: Any) -> str:  # pragma: no cover
        raise NotImplementedError("MCP tools are async only")

    async def _arun(self, **kwargs: Any) -> str:
        arguments = {k: v for k, v in kwargs.items() if v is not None and k != "run_manager"}
        try:
            return await call_tool(self.server_config, self.remote_name, arguments)
        except TimeoutError:
            return f"Error: MCP server '{self.server_config.name}' timed out"
        except Exception as e:  # noqa: BLE001 — surfaced to the model as a tool error
            logger.warning(
                "mcp_tool_failed", server=self.server_config.name, tool=self.remote_name, error=str(e)[:200]
            )
            return f"Error: {str(e)[:300]}"


def build_tool(spec: MCPToolSpec, cfg: ServerConfig) -> MCPTool:
    description = spec.description or f"Tool '{spec.name}' from MCP server '{spec.server}'"
    if spec.input_schema.get("properties"):
        description += "\nArguments: " + json.dumps(spec.input_schema.get("properties"))[:800]
    return MCPTool(
        name=spec.full_name,
        description=description[:3000],
        args_schema=json_schema_to_model(spec.full_name, spec.input_schema),
        server_config=cfg,
        remote_name=spec.name,
        nexusbot_requires_approval=spec.requires_approval,
    )


def default_timeout() -> float:
    return float(settings.MCP_CALL_TIMEOUT_SECONDS)
