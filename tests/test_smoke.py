"""Lightweight smoke tests — no LLM calls, no DB.

Exercises pure modules so CI catches import-time and logic regressions even
without infra. Integration tests against Postgres/Redis/OpenAI belong in a
separate suite gated by env flags.
"""
from app.ai.prompts.templates import build_chat_prompt
from app.ai.tools.calculator import CalculatorTool
from app.ai.tools.registry import get_tools, list_tool_names
from app.utils.tokens import count_tokens, estimate_cost


def test_calculator_arithmetic():
    t = CalculatorTool()
    assert t._run(expression="2*(3+4)/5") == "2.8"


def test_calculator_rejects_code():
    import pytest
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


def test_prompt_composes_rag_and_tools():
    p = build_chat_prompt(use_rag=True, use_tools=True)
    rendered = p.format_messages(input="hi", history=[], summary="", context="ctx")
    assert any("Retrieved context" in m.content for m in rendered)
