"""Generation logs: background turns, replay/follow, explicit stop, idempotency keys."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.chat import ChatMessage, MessageRole
from app.services import chat_service
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"
LONG_INPUT = " ".join(f"word{i}" for i in range(150))


def _frames(body: str) -> list[tuple[int | None, dict]]:
    out: list[tuple[int | None, dict]] = []
    for block in body.split("\n\n"):
        current_id = None
        for line in block.splitlines():
            if line.startswith("id:"):
                current_id = int(line[3:].strip())
            elif line.startswith("data:"):
                out.append((current_id, json.loads(line[5:].strip())))
    return out


async def _session(client, acct) -> str:
    r = await client.post(f"{P}/sessions", json={"title": "gen"}, headers=auth_headers(acct))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _assistant_rows(db, sid: str) -> list[ChatMessage]:
    stmt = select(ChatMessage).where(
        ChatMessage.session_id == UUID(sid), ChatMessage.role == MessageRole.ASSISTANT
    )
    return list((await db.execute(stmt)).scalars().all())


async def test_background_turn_then_attach_and_replay(client, db):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/messages/background",
        json={"content": "run in background", "use_rag": False},
        headers=auth_headers(acct),
    )
    assert r.status_code == 202, r.text
    gid = r.json()["generation_id"]
    assert r.json()["user_message"]["content"] == "run in background"

    r = await client.get(
        f"{P}/sessions/{sid}/generations/{gid}/events",
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200, r.text
    frames = _frames(r.text)
    ids = [i for i, _ in frames]
    assert ids[0] == 1 and ids == sorted(ids) and len(set(ids)) == len(ids)
    assert frames[0][1]["type"] == "user_message" and frames[0][1]["data"]["generation_id"] == gid
    types = [e["type"] for _, e in frames]
    assert "token" in types and types[-2:] == ["done", "assistant_message"]
    final = frames[-1][1]
    assert final["data"]["generation_id"] == gid and "run in background" in final["data"]["content"]

    # Replay from the middle: only later frames, same order.
    cut = ids[len(ids) // 2]
    r = await client.get(
        f"{P}/sessions/{sid}/generations/{gid}/events?after={cut}",
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    tail = _frames(r.text)
    assert [i for i, _ in tail] == [i for i in ids if i > cut]
    r = await client.get(
        f"{P}/sessions/{sid}/generations/{gid}/events",
        headers={**auth_headers(acct), "Accept": "text/event-stream", "Last-Event-ID": str(ids[-1])},
    )
    assert _frames(r.text) == []

    r = await client.get(f"{P}/sessions/{sid}/generations/active", headers=auth_headers(acct))
    assert r.status_code == 200 and r.json() is None
    assert len(await _assistant_rows(db, sid)) == 1

    # Other users (or other sessions) cannot read the log.
    other = await register_and_login(client)
    r = await client.get(f"{P}/sessions/{sid}/generations/{gid}/events", headers=auth_headers(other))
    assert r.status_code == 404
    sid2 = await _session(client, acct)
    r = await client.get(f"{P}/sessions/{sid2}/generations/{gid}/events", headers=auth_headers(acct))
    assert r.status_code == 404
    r = await client.post(f"{P}/sessions/{sid}/generations/{gid}/stop", headers=auth_headers(acct))
    assert r.status_code == 200 and "already finished" in r.json()["message"]


async def test_stop_endpoint_interrupts_background_turn(client, db, monkeypatch):
    monkeypatch.setattr(settings, "AI_FAKE_DELAY_SECONDS", 0.02)
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    r = await client.post(
        f"{P}/sessions/{sid}/messages/background",
        json={"content": LONG_INPUT, "use_rag": False},
        headers=auth_headers(acct),
    )
    gid = r.json()["generation_id"]
    r = await client.get(f"{P}/sessions/{sid}/generations/active", headers=auth_headers(acct))
    assert r.json() and r.json()["generation_id"] == gid and r.json()["status"] == "running"

    await asyncio.sleep(0.1)
    r = await client.post(f"{P}/sessions/{sid}/generations/{gid}/stop", headers=auth_headers(acct))
    assert r.status_code == 200 and r.json()["message"] == "Stop requested"
    events = [evt async for evt in chat_service.follow_generation(gid)]
    done = next(e for e in events if e["type"] == "done")
    assert done["data"]["interrupted"] is True and done["data"]["interrupt_reason"] == "stop"
    final = events[-1]
    assert final["type"] == "assistant_message" and final["data"]["interrupted"] is True
    assert 0 < len(final["data"]["content"]) < len(LONG_INPUT)
    rows = await _assistant_rows(db, sid)
    assert len(rows) == 1 and rows[0].meta["status"] == "interrupted"


async def test_idempotency_key_blocking_stream_and_background(client, db):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    key = str(uuid4())
    h = {**auth_headers(acct), "Idempotency-Key": key}
    r1 = await client.post(
        f"{P}/sessions/{sid}/messages", json={"content": "once", "use_rag": False}, headers=h
    )
    r2 = await client.post(
        f"{P}/sessions/{sid}/messages", json={"content": "once", "use_rag": False}, headers=h
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["assistant_message"]["id"] == r2.json()["assistant_message"]["id"]
    assert r1.json()["user_message"]["id"] == r2.json()["user_message"]["id"]
    assert r2.json()["usage"] == r1.json()["usage"]
    assert len(await _assistant_rows(db, sid)) == 1

    # The key is per user: someone else reusing it gets their own turn.
    other = await register_and_login(client)
    sid_other = await _session(client, other)
    r = await client.post(
        f"{P}/sessions/{sid_other}/messages",
        json={"content": "mine", "use_rag": False},
        headers={**auth_headers(other), "Idempotency-Key": key},
    )
    assert r.status_code == 200 and "mine" in r.json()["assistant_message"]["content"]

    # Streams: the retry replays the original generation (same id, same frames).
    key2 = str(uuid4())
    h2 = {**auth_headers(acct), "Idempotency-Key": key2, "Accept": "text/event-stream"}
    s1 = await client.post(
        f"{P}/sessions/{sid}/messages/stream", json={"content": "streamed", "use_rag": False}, headers=h2
    )
    s2 = await client.post(
        f"{P}/sessions/{sid}/messages/stream", json={"content": "streamed", "use_rag": False}, headers=h2
    )
    f1, f2 = _frames(s1.text), _frames(s2.text)
    assert f1 and f1 == f2
    assert f1[0][1]["data"]["generation_id"] == f2[0][1]["data"]["generation_id"]
    assert len(await _assistant_rows(db, sid)) == 2

    # Background: same generation id on retry.
    key3 = str(uuid4())
    h3 = {**auth_headers(acct), "Idempotency-Key": key3}
    b1 = await client.post(
        f"{P}/sessions/{sid}/messages/background", json={"content": "bg", "use_rag": False}, headers=h3
    )
    b2 = await client.post(
        f"{P}/sessions/{sid}/messages/background", json={"content": "bg", "use_rag": False}, headers=h3
    )
    assert b1.status_code == 202 and b2.status_code == 202
    assert b1.json()["generation_id"] == b2.json()["generation_id"]
    assert b1.json()["user_message"]["id"] == b2.json()["user_message"]["id"]
    # Let the background turn finish before the table is truncated.
    async for _ in chat_service.follow_generation(b1.json()["generation_id"]):
        pass
    assert len(await _assistant_rows(db, sid)) == 3


async def test_failed_attempt_releases_idempotency_key(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    key = str(uuid4())
    h = {**auth_headers(acct), "Idempotency-Key": key}
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "x", "use_rag": False, "model": "no-such-model"},
        headers=h,
    )
    assert r.status_code == 422
    r = await client.post(f"{P}/sessions/{sid}/messages", json={"content": "x", "use_rag": False}, headers=h)
    assert r.status_code == 200, r.text
