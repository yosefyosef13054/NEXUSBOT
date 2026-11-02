"""GET /models, model validation on sessions/turns, and the user's default model."""

from __future__ import annotations

import pytest

from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"


async def test_models_endpoint_lists_available_models(client):
    acct = await register_and_login(client)
    r = await client.get(f"{P}/models", headers=auth_headers(acct))
    assert r.status_code == 200
    body = r.json()
    assert body["default_model"] == "nexusbot-fake"
    assert [m["id"] for m in body["items"]] == ["nexusbot-fake"]
    item = body["items"][0]
    assert item["provider"] == "fake" and item["is_default"] is True and item["supports_streaming"] is True
    r = await client.get(f"{P}/models")
    assert r.status_code == 401


async def test_session_model_is_validated(client):
    acct = await register_and_login(client)
    r = await client.post(f"{P}/sessions", json={"model": "gpt-4o-mini"}, headers=auth_headers(acct))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["available"] == ["nexusbot-fake"]
    r = await client.post(f"{P}/sessions", json={"model": "nexusbot-fake"}, headers=auth_headers(acct))
    assert r.status_code == 201 and r.json()["model"] == "nexusbot-fake"
    sid = r.json()["id"]
    r = await client.patch(
        f"{P}/sessions/{sid}", json={"model": "claude-opus-5-5"}, headers=auth_headers(acct)
    )
    assert r.status_code == 422
    r = await client.post(
        f"{P}/sessions/{sid}/messages", json={"content": "hi", "model": "gpt-4o"}, headers=auth_headers(acct)
    )
    assert r.status_code == 422
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "hi", "model": "nexusbot-fake", "use_rag": False},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200
    meta = r.json()["assistant_message"]["meta"]
    assert meta["model"] == "nexusbot-fake" and meta["provider"] == "fake"
    assert meta["generation"]["model"] == "nexusbot-fake"


async def test_stream_reports_model(client):
    acct = await register_and_login(client)
    r = await client.post(f"{P}/sessions", json={}, headers=auth_headers(acct))
    sid = r.json()["id"]
    r = await client.post(
        f"{P}/sessions/{sid}/messages/stream",
        json={"content": "hello", "use_rag": False},
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200
    assert '"model": "nexusbot-fake"' in r.text and '"provider": "fake"' in r.text


async def test_user_default_model(client):
    acct = await register_and_login(client)
    r = await client.get(f"{P}/auth/me", headers=auth_headers(acct))
    assert r.json()["default_model"] is None
    r = await client.patch(
        f"{P}/auth/me", json={"default_model": "nexusbot-fake"}, headers=auth_headers(acct)
    )
    assert r.status_code == 200 and r.json()["default_model"] == "nexusbot-fake"
    r = await client.patch(f"{P}/auth/me", json={"default_model": "gpt-4o"}, headers=auth_headers(acct))
    assert r.status_code == 422
    r = await client.patch(f"{P}/auth/me", json={"full_name": "Only Name"}, headers=auth_headers(acct))
    assert r.json()["default_model"] == "nexusbot-fake"  # untouched when the field is absent
    r = await client.patch(f"{P}/auth/me", json={"default_model": None}, headers=auth_headers(acct))
    assert r.json()["default_model"] is None
