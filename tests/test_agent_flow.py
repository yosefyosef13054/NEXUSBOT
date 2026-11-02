"""LangGraph agent: tool execution, persisted tool messages, approvals, budgets."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import select

from app.ai.fake import script_responses, tool_call_message
from app.config import settings
from app.models.approval import Approval, ApprovalStatus
from app.models.chat import ChatMessage, MessageRole
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"


async def _session(client, acct) -> str:
    r = await client.post(f"{P}/sessions", json={}, headers=auth_headers(acct))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _frames(body: str) -> list[dict]:
    out = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


async def test_agent_runs_tool_and_persists_tool_message(client, db):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    script_responses(tool_call_message("calculator", {"expression": "6*7"}), "The answer is 42")
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "what is 6*7?", "use_rag": False, "use_tools": True, "tool_names": ["calculator"]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assistant_message"]["content"] == "The answer is 42"
    assert body["assistant_message"]["meta"]["status"] == "ok"
    assert body["approval"] is None
    assert len(body["tool_messages"]) == 1
    tool_msg = body["tool_messages"][0]
    assert tool_msg["role"] == "tool" and tool_msg["content"] == "42.0"
    assert tool_msg["meta"]["tool"] == "calculator" and tool_msg["meta"]["status"] == "ok"
    assert tool_msg["meta"]["input"] == {"expression": "6*7"}
    assert body["tool_calls"][0]["call_id"] == "call_1"

    r = await client.get(f"{P}/sessions/{sid}/messages", headers=auth_headers(acct))
    assert [m["role"] for m in r.json()["items"]] == ["user", "tool", "assistant"]
    # Usage covers both model calls of the agent loop.
    assert body["usage"]["prompt_tokens"] > 0 and body["usage"]["completion_tokens"] > 0


async def test_tool_messages_are_not_fed_back_to_the_model(client, db):
    from app.ai.memory.short_term import load_recent_history

    acct = await register_and_login(client)
    sid = await _session(client, acct)
    script_responses(tool_call_message("calculator", {"expression": "1+1"}), "two")
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "1+1", "use_rag": False, "use_tools": True, "tool_names": ["calculator"]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200
    history = await load_recent_history(db, session_id=UUID(sid))
    assert [m.type for m in history] == ["human", "ai"]


async def test_approval_required_then_denied(client, db):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    script_responses(
        tool_call_message("fetch_url", {"url": "https://example.com/report"}),
        "I could not read the page, but here is what I know.",
    )
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={
            "content": "read https://example.com/report",
            "use_rag": False,
            "use_tools": True,
            "tool_names": ["fetch_url"],
        },
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assistant_message"]["meta"]["status"] == "awaiting_approval"
    approval = body["approval"]
    assert approval["status"] == "pending"
    assert approval["tool_calls"][0]["tool"] == "fetch_url"
    assert approval["tool_calls"][0]["input"] == {"url": "https://example.com/report"}
    assert approval["tool_calls"][0]["call_id"] == "call_1"
    assert approval["message_id"] == body["assistant_message"]["id"]
    assert body["tool_messages"] == []

    r = await client.get(
        f"{P}/sessions/{sid}/approvals", params={"status": "pending"}, headers=auth_headers(acct)
    )
    assert [a["approval_id"] for a in r.json()["items"]] == [approval["approval_id"]]

    r = await client.post(
        f"{P}/sessions/{sid}/approvals/{approval['approval_id']}",
        json={"decision": "deny"},
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200, r.text
    events = _frames(r.text)
    types = [e["type"] for e in events]
    assert types[-1] == "assistant_message" and "done" in types
    final = events[-1]["data"]
    assert final["id"] == body["assistant_message"]["id"]  # updated in place
    assert final["status"] == "ok" and final["interrupted"] is False
    assert "could not read" in final["content"]
    tokens = "".join(e["data"] for e in events if e["type"] == "token")
    assert tokens.strip() == final["content"]

    r = await client.get(f"{P}/sessions/{sid}/messages", headers=auth_headers(acct))
    items = r.json()["items"]
    assert [m["role"] for m in items] == ["user", "tool", "assistant"]
    assert items[1]["meta"]["status"] == "denied" and items[1]["meta"]["tool"] == "fetch_url"
    assert items[2]["content"] == final["content"] and "approval" not in items[2]["meta"]

    row = await db.get(Approval, UUID(approval["approval_id"]))
    assert row.status == ApprovalStatus.DENIED and row.decided_at is not None

    r = await client.post(
        f"{P}/sessions/{sid}/approvals/{approval['approval_id']}",
        json={"decision": "approve"},
        headers=auth_headers(acct),
    )
    assert r.status_code == 409


async def test_approval_approved_runs_tool_over_sse(client, db, monkeypatch):
    monkeypatch.setattr(settings, "TOOLS_REQUIRING_APPROVAL", ["calculator"])
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    script_responses(tool_call_message("calculator", {"expression": "2**10"}, call_id="call_x"), "It is 1024")

    r = await client.post(
        f"{P}/sessions/{sid}/messages/stream",
        json={"content": "2**10", "use_rag": False, "use_tools": True, "tool_names": ["calculator"]},
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200
    events = _frames(r.text)
    types = [e["type"] for e in events]
    assert "approval_required" in types
    assert types[-1] == "assistant_message"
    approval_evt = next(e for e in events if e["type"] == "approval_required")["data"]
    done = next(e for e in events if e["type"] == "done")["data"]
    assert done["status"] == "awaiting_approval" and done["interrupted"] is False
    final = events[-1]["data"]
    assert (
        final["status"] == "awaiting_approval"
        and final["approval"]["approval_id"] == approval_evt["approval_id"]
    )
    assert approval_evt["tool_calls"][0]["call_id"] == "call_x"

    r = await client.post(
        f"{P}/sessions/{sid}/approvals/{approval_evt['approval_id']}",
        json={"decision": "approve"},
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200, r.text
    events = _frames(r.text)
    types = [e["type"] for e in events]
    assert "tool_call" in types  # the approved tool actually ran
    ends = [e["data"] for e in events if e["type"] == "tool_call" and e["data"]["phase"] == "end"]
    assert ends and ends[0]["tool"] == "calculator" and ends[0]["output"] == "1024.0"
    final = events[-1]["data"]
    assert final["content"] == "It is 1024" and final["status"] == "ok"
    assert final["id"] == approval_evt["message_id"]

    r = await client.get(f"{P}/sessions/{sid}/messages", headers=auth_headers(acct))
    items = r.json()["items"]
    assert [m["role"] for m in items] == ["user", "tool", "assistant"]
    assert items[1]["content"] == "1024.0" and items[1]["meta"]["status"] == "ok"
    assert items[1]["meta"]["call_id"] == "call_x"
    assert items[2]["meta"]["usage"]["prompt_tokens"] > 0

    r = await client.get(
        f"{P}/sessions/{sid}/approvals", params={"status": "pending"}, headers=auth_headers(acct)
    )
    assert r.json()["items"] == []


async def test_step_budget_forces_an_answer(client, db, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MAX_STEPS", 1)
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    script_responses(
        tool_call_message("calculator", {"expression": "1+1"}),
        tool_call_message("calculator", {"expression": "2+2"}, call_id="call_2"),  # would exceed the budget
    )
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "add things", "use_rag": False, "use_tools": True, "tool_names": ["calculator"]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Only the first tool call ran; the second was stripped by the budget guard.
    assert len(body["tool_messages"]) == 1
    rows = (
        (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == UUID(sid), ChatMessage.role == MessageRole.TOOL
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_tools_list_and_unknown_approval(client):
    acct = await register_and_login(client)
    r = await client.get(f"{P}/tools", headers=auth_headers(acct))
    tools = {t["name"]: t for t in r.json()["items"]}
    assert tools["fetch_url"]["requires_approval"] is True
    assert tools["calculator"]["requires_approval"] is False
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/approvals/{UUID(int=1)}",
        json={"decision": "approve"},
        headers=auth_headers(acct),
    )
    assert r.status_code == 404
