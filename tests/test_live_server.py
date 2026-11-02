"""End-to-end tests over real sockets: SSE disconnect and the WebSocket protocol.

Boots `uvicorn app.main:app` in a subprocess (same test database, fake model with a
small per-token delay) so client-side disconnects and concurrent WS frames behave
exactly as they do in production.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import websockets

from tests.conftest import INFRA, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

LONG_INPUT = " ".join(f"token{i}" for i in range(200))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server():
    if not INFRA:
        pytest.skip("infra unavailable")
    port = _free_port()
    env = {**os.environ, "AI_FAKE_DELAY_SECONDS": "0.03", "LOG_LEVEL": "WARNING"}
    log_path = Path(os.environ.get("UPLOAD_DIR", "/tmp")).parent / "nexusbot-live-server.log"
    log = open(log_path, "wb")  # noqa: SIM115 — closed when the process exits
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server died, see {log_path}")
        try:
            with httpx.Client(trust_env=False, timeout=2) as probe:
                if probe.get(f"{base}/api/v1/healthz").status_code == 200:
                    break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        proc.kill()
        raise RuntimeError("server did not start")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


async def _account(base: str) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as c:
        email = f"live-{time.time_ns()}@example.com"
        r = await c.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
        assert r.status_code == 201, r.text
        r = await c.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
        tokens = r.json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        r = await c.post("/api/v1/sessions", json={}, headers=headers)
        return {"headers": headers, "session_id": r.json()["id"], "access_token": tokens["access_token"]}


async def _wait_for_assistant(base: str, acct: dict, *, timeout: float = 10) -> dict | None:
    async with httpx.AsyncClient(base_url=base, timeout=10, trust_env=False) as c:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = await c.get(f"/api/v1/sessions/{acct['session_id']}/messages", headers=acct["headers"])
            items = [m for m in r.json()["items"] if m["role"] == "assistant"]
            if items:
                return items[-1]
            await asyncio.sleep(0.25)
    return None


async def test_sse_client_disconnect_persists_partial_reply(live_server):
    acct = await _account(live_server)
    seen = 0
    async with httpx.AsyncClient(base_url=live_server, timeout=30, trust_env=False) as c:
        async with c.stream(
            "POST",
            f"/api/v1/sessions/{acct['session_id']}/messages/stream",
            json={"content": LONG_INPUT, "use_rag": False},
            headers={**acct["headers"], "Accept": "text/event-stream"},
        ) as r:
            assert r.status_code == 200
            async for line in r.aiter_lines():
                if line.startswith("data:") and json.loads(line[5:])["type"] == "token":
                    seen += 1
                    if seen >= 5:
                        break  # closes the connection on exit of the context manager
    msg = await _wait_for_assistant(live_server, acct)
    assert msg is not None, "partial reply was not persisted after disconnect"
    assert msg["meta"]["status"] == "interrupted"
    assert msg["meta"]["interrupt_reason"] == "disconnect"
    assert 0 < len(msg["content"]) < len(LONG_INPUT)


async def _recv(ws) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=15))


async def test_websocket_first_frame_auth_stop_and_busy(live_server):
    acct = await _account(live_server)
    url = f"ws://{live_server.removeprefix('http://')}/api/v1/ws/sessions/{acct['session_id']}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "auth", "token": acct["access_token"]}))
        assert (await _recv(ws))["type"] == "auth_ok"

        await ws.send(json.dumps({"type": "ping"}))
        assert (await _recv(ws))["type"] == "pong"

        await ws.send(json.dumps({"type": "user_message", "content": LONG_INPUT, "use_rag": False}))
        tokens = 0
        busy_seen = False
        final = None
        while True:
            evt = await _recv(ws)
            if evt["type"] == "token":
                tokens += 1
                if tokens == 3:
                    await ws.send(json.dumps({"type": "user_message", "content": "again"}))
                if tokens == 6:
                    await ws.send(json.dumps({"type": "stop"}))
            elif evt["type"] == "error" and evt["data"].get("code") == "busy":
                busy_seen = True
            elif evt["type"] == "assistant_message":
                final = evt
                break
        assert busy_seen
        assert final["data"]["interrupted"] is True
        assert 0 < len(final["data"]["content"]) < len(LONG_INPUT)

        # The socket is reusable after a stop.
        await ws.send(json.dumps({"type": "user_message", "content": "short one", "use_rag": False}))
        while (evt := await _recv(ws))["type"] != "assistant_message":
            pass
        assert evt["data"]["interrupted"] is False
        assert "short one" in evt["data"]["content"]


async def test_websocket_rejects_bad_auth(live_server):
    acct = await _account(live_server)
    url = f"ws://{live_server.removeprefix('http://')}/api/v1/ws/sessions/{acct['session_id']}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "user_message", "content": "no auth"}))
        evt = await _recv(ws)
        assert evt["type"] == "error" and evt["data"]["code"] == "unauthorized"
        with pytest.raises(websockets.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), timeout=5)
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "auth", "token": "not-a-jwt"}))
        evt = await _recv(ws)
        assert evt["type"] == "error" and evt["data"]["code"] == "unauthorized"


async def test_websocket_query_string_auth_still_accepted(live_server):
    acct = await _account(live_server)
    url = f"ws://{live_server.removeprefix('http://')}/api/v1/ws/sessions/{acct['session_id']}?token={acct['access_token']}"
    async with websockets.connect(url) as ws:
        assert (await _recv(ws))["type"] == "auth_ok"
