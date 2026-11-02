"""Lightweight smoke tests — no LLM calls, no DB.

Exercises pure modules so CI catches import-time and logic regressions even
without infra. Integration tests against Postgres/Redis live in the other modules.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.ai.prompts.templates import build_chat_prompt
from app.ai.tools.calculator import CalculatorTool
from app.ai.tools.context import ToolContext
from app.ai.tools.registry import get_tools, list_tool_names, unknown_tool_names
from app.utils.tokens import count_tokens, estimate_cost


def test_calculator_arithmetic():
    t = CalculatorTool()
    assert t._run(expression="2*(3+4)/5") == "2.8"


def test_calculator_rejects_code():
    t = CalculatorTool()
    with pytest.raises(ValueError):
        t._run(expression="__import__('os').system('echo hi')")


def test_token_count_is_positive():
    assert count_tokens("Hello, world!") > 0
    assert count_tokens("") == 0


def test_cost_estimate_known_model():
    cost = estimate_cost("gpt-4o-mini", prompt_tokens=1000, completion_tokens=1000)
    assert cost > 0


def test_tool_registry_default_set():
    names = list_tool_names()
    assert "calculator" in names and "web_search" in names
    tools = get_tools()
    assert {t.name for t in tools} == {"calculator", "web_search"}


def test_tool_registry_scoped_tool_needs_context():
    with pytest.raises(ValueError):
        get_tools(["list_user_sessions"])
    ctx = ToolContext(user_id=uuid4())
    (tool,) = get_tools(["list_user_sessions"], ctx)
    assert tool.ctx.user_id == ctx.user_id
    assert unknown_tool_names(["calculator", "nope"]) == ["nope"]


def test_prompt_composes_rag_and_tools():
    p = build_chat_prompt(use_rag=True, use_tools=True)
    from langchain_core.messages import HumanMessage

    rendered = p.format_messages(
        input_messages=[HumanMessage(content="hi")],
        history=[],
        summary="",
        context="ctx",
        agent_scratchpad=[],
    )
    assert any("Retrieved context" in m.content for m in rendered)


def test_tools_prompt_declares_agent_scratchpad():
    """`create_openai_tools_agent` refuses prompts without this placeholder."""
    p = build_chat_prompt(use_tools=True)
    assert "agent_scratchpad" in p.input_variables + list(p.optional_variables)
    assert "agent_scratchpad" not in build_chat_prompt(use_tools=False).input_variables
