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


def _parse_sse(lines: list[str]) -> list[tuple[int | None, dict]]:
    """(id, event) pairs from raw SSE lines."""
    out: list[tuple[int | None, dict]] = []
    current_id: int | None = None
    for line in lines:
        if line.startswith("id:"):
            current_id = int(line[3:].strip())
        elif line.startswith("data:"):
            out.append((current_id, json.loads(line[5:])))
        elif not line.strip():
            current_id = None
    return out


async def test_sse_client_disconnect_then_resume_via_generation_events(live_server):
    """Dropping the SSE connection no longer cancels the turn: the client re-attaches with
    `Last-Event-ID` and receives exactly the frames it missed."""
    acct = await _account(live_server)
    seen: list[tuple[int | None, dict]] = []
    async with httpx.AsyncClient(base_url=live_server, timeout=30, trust_env=False) as c:
        async with c.stream(
            "POST",
            f"/api/v1/sessions/{acct['session_id']}/messages/stream",
            json={"content": LONG_INPUT, "use_rag": False},
            headers={**acct["headers"], "Accept": "text/event-stream"},
        ) as r:
            assert r.status_code == 200
            buffer: list[str] = []
            async for line in r.aiter_lines():
                buffer.append(line)
                seen = _parse_sse(buffer)
                if sum(1 for _, e in seen if e["type"] == "token") >= 5:
                    break  # closes the connection on exit of the context manager
    first_id, first = seen[0]
    assert first["type"] == "user_message" and first_id == 1
    generation_id = first["data"]["generation_id"]
    last_id = seen[-1][0]
    assert last_id is not None and last_id >= 6

    async with httpx.AsyncClient(base_url=live_server, timeout=30, trust_env=False) as c:
        active = await c.get(
            f"/api/v1/sessions/{acct['session_id']}/generations/active", headers=acct["headers"]
        )
        assert active.status_code == 200
        assert active.json() is None or active.json()["generation_id"] == generation_id

        async with c.stream(
            "GET",
            f"/api/v1/sessions/{acct['session_id']}/generations/{generation_id}/events",
            headers={**acct["headers"], "Accept": "text/event-stream", "Last-Event-ID": str(last_id)},
        ) as r:
            assert r.status_code == 200
            resumed = _parse_sse([line async for line in r.aiter_lines()])
    ids = [i for i, _ in resumed]
    assert ids and ids[0] == last_id + 1 and ids == sorted(ids)
    final = resumed[-1][1]
    assert final["type"] == "assistant_message" and final["data"]["interrupted"] is False
    assert final["data"]["generation_id"] == generation_id

    msg = await _wait_for_assistant(live_server, acct)
    assert msg is not None and msg["meta"]["status"] == "ok"
    assert "You said:" in msg["content"]

    async with httpx.AsyncClient(base_url=live_server, timeout=30, trust_env=False) as c:
        active = await c.get(
            f"/api/v1/sessions/{acct['session_id']}/generations/active", headers=acct["headers"]
        )
        assert active.json() is None
        r = await c.get(
            f"/api/v1/sessions/{acct['session_id']}/generations/nope/events", headers=acct["headers"]
        )
        assert r.status_code == 404


async def test_sse_disconnect_then_stop_endpoint_persists_partial_reply(live_server):
    acct = await _account(live_server)
    generation_id = None
    async with httpx.AsyncClient(base_url=live_server, timeout=30, trust_env=False) as c:
        async with c.stream(
            "POST",
            f"/api/v1/sessions/{acct['session_id']}/messages/stream",
            json={"content": LONG_INPUT, "use_rag": False},
            headers={**acct["headers"], "Accept": "text/event-stream"},
        ) as r:
            tokens = 0
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    evt = json.loads(line[5:])
                    if evt["type"] == "user_message":
                        generation_id = evt["data"]["generation_id"]
                    elif evt["type"] == "token":
                        tokens += 1
                        if tokens >= 3:
                            break
        r = await c.post(
            f"/api/v1/sessions/{acct['session_id']}/generations/{generation_id}/stop", headers=acct["headers"]
        )
        assert r.status_code == 200, r.text
    msg = await _wait_for_assistant(live_server, acct)
    assert msg is not None, "partial reply was not persisted after stop"
    assert msg["meta"]["status"] == "interrupted" and msg["meta"]["interrupt_reason"] == "stop"
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
