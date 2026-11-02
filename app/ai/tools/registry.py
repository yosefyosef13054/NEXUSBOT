from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from langchain_core.tools import BaseTool

from app.ai.tools.calculator import CalculatorTool
from app.ai.tools.context import ToolContext
from app.ai.tools.database_query import UserSessionsTool
from app.ai.tools.fetch_url import FetchUrlTool
from app.ai.tools.web_search import WebSearchTool
from app.config import settings

# A factory receives the request-scoped ToolContext so tools that touch user data are
# bound to the caller's identity instead of trusting a `user_id` argument from the model.
ToolFactory = Callable[[ToolContext | None], BaseTool]


def _needs_context(name: str) -> Callable[[ToolContext | None], ToolContext]:
    def _check(ctx: ToolContext | None) -> ToolContext:
        if ctx is None:
            raise ValueError(f"Tool '{name}' requires a ToolContext")
        return ctx

    return _check


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    factory: ToolFactory
    # Side-effecting or outbound tools stop the agent and ask the user before running.
    requires_approval: bool = False


_TOOLS: dict[str, ToolInfo] = {
    "calculator": ToolInfo(
        name="calculator",
        description=CalculatorTool().description,
        factory=lambda ctx: CalculatorTool(),
    ),
    "web_search": ToolInfo(
        name="web_search",
        description=WebSearchTool().description,
        factory=lambda ctx: WebSearchTool(),
    ),
    "fetch_url": ToolInfo(
        name="fetch_url",
        description=FetchUrlTool().description,
        factory=lambda ctx: FetchUrlTool(),
        requires_approval=True,
    ),
    "list_user_sessions": ToolInfo(
        name="list_user_sessions",
        description=UserSessionsTool(ctx=ToolContext(user_id=UUID(int=0))).description,
        factory=lambda ctx: UserSessionsTool(ctx=_needs_context("list_user_sessions")(ctx)),
    ),
}

DEFAULT_TOOL_NAMES: tuple[str, ...] = ("calculator", "web_search")


def list_tool_names() -> list[str]:
    return list(_TOOLS.keys())


def list_tools() -> list[ToolInfo]:
    return [
        ToolInfo(
            name=t.name,
            description=t.description,
            factory=t.factory,
            requires_approval=requires_approval(t.name),
        )
        for t in _TOOLS.values()
    ]


def requires_approval(name: str) -> bool:
    info = _TOOLS.get(name)
    if info is None:
        return False
    return info.requires_approval or name in set(settings.TOOLS_REQUIRING_APPROVAL)


def unknown_tool_names(names: list[str] | None) -> list[str]:
    return [n for n in (names or []) if n not in _TOOLS]


def get_tools(names: list[str] | None = None, ctx: ToolContext | None = None) -> list[BaseTool]:
    """Build the requested toolset.

    `names=None` returns the default safe set (calculator + web_search). Unknown names
    are ignored here; the API layer rejects them up front with a 422.
    """
    selected = names or list(DEFAULT_TOOL_NAMES)
    return [_TOOLS[n].factory(ctx) for n in selected if n in _TOOLS]


def register_tool(info: ToolInfo) -> None:
    """Extension point for custom tools (plugins, Phase 4 MCP adapters)."""
    _TOOLS[info.name] = info
