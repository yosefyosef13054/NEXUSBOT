"""Request ids, metrics, selective gzip and CORS against the real app."""

from __future__ import annotations

import pytest

from app.config import settings
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"


async def test_request_id_in_headers_and_error_envelope(client):
    r = await client.get("/")
    assert len(r.headers["x-request-id"]) == 32
    r = await client.get(f"{P}/sessions", headers={"X-Request-Id": "abc-123"})
    assert r.status_code == 401
    assert r.headers["x-request-id"] == "abc-123"
    assert r.json()["request_id"] == "abc-123"
    assert r.json()["error"]["code"] == "unauthorized"


async def test_metrics_endpoint_exposes_request_counters(client):
    await client.get(f"{P}/healthz")
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "nexusbot_http_requests_total" in r.text
    assert 'route="/api/v1/healthz"' in r.text


async def test_sse_response_is_never_gzipped(client):
    acct = await register_and_login(client)
    r = await client.post(f"{P}/sessions", json={}, headers=auth_headers(acct))
    sid = r.json()["id"]
    r = await client.post(
        f"{P}/sessions/{sid}/messages/stream",
        json={"content": "hi", "use_rag": False},
        headers={**auth_headers(acct), "Accept": "text/event-stream", "Accept-Encoding": "gzip"},
    )
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.text.startswith(": connected")


async def test_large_json_is_gzipped(client):
    r = await client.get(f"{P}/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


async def test_readiness_probe(client):
    r = await client.get(f"{P}/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_wildcard_origins_disable_credentials():
    assert settings.ALLOWED_ORIGINS == ["*"]
    assert settings.cors_allow_credentials is False
