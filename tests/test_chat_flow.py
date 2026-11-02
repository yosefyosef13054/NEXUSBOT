"""Chat generation against the real app with the deterministic fake model."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import pytest
from sqlalchemy import select

from app.ai.rag.retriever import retrieve_chunks
from app.ai.tools.context import ToolContext
from app.ai.tools.database_query import UserSessionsTool
from app.config import settings
from app.models.chat import ChatMessage, MessageRole
from app.models.usage import AIUsageLog
from app.services import chat_service
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"
LONG_INPUT = " ".join(f"word{i}" for i in range(150))


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


async def test_blocking_message_roundtrip_with_usage_and_title(client, db):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "hello there", "use_rag": False},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "You said: hello there" in body["assistant_message"]["content"]
    assert body["usage"]["prompt_tokens"] > 0 and body["usage"]["completion_tokens"] > 0
    assert body["assistant_message"]["meta"]["status"] == "ok"
    assert body["assistant_message"]["meta"]["generation"]["use_rag"] is False

    # Post-turn maintenance ran inline (BACKGROUND_TASKS_INLINE): a title was generated.
    r = await client.get(f"{P}/sessions/{sid}", headers=auth_headers(acct))
    assert r.json()["title"] != "New chat"

    rows = (await db.execute(select(AIUsageLog).where(AIUsageLog.session_id == UUID(sid)))).scalars().all()
    ops = {row.operation for row in rows}
    assert "chat" in ops
    chat_row = next(row for row in rows if row.operation == "chat")
    assert chat_row.prompt_tokens == body["usage"]["prompt_tokens"]
    assert (
        chat_row.prompt_tokens > chat_row.completion_tokens
    )  # system prompt counted, not just the user text


async def test_sse_stream_event_sequence(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/messages/stream",
        json={"content": "stream me please", "use_rag": False},
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _frames(r.text)
    types = [e["type"] for e in events]
    assert types[0] == "user_message"
    assert types[-1] == "assistant_message"
    assert "done" in types and "usage" in types and types.count("token") > 1
    assert types.index("done") < types.index("assistant_message")
    tokens = "".join(e["data"] for e in events if e["type"] == "token")
    done = next(e for e in events if e["type"] == "done")
    final = events[-1]
    assert tokens.strip() == done["data"]["text"] == final["data"]["content"]
    assert done["data"]["interrupted"] is False and final["data"]["interrupted"] is False
    assert done["data"]["prompt_tokens"] > 0 and done["data"]["completion_tokens"] > 0
    assert final["data"]["usage"]["total_tokens"] > 0

    r = await client.get(f"{P}/sessions/{sid}/messages", headers=auth_headers(acct))
    items = r.json()["items"]
    assert [m["role"] for m in items] == ["user", "assistant"]
    assert items[1]["content"] == final["data"]["content"]


async def test_tools_path_runs_with_fake_model(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "what is 2+2", "use_rag": False, "use_tools": True, "tool_names": ["calculator"]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    assert r.json()["assistant_message"]["content"]
    assert r.json()["assistant_message"]["meta"]["generation"]["use_tools"] is True


async def test_unknown_tool_is_rejected(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "hi", "use_tools": True, "tool_names": ["rm_rf"]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["unknown"] == ["rm_rf"]


async def test_regenerate_replays_previous_settings(client, db):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "first", "use_rag": False},
        headers=auth_headers(acct),
    )
    first_id = r.json()["assistant_message"]["id"]
    r = await client.post(f"{P}/sessions/{sid}/messages/regenerate", json={}, headers=auth_headers(acct))
    assert r.status_code == 200, r.text
    assert r.json()["assistant_message"]["id"] != first_id
    assert r.json()["assistant_message"]["meta"]["generation"]["use_rag"] is False
    assert r.json()["usage"]["prompt_tokens"] > 0
    r = await client.get(f"{P}/sessions/{sid}/messages", headers=auth_headers(acct))
    assert [m["role"] for m in r.json()["items"]] == ["user", "assistant"]


async def test_session_access_is_scoped_to_owner(client):
    a = await register_and_login(client)
    b = await register_and_login(client)
    sid = await _session(client, a)
    r = await client.post(f"{P}/sessions/{sid}/messages", json={"content": "x"}, headers=auth_headers(b))
    assert r.status_code == 404


async def test_db_tool_only_sees_calling_user(client):
    a = await register_and_login(client)
    b = await register_and_login(client)
    sid_a = await _session(client, a)
    await _session(client, b)
    tool = UserSessionsTool(ctx=ToolContext(user_id=UUID(a["user"]["id"])))
    rows = json.loads(await tool._arun(limit=10))
    assert [row["id"] for row in rows] == [sid_a]


async def test_retrieval_skipped_without_documents(client, db):
    acct = await register_and_login(client)
    result = await retrieve_chunks(db, user_id=UUID(acct["user"]["id"]), query="anything")
    assert result.chunks == [] and result.embedded is False and result.query_tokens == 0


async def _collect_until(gen, *, stop_after_tokens: int, action: str, stop_event: asyncio.Event | None):
    events = []
    tokens = 0
    async for evt in gen:
        events.append(evt)
        if evt["type"] == "token":
            tokens += 1
            if tokens == stop_after_tokens:
                if action == "stop":
                    assert stop_event is not None
                    stop_event.set()
                elif action == "close":
                    await gen.aclose()
                    break
    return events


async def test_stop_event_interrupts_and_persists_partial(client, db, monkeypatch):
    monkeypatch.setattr(settings, "AI_FAKE_DELAY_SECONDS", 0.01)
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    stop = asyncio.Event()
    gen = chat_service.stream_message(
        db,
        user_id=UUID(acct["user"]["id"]),
        session_id=UUID(sid),
        content=LONG_INPUT,
        use_rag=False,
        use_tools=False,
        tool_names=None,
        temperature=None,
        max_tokens=None,
        stop_event=stop,
    )
    events = await _collect_until(gen, stop_after_tokens=5, action="stop", stop_event=stop)
    done = next(e for e in events if e["type"] == "done")
    final = events[-1]
    assert done["data"]["interrupted"] is True and done["data"]["interrupt_reason"] == "stop"
    assert final["type"] == "assistant_message" and final["data"]["interrupted"] is True
    assert 0 < len(final["data"]["content"]) < len(LONG_INPUT)

    msg = await db.get(ChatMessage, UUID(final["data"]["id"]))
    assert msg is not None and msg.meta["status"] == "interrupted" and msg.meta["interrupt_reason"] == "stop"


async def test_consumer_close_cancels_model_and_persists_partial(client, db, monkeypatch):
    """Simulates the SSE client disconnecting mid-stream."""
    monkeypatch.setattr(settings, "AI_FAKE_DELAY_SECONDS", 0.01)
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    gen = chat_service.stream_message(
        db,
        user_id=UUID(acct["user"]["id"]),
        session_id=UUID(sid),
        content=LONG_INPUT,
        use_rag=False,
        use_tools=False,
        tool_names=None,
        temperature=None,
        max_tokens=None,
    )
    events = await _collect_until(gen, stop_after_tokens=5, action="close", stop_event=None)
    assert events[-1]["type"] == "token"

    rows = (
        (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.session_id == UUID(sid), ChatMessage.role == MessageRole.ASSISTANT
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    partial = rows[0]
    assert partial.meta["status"] == "interrupted"
    assert partial.meta["interrupt_reason"] == "disconnect"
    assert 0 < len(partial.content) < len(LONG_INPUT)
    usage = (await db.execute(select(AIUsageLog).where(AIUsageLog.session_id == UUID(sid)))).scalars().all()
    assert any(u.operation == "chat_stream" and u.meta.get("status") == "interrupted" for u in usage)


async def test_history_cursor_pagination(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    for i in range(3):
        r = await client.post(
            f"{P}/sessions/{sid}/messages",
            json={"content": f"m{i}", "use_rag": False},
            headers=auth_headers(acct),
        )
        assert r.status_code == 200
    r = await client.get(f"{P}/sessions/{sid}/messages", params={"limit": 4}, headers=auth_headers(acct))
    page1 = r.json()
    assert len(page1["items"]) == 4 and page1["next_cursor"]
    r = await client.get(
        f"{P}/sessions/{sid}/messages",
        params={"limit": 4, "cursor": page1["next_cursor"]},
        headers=auth_headers(acct),
    )
    page2 = r.json()
    assert len(page2["items"]) == 2 and page2["next_cursor"] is None
    assert {m["id"] for m in page1["items"]}.isdisjoint({m["id"] for m in page2["items"]})
