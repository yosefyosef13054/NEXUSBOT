from __future__ import annotations

from typing import Callable

from langchain_core.tools import BaseTool

from app.ai.tools.calculator import CalculatorTool
from app.ai.tools.database_query import UserSessionsTool
from app.ai.tools.web_search import WebSearchTool

# To register a new tool: implement BaseTool then add it here. The agent will
# pick it up automatically by name.
_FACTORIES: dict[str, Callable[[], BaseTool]] = {
    "calculator": CalculatorTool,
    "web_search": WebSearchTool,
    "list_user_sessions": UserSessionsTool,
}


def list_tool_names() -> list[str]:
    return list(_FACTORIES.keys())


def get_tools(names: list[str] | None = None) -> list[BaseTool]:
    """Build the requested toolset.

    `names=None` returns the default safe set (calculator + web_search).
    Pass an explicit list to scope down what the agent can call.
    """
    selected = names or ["calculator", "web_search"]
    return [_FACTORIES[n]() for n in selected if n in _FACTORIES]
