"""Grading functions for eval cases. Each returns (passed, detail)."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

JUDGE_SYSTEM = (
    "You are a strict evaluator. Judge whether the assistant reply satisfies the rubric. "
    'Reply with JSON only: {"pass": true|false, "reason": "<one sentence>"}.'
)


def _lower(text: str) -> str:
    return text.lower()


def check_contains(reply: str, needles: list[str]) -> tuple[bool, str]:
    missing = [n for n in needles if _lower(n) not in _lower(reply)]
    return (not missing, f"missing: {missing}" if missing else "all present")


def check_not_contains(reply: str, needles: list[str]) -> tuple[bool, str]:
    present = [n for n in needles if _lower(n) in _lower(reply)]
    return (not present, f"present: {present}" if present else "none present")


def check_regex(reply: str, pattern: str) -> tuple[bool, str]:
    ok = re.search(pattern, reply, re.S | re.I) is not None
    return ok, f"pattern {pattern!r} {'matched' if ok else 'did not match'}"


def check_equals(reply: str, expected: str) -> tuple[bool, str]:
    ok = reply.strip() == expected.strip()
    return ok, "exact match" if ok else f"got {reply.strip()[:80]!r}"


def check_min_length(reply: str, n: int) -> tuple[bool, str]:
    return len(reply) >= n, f"length {len(reply)} (min {n})"


def check_max_length(reply: str, n: int) -> tuple[bool, str]:
    return len(reply) <= n, f"length {len(reply)} (max {n})"


def check_tool_called(tool_calls: list[dict[str, Any]], name: str) -> tuple[bool, str]:
    names = [t.get("tool") or t.get("name") for t in tool_calls]
    return name in names, f"tools called: {names}"


def check_citations_min(citations: list[dict[str, Any]], n: int) -> tuple[bool, str]:
    return len(citations) >= n, f"{len(citations)} citation(s) (min {n})"


def check_status(status: str, expected: str) -> tuple[bool, str]:
    return status == expected, f"status {status!r} (expected {expected!r})"


async def check_judge(
    *, user_input: str, reply: str, rubric: str, model: str | None = None
) -> tuple[bool, str]:
    """LLM-as-judge: a second model grades the reply against a rubric."""
    from app.ai.llm import build_chat_llm

    llm = build_chat_llm(model=model, streaming=False, temperature=0, max_tokens=200)
    out = await llm.ainvoke(
        [
            SystemMessage(content=JUDGE_SYSTEM),
            HumanMessage(
                content=f"Rubric: {rubric}\n\nUser message:\n{user_input[:3000]}\n\nAssistant reply:\n{reply[:6000]}"
            ),
        ]
    )
    text = out.content if isinstance(out.content, str) else str(out.content)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return False, f"judge returned no JSON: {text[:120]!r}"
    try:
        verdict = json.loads(match.group(0))
    except json.JSONDecodeError:
        return False, f"judge returned invalid JSON: {text[:120]!r}"
    return bool(verdict.get("pass")), str(verdict.get("reason", ""))[:300]
