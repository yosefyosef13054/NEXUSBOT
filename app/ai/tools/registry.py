from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from langchain_core.tools import BaseTool

from app.ai.tools.calculator import CalculatorTool
from app.ai.tools.context import ToolContext
from app.ai.tools.database_query import UserSessionsTool
from app.ai.tools.fetch_url import FetchUrlTool
from app.ai.tools.python_sandbox import PythonSandboxTool
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
    # Feature-flagged tools disappear from the registry when their switch is off.
    enabled: Callable[[], bool] = lambda: True


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
    "run_python": ToolInfo(
        name="run_python",
        description=PythonSandboxTool(ctx=ToolContext(user_id=UUID(int=0))).description,
        factory=lambda ctx: PythonSandboxTool(ctx=_needs_context("run_python")(ctx)),
        requires_approval=True,
        enabled=lambda: settings.CODE_EXECUTION_ENABLED,
    ),
}

DEFAULT_TOOL_NAMES: tuple[str, ...] = ("calculator", "web_search")


def _enabled() -> dict[str, ToolInfo]:
    return {name: t for name, t in _TOOLS.items() if t.enabled()}


def list_tool_names() -> list[str]:
    return list(_enabled().keys())


def list_tools() -> list[ToolInfo]:
    return [
        ToolInfo(
            name=t.name,
            description=t.description,
            factory=t.factory,
            requires_approval=requires_approval(t.name),
            enabled=t.enabled,
        )
        for t in _enabled().values()
    ]


def requires_approval(name: str) -> bool:
    info = _TOOLS.get(name)
    if info is None:
        return False
    return info.requires_approval or name in set(settings.TOOLS_REQUIRING_APPROVAL)


def is_external(name: str) -> bool:
    """Tools served by MCP servers are namespaced `mcp:<server>:<tool>`."""
    return name.startswith("mcp:")


def unknown_tool_names(names: list[str] | None) -> list[str]:
    """Built-in names that do not exist (external `mcp:` names are validated per org)."""
    enabled = _enabled()
    return [n for n in (names or []) if not is_external(n) and n not in enabled]


def get_tools(names: list[str] | None = None, ctx: ToolContext | None = None) -> list[BaseTool]:
    """Build the requested toolset.

    `names=None` returns the default safe set (calculator + web_search). Unknown names
    are ignored here; the API layer rejects them up front with a 422.
    """
    selected = names or list(DEFAULT_TOOL_NAMES)
    enabled = _enabled()
    return [enabled[n].factory(ctx) for n in selected if n in enabled]


def register_tool(info: ToolInfo) -> None:
    """Extension point for custom tools (plugins, Phase 4 MCP adapters)."""
    _TOOLS[info.name] = info
