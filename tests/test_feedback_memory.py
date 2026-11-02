"""Message feedback and long-term memory (CRUD, recall into the prompt, extraction)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.ai.fake import script_responses
from app.config import settings
from app.models.feedback import MessageFeedback
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"


async def _turn(client, acct, sid: str, content: str) -> dict:
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": content, "use_rag": False},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _session(client, acct, **fields) -> str:
    r = await client.post(f"{P}/sessions", json=fields, headers=auth_headers(acct))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_feedback_upsert_delete_and_admin_listing(client, db):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    body = await _turn(client, acct, sid, "hello")
    mid = body["assistant_message"]["id"]
    url = f"{P}/sessions/{sid}/messages/{mid}/feedback"

    r = await client.post(url, json={"rating": -1, "comment": "too short"}, headers=auth_headers(acct))
    assert r.status_code == 200, r.text
    assert r.json()["rating"] == -1 and r.json()["comment"] == "too short"
    r = await client.post(url, json={"rating": 1}, headers=auth_headers(acct))
    assert r.status_code == 200 and r.json()["rating"] == 1 and r.json()["comment"] is None
    rows = (await db.execute(select(MessageFeedback))).scalars().all()
    assert len(rows) == 1 and rows[0].rating == 1

    r = await client.get(f"{P}/sessions/{sid}/messages", headers=auth_headers(acct))
    assistant = next(m for m in r.json()["items"] if m["id"] == mid)
    assert assistant["meta"]["feedback"] == {"rating": 1, "comment": None}

    r = await client.post(url, json={"rating": 2}, headers=auth_headers(acct))
    assert r.status_code == 422
    user_mid = body["user_message"]["id"]
    r = await client.post(
        f"{P}/sessions/{sid}/messages/{user_mid}/feedback", json={"rating": 1}, headers=auth_headers(acct)
    )
    assert r.status_code == 404
    other = await register_and_login(client)
    r = await client.post(url, json={"rating": 1}, headers=auth_headers(other))
    assert r.status_code == 404

    r = await client.delete(url, headers=auth_headers(acct))
    assert r.status_code == 200
    r = await client.get(f"{P}/sessions/{sid}/messages", headers=auth_headers(acct))
    assert "feedback" not in next(m for m in r.json()["items"] if m["id"] == mid)["meta"]
    assert (await db.execute(select(MessageFeedback))).scalars().all() == []


async def test_memory_crud_and_profile_toggle(client):
    acct = await register_and_login(client)
    r = await client.post(
        f"{P}/memories",
        json={"content": "Prefers concise answers", "kind": "preference", "importance": 4},
        headers=auth_headers(acct),
    )
    assert r.status_code == 201, r.text
    mem = r.json()
    assert mem["kind"] == "preference" and mem["importance"] == 4
    # Near-duplicates (identical here) are rejected.
    r = await client.post(
        f"{P}/memories", json={"content": "Prefers concise answers"}, headers=auth_headers(acct)
    )
    assert r.status_code == 409
    r = await client.post(f"{P}/memories", json={"content": "Lives in Haifa"}, headers=auth_headers(acct))
    assert r.status_code == 201
    r = await client.get(f"{P}/memories", headers=auth_headers(acct))
    assert r.json()["total"] == 2
    r = await client.patch(
        f"{P}/memories/{mem['id']}",
        json={"importance": 5, "content": "Prefers very concise answers"},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200 and r.json()["importance"] == 5 and "very" in r.json()["content"]

    other = await register_and_login(client)
    r = await client.get(f"{P}/memories", headers=auth_headers(other))
    assert r.json()["total"] == 0
    r = await client.delete(f"{P}/memories/{mem['id']}", headers=auth_headers(other))
    assert r.status_code == 404

    r = await client.delete(f"{P}/memories/{mem['id']}", headers=auth_headers(acct))
    assert r.status_code == 200
    r = await client.delete(f"{P}/memories", headers=auth_headers(acct))
    assert r.status_code == 200
    r = await client.get(f"{P}/memories", headers=auth_headers(acct))
    assert r.json()["total"] == 0

    r = await client.patch(f"{P}/auth/me", json={"memory_enabled": False}, headers=auth_headers(acct))
    assert r.status_code == 200 and r.json()["memory_enabled"] is False
    r = await client.get(f"{P}/auth/me", headers=auth_headers(acct))
    assert r.json()["memory_enabled"] is False


async def test_memories_are_recalled_into_turns(client, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    acct = await register_and_login(client)
    r = await client.post(
        f"{P}/memories", json={"content": "The user's name is Dana"}, headers=auth_headers(acct)
    )
    mem_id = r.json()["id"]
    sid = await _session(client, acct, title="memory test")
    body = await _turn(client, acct, sid, "What is my name?")
    assert body["assistant_message"]["meta"]["memories_used"] == [mem_id]
    r = await client.get(f"{P}/memories", headers=auth_headers(acct))
    assert r.json()["items"][0]["last_used_at"] is not None

    # Turning memory off for the user stops recall (and extraction).
    await client.patch(f"{P}/auth/me", json={"memory_enabled": False}, headers=auth_headers(acct))
    body = await _turn(client, acct, sid, "And now?")
    assert body["assistant_message"]["meta"]["memories_used"] == []


async def test_memory_extraction_after_turns(client, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLED", True)
    acct = await register_and_login(client)
    sid = await _session(client, acct, title="extraction test")  # explicit title: no title generation call
    script_responses(
        "Nice to meet you, Dana!",
        '[{"content": "User is named Dana", "kind": "fact", "importance": 4}, '
        '{"content": "Likes answers in bullet points", "kind": "preference", "importance": 3}]',
    )
    body = await _turn(client, acct, sid, "Hi, I am Dana and I like bullet points")
    assert body["assistant_message"]["content"] == "Nice to meet you, Dana!"
    r = await client.get(f"{P}/memories", headers=auth_headers(acct))
    items = r.json()["items"]
    assert {(m["content"], m["kind"]) for m in items} == {
        ("User is named Dana", "fact"),
        ("Likes answers in bullet points", "preference"),
    }
    assert all(m["source_message_id"] == body["assistant_message"]["id"] for m in items)

    # A reply the model marks as nothing-to-remember stores nothing.
    script_responses("ok", "[]")
    await _turn(client, acct, sid, "thanks")
    r = await client.get(f"{P}/memories", headers=auth_headers(acct))
    assert r.json()["total"] == 2
