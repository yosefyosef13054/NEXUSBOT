"""Message tree: edits, regenerations as siblings, branch switching, active-branch history."""

from __future__ import annotations

import json

import pytest

from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"


def _frames(body: str) -> list[dict]:
    out = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


async def _session(client, acct) -> str:
    r = await client.post(f"{P}/sessions", json={"title": "tree"}, headers=auth_headers(acct))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _turn(client, acct, sid: str, content: str) -> dict:
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": content, "use_rag": False},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _history(client, acct, sid: str) -> list[dict]:
    r = await client.get(f"{P}/sessions/{sid}/messages", headers=auth_headers(acct))
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_edit_creates_branch_and_switching_restores_old_one(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    t1 = await _turn(client, acct, sid, "first")
    t2 = await _turn(client, acct, sid, "second")
    m1, a1 = t1["user_message"], t1["assistant_message"]
    m2, a2 = t2["user_message"], t2["assistant_message"]
    assert m1["parent_id"] is None and a1["parent_id"] == m1["id"]
    assert m2["parent_id"] == a1["id"] and a2["parent_id"] == m2["id"]
    history = await _history(client, acct, sid)
    assert [m["id"] for m in history] == [m1["id"], a1["id"], m2["id"], a2["id"]]
    assert all(m["siblings"] is None for m in history)
    r = await client.get(f"{P}/sessions/{sid}", headers=auth_headers(acct))
    assert r.json()["active_leaf_id"] == a2["id"]

    # Edit the second user message: a sibling branch, answered, now active.
    r = await client.post(
        f"{P}/sessions/{sid}/messages/{m2['id']}/edit",
        json={"content": "second (edited)", "use_rag": False},
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200, r.text
    frames = _frames(r.text)
    assert frames[0]["type"] == "user_message" and frames[0]["data"]["parent_id"] == a1["id"]
    assert frames[0]["data"]["content"] == "second (edited)"
    final = frames[-1]
    assert final["type"] == "assistant_message" and "second (edited)" in final["data"]["content"]
    m2b, a2b = frames[0]["data"]["id"], final["data"]["id"]

    history = await _history(client, acct, sid)
    assert [m["id"] for m in history] == [m1["id"], a1["id"], m2b, a2b]
    edited = history[2]
    assert edited["siblings"] == {"index": 1, "count": 2}
    assert history[3]["siblings"] is None

    r = await client.get(f"{P}/sessions/{sid}/messages/{m2b}/siblings", headers=auth_headers(acct))
    assert r.status_code == 200
    sibs = r.json()
    assert [m["id"] for m in sibs["items"]] == [m2["id"], m2b] and sibs["active_index"] == 1
    assert sibs["items"][0]["siblings"] == {"index": 0, "count": 2}

    # Switch back to the original branch (its deepest descendant becomes the leaf).
    r = await client.post(
        f"{P}/sessions/{sid}/branch", json={"leaf_message_id": m2["id"]}, headers=auth_headers(acct)
    )
    assert r.status_code == 200, r.text
    assert [m["id"] for m in r.json()["items"]] == [m1["id"], a1["id"], m2["id"], a2["id"]]
    r = await client.get(f"{P}/sessions/{sid}", headers=auth_headers(acct))
    assert r.json()["active_leaf_id"] == a2["id"]

    # New turns continue the active branch.
    t3 = await _turn(client, acct, sid, "third")
    assert t3["user_message"]["parent_id"] == a2["id"]
    history = await _history(client, acct, sid)
    assert [m["id"] for m in history][-2:] == [t3["user_message"]["id"], t3["assistant_message"]["id"]]

    # Guards.
    r = await client.post(
        f"{P}/sessions/{sid}/messages/{a1['id']}/edit", json={"content": "x"}, headers=auth_headers(acct)
    )
    assert '"code": "validation_error"' in r.text or r.status_code == 422
    r = await client.post(
        f"{P}/sessions/{sid}/branch", json={"leaf_message_id": sid}, headers=auth_headers(acct)
    )
    assert r.status_code == 404
    other = await register_and_login(client)
    r = await client.get(f"{P}/sessions/{sid}/messages/{m2b}/siblings", headers=auth_headers(other))
    assert r.status_code == 404


async def test_regenerate_keeps_previous_reply_as_sibling(client):
    acct = await register_and_login(client)
    sid = await _session(client, acct)
    t1 = await _turn(client, acct, sid, "hello")
    m1, a1 = t1["user_message"], t1["assistant_message"]

    r = await client.post(f"{P}/sessions/{sid}/messages/regenerate", json={}, headers=auth_headers(acct))
    assert r.status_code == 200, r.text
    a1b = r.json()["assistant_message"]
    assert a1b["id"] != a1["id"] and a1b["parent_id"] == m1["id"]
    history = await _history(client, acct, sid)
    assert [m["id"] for m in history] == [m1["id"], a1b["id"]]
    assert history[1]["siblings"] == {"index": 1, "count": 2}

    r = await client.post(
        f"{P}/sessions/{sid}/messages/regenerate/stream",
        json={},
        headers={**auth_headers(acct), "Accept": "text/event-stream"},
    )
    assert r.status_code == 200, r.text
    frames = _frames(r.text)
    assert frames[0]["type"] == "user_message" and frames[0]["data"]["id"] == m1["id"]
    final = frames[-1]
    assert final["type"] == "assistant_message" and final["data"]["parent_id"] == m1["id"]
    a1c = final["data"]["id"]
    history = await _history(client, acct, sid)
    assert [m["id"] for m in history] == [m1["id"], a1c]
    assert history[1]["siblings"] == {"index": 2, "count": 3}

    r = await client.get(f"{P}/sessions/{sid}/messages/{a1['id']}/siblings", headers=auth_headers(acct))
    assert [m["id"] for m in r.json()["items"]] == [a1["id"], a1b["id"], a1c] and r.json()[
        "active_index"
    ] == 2

    # Pick the first reply again, then continue the conversation from there.
    r = await client.post(
        f"{P}/sessions/{sid}/branch", json={"leaf_message_id": a1["id"]}, headers=auth_headers(acct)
    )
    assert [m["id"] for m in r.json()["items"]] == [m1["id"], a1["id"]]
    t2 = await _turn(client, acct, sid, "more")
    assert t2["user_message"]["parent_id"] == a1["id"]
