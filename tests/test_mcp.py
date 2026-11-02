"""Model Context Protocol: server registration, discovery, agent calls, approvals."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import update

from app.ai.fake import script_responses, tool_call_message
from app.ai.mcp.client import MCPToolSpec, json_schema_to_model, split_full_name
from app.models.user import User
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"
ECHO_SERVER = str(Path(__file__).resolve().parent / "mcp_echo_server.py")


async def _admin(client, db) -> dict:
    acct = await register_and_login(client)
    await db.execute(update(User).where(User.id == acct["user"]["id"]).values(role="admin"))
    await db.commit()
    return acct


def _echo_payload(**overrides) -> dict:
    return {
        "name": "echo",
        "transport": "stdio",
        "command": sys.executable,
        "args": [ECHO_SERVER],
        "env": {"SECRET_TOKEN": "hunter2"},
        "requires_approval": False,
        **overrides,
    }


def test_json_schema_to_model_and_names():
    model = json_schema_to_model(
        "mcp:echo:shout",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "words"},
                "count": {"type": "integer"},
                "flag": {"type": ["boolean", "null"]},
            },
            "required": ["text"],
        },
    )
    obj = model(text="hi")
    assert obj.text == "hi" and obj.count is None and obj.flag is None
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        model(count=1)
    assert split_full_name("mcp:echo:shout") == ("echo", "shout")
    assert split_full_name("calculator") is None and split_full_name("mcp:echo") is None
    spec = MCPToolSpec(server="echo", name="shout", description="d", input_schema={})
    assert spec.full_name == "mcp:echo:shout" and spec.to_public()["requires_approval"] is True


async def test_register_test_and_list_mcp_server(client, db):
    admin = await _admin(client, db)
    member = await register_and_login(client)

    # Non-admins cannot register stdio servers (they run commands on the host).
    r = await client.post(f"{P}/mcp/servers", json=_echo_payload(), headers=auth_headers(member))
    assert r.status_code == 403
    r = await client.post(
        f"{P}/mcp/servers", json=_echo_payload(name="Bad Name"), headers=auth_headers(admin)
    )
    assert r.status_code == 422
    r = await client.post(
        f"{P}/mcp/servers", json=_echo_payload(transport="http", url=None), headers=auth_headers(admin)
    )
    assert r.status_code == 422

    r = await client.post(f"{P}/mcp/servers", json=_echo_payload(), headers=auth_headers(admin))
    assert r.status_code == 201, r.text
    server = r.json()
    assert server["name"] == "echo" and server["env_keys"] == ["SECRET_TOKEN"] and "hunter2" not in r.text
    r = await client.post(f"{P}/mcp/servers", json=_echo_payload(), headers=auth_headers(admin))
    assert r.status_code == 409

    r = await client.post(f"{P}/mcp/servers/{server['id']}/test", headers=auth_headers(admin))
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["ok"] is True and result["error"] is None
    assert {t["name"] for t in result["tools"]} == {"mcp:echo:shout", "mcp:echo:add"}
    shout = next(t for t in result["tools"] if t["tool"] == "shout")
    assert (
        shout["input_schema"]["properties"]["text"]["type"] == "string"
        and shout["requires_approval"] is False
    )

    r = await client.get(f"{P}/tools", headers=auth_headers(admin))
    names = {t["name"]: t for t in r.json()["items"]}
    assert "mcp:echo:add" in names and names["mcp:echo:add"]["source"] == "mcp:echo"
    assert names["calculator"]["source"] == "builtin"
    # Another org (the member's personal org) sees no MCP tools.
    r = await client.get(f"{P}/tools", headers=auth_headers(member))
    assert not any(t["name"].startswith("mcp:") for t in r.json()["items"])

    r = await client.patch(
        f"{P}/mcp/servers/{server['id']}", json={"enabled": False}, headers=auth_headers(admin)
    )
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = await client.get(f"{P}/tools", headers=auth_headers(admin))
    assert not any(t["name"].startswith("mcp:") for t in r.json()["items"])
    r = await client.delete(f"{P}/mcp/servers/{server['id']}", headers=auth_headers(admin))
    assert r.status_code == 200
    r = await client.get(f"{P}/mcp/servers", headers=auth_headers(admin))
    assert r.json()["items"] == []

    # A broken command is reported, not raised.
    r = await client.post(
        f"{P}/mcp/servers",
        json=_echo_payload(name="broken", command="/nonexistent/binary"),
        headers=auth_headers(admin),
    )
    assert r.status_code == 201
    r = await client.post(f"{P}/mcp/servers/{r.json()['id']}/test", headers=auth_headers(admin))
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["error"]


async def test_agent_calls_mcp_tool(client, db):
    admin = await _admin(client, db)
    r = await client.post(f"{P}/mcp/servers", json=_echo_payload(), headers=auth_headers(admin))
    assert r.status_code == 201, r.text
    r = await client.post(f"{P}/sessions", json={"title": "mcp"}, headers=auth_headers(admin))
    sid = r.json()["id"]

    script_responses(tool_call_message("mcp:echo:shout", {"text": "hello"}), "The server says HELLO!")
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={
            "content": "shout hello",
            "use_rag": False,
            "use_tools": True,
            "tool_names": ["mcp:echo:shout"],
        },
        headers=auth_headers(admin),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approval"] is None and body["assistant_message"]["content"] == "The server says HELLO!"
    assert len(body["tool_messages"]) == 1
    tool_msg = body["tool_messages"][0]
    assert tool_msg["meta"]["tool"] == "mcp:echo:shout" and tool_msg["content"] == "HELLO!"
    assert body["tool_calls"][0]["input"] == {"text": "hello"}

    # Unknown MCP tools are rejected up front; so are tools of servers in other orgs.
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "x", "use_rag": False, "use_tools": True, "tool_names": ["mcp:echo:nope"]},
        headers=auth_headers(admin),
    )
    assert r.status_code == 422 and r.json()["error"]["details"]["unknown"] == ["mcp:echo:nope"]
    other = await register_and_login(client)
    r = await client.post(f"{P}/sessions", json={}, headers=auth_headers(other))
    r = await client.post(
        f"{P}/sessions/{r.json()['id']}/messages",
        json={"content": "x", "use_rag": False, "use_tools": True, "tool_names": ["mcp:echo:shout"]},
        headers=auth_headers(other),
    )
    assert r.status_code == 422

    # Assistants may reference MCP tools of their org.
    r = await client.post(
        f"{P}/assistants",
        json={"name": "Shouter", "use_tools": True, "tool_names": ["mcp:echo:add"]},
        headers=auth_headers(admin),
    )
    assert r.status_code == 201, r.text


async def test_mcp_tool_with_approval_streams(client, db):
    admin = await _admin(client, db)
    r = await client.post(
        f"{P}/mcp/servers", json=_echo_payload(requires_approval=True), headers=auth_headers(admin)
    )
    assert r.status_code == 201, r.text
    r = await client.post(f"{P}/sessions", json={"title": "mcp approve"}, headers=auth_headers(admin))
    sid = r.json()["id"]
    script_responses(tool_call_message("mcp:echo:add", {"a": 2, "b": 3}), "2 + 3 = 5")
    r = await client.post(
        f"{P}/sessions/{sid}/messages/stream",
        json={"content": "add 2 and 3", "use_rag": False, "use_tools": True, "tool_names": ["mcp:echo:add"]},
        headers={**auth_headers(admin), "Accept": "text/event-stream"},
    )
    frames = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    approval = next(f for f in frames if f["type"] == "approval_required")["data"]
    assert approval["tool_calls"][0]["tool"] == "mcp:echo:add"
    r = await client.post(
        f"{P}/sessions/{sid}/approvals/{approval['approval_id']}",
        json={"decision": "approve"},
        headers={**auth_headers(admin), "Accept": "text/event-stream"},
    )
    frames = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    tool_end = next(f for f in frames if f["type"] == "tool_call" and f["data"].get("phase") == "end")["data"]
    assert tool_end["output"] == "5"
    assert frames[-1]["data"]["content"] == "2 + 3 = 5"
