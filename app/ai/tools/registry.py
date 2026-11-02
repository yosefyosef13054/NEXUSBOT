from __future__ import annotations

from typing import Callable

from langchain_core.tools import BaseTool

from app.ai.tools.calculator import CalculatorTool
from app.ai.tools.context import ToolContext
from app.ai.tools.database_query import UserSessionsTool
from app.ai.tools.web_search import WebSearchTool

# A factory receives the request-scoped ToolContext so tools that touch user data are
# bound to the caller's identity instead of trusting a `user_id` argument from the model.
ToolFactory = Callable[[ToolContext | None], BaseTool]


def _needs_context(name: str) -> Callable[[ToolContext | None], ToolContext]:
    def _check(ctx: ToolContext | None) -> ToolContext:
        if ctx is None:
            raise ValueError(f"Tool '{name}' requires a ToolContext")
        return ctx

    return _check


_FACTORIES: dict[str, ToolFactory] = {
    "calculator": lambda ctx: CalculatorTool(),
    "web_search": lambda ctx: WebSearchTool(),
    "list_user_sessions": lambda ctx: UserSessionsTool(ctx=_needs_context("list_user_sessions")(ctx)),
}

DEFAULT_TOOL_NAMES: tuple[str, ...] = ("calculator", "web_search")


def list_tool_names() -> list[str]:
    return list(_FACTORIES.keys())


def unknown_tool_names(names: list[str] | None) -> list[str]:
    return [n for n in (names or []) if n not in _FACTORIES]


def get_tools(names: list[str] | None = None, ctx: ToolContext | None = None) -> list[BaseTool]:
    """Build the requested toolset.

    `names=None` returns the default safe set (calculator + web_search). Unknown names
    are ignored here; the API layer rejects them up front with a 422.
    """
    selected = names or list(DEFAULT_TOOL_NAMES)
    return [_FACTORIES[n](ctx) for n in selected if n in _FACTORIES]
