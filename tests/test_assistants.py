"""Assistants (personas): org scoping, defaults applied to turns, edit permissions."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.services import chat_service
from app.services.chat_service import TurnScope
from app.services.session_service import get_session
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"


async def _assistant(client, acct, **fields) -> dict:
    payload = {"name": "Support bot", "use_rag": False, **fields}
    r = await client.post(f"{P}/assistants", json=payload, headers=auth_headers(acct))
    assert r.status_code == 201, r.text
    return r.json()


async def test_assistant_crud_default_exclusivity_and_validation(client):
    acct = await register_and_login(client)
    a = await _assistant(client, acct, system_prompt="You are Bob.", avatar="🤖", is_default=True)
    assert a["org_id"] == acct["user"]["personal_org_id"] and a["is_default"] is True
    assert a["created_by"] == acct["user"]["id"]

    b = await _assistant(client, acct, name="Second", is_default=True)
    r = await client.get(f"{P}/assistants", headers=auth_headers(acct))
    items = {i["id"]: i for i in r.json()["items"]}
    assert r.json()["total"] == 2
    assert items[b["id"]]["is_default"] is True and items[a["id"]]["is_default"] is False

    r = await client.patch(
        f"{P}/assistants/{a['id']}", json={"temperature": 0.1, "name": "Bob"}, headers=auth_headers(acct)
    )
    assert r.status_code == 200 and r.json()["temperature"] == 0.1 and r.json()["name"] == "Bob"
    r = await client.patch(f"{P}/assistants/{a['id']}", json={"unknown": 1}, headers=auth_headers(acct))
    assert r.status_code == 422
    r = await client.post(
        f"{P}/assistants", json={"name": "bad", "model": "no-such-model"}, headers=auth_headers(acct)
    )
    assert r.status_code == 422
    r = await client.post(
        f"{P}/assistants",
        json={"name": "bad", "use_tools": True, "tool_names": ["nope"]},
        headers=auth_headers(acct),
    )
    assert r.status_code == 422

    r = await client.delete(f"{P}/assistants/{a['id']}", headers=auth_headers(acct))
    assert r.status_code == 200
    r = await client.get(f"{P}/assistants/{a['id']}", headers=auth_headers(acct))
    assert r.status_code == 404

    # Other users' personal orgs never see it.
    other = await register_and_login(client)
    r = await client.get(f"{P}/assistants", headers=auth_headers(other))
    assert r.json()["total"] == 0
    r = await client.get(f"{P}/assistants/{b['id']}", headers=auth_headers(other))
    assert r.status_code == 404


async def test_assistant_defaults_apply_to_turns_and_requests_override(client, db):
    acct = await register_and_login(client)
    a = await _assistant(client, acct, system_prompt="You are Bob.", use_rag=False, temperature=0.2)
    r = await client.post(f"{P}/sessions", json={"assistant_id": a["id"]}, headers=auth_headers(acct))
    assert r.status_code == 201 and r.json()["assistant_id"] == a["id"]
    sid = r.json()["id"]

    # Defaults come from the assistant when the request does not set the field…
    r = await client.post(f"{P}/sessions/{sid}/messages", json={"content": "hi"}, headers=auth_headers(acct))
    assert r.status_code == 200, r.text
    gen = r.json()["assistant_message"]["meta"]["generation"]
    assert gen["use_rag"] is False and gen["temperature"] == 0.2
    # …and the request wins when it does.
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "hi", "use_rag": True, "temperature": 0.9},
        headers=auth_headers(acct),
    )
    gen = r.json()["assistant_message"]["meta"]["generation"]
    assert gen["use_rag"] is True and gen["temperature"] == 0.9

    # The assistant's system prompt is what the model sees (session prompt would win over it).
    session_obj = await get_session(db, user_id=UUID(acct["user"]["id"]), session_id=UUID(sid))
    turn = await chat_service.resolve_turn_settings(
        db,
        scope=TurnScope(user_id=UUID(acct["user"]["id"])),
        session_obj=session_obj,
        use_rag=True,
        use_tools=False,
        tool_names=None,
        model=None,
        collection_ids=None,
        temperature=None,
        max_tokens=None,
        explicit=set(),
    )
    assert turn.system_prompt == "You are Bob." and turn.use_rag is False and turn.temperature == 0.2

    # Switching the session to no assistant restores plain defaults.
    r = await client.patch(f"{P}/sessions/{sid}", json={"assistant_id": None}, headers=auth_headers(acct))
    assert r.status_code == 200 and r.json()["assistant_id"] is None
    r = await client.post(f"{P}/sessions/{sid}/messages", json={"content": "hi"}, headers=auth_headers(acct))
    assert r.json()["assistant_message"]["meta"]["generation"]["use_rag"] is True

    # An assistant from another org cannot be attached.
    other = await register_and_login(client)
    r = await client.post(f"{P}/sessions", json={"assistant_id": a["id"]}, headers=auth_headers(other))
    assert r.status_code in (404, 422)


async def test_assistant_edit_requires_creator_or_org_admin(client):
    owner = await register_and_login(client)
    member = await register_and_login(client)
    r = await client.post(f"{P}/orgs", json={"name": "Team"}, headers=auth_headers(owner))
    org = r.json()
    r = await client.post(
        f"{P}/orgs/{org['id']}/members", json={"email": member["email"]}, headers=auth_headers(owner)
    )
    assert r.status_code == 201
    h_owner = {**auth_headers(owner), "X-Org-Id": org["id"]}
    h_member = {**auth_headers(member), "X-Org-Id": org["id"]}

    r = await client.post(f"{P}/assistants", json={"name": "Owner's"}, headers=h_owner)
    owners = r.json()
    r = await client.post(f"{P}/assistants", json={"name": "Member's"}, headers=h_member)
    members = r.json()

    # Everyone in the org can use both; only the creator or an admin can change them.
    r = await client.get(f"{P}/assistants", headers=h_member)
    assert r.json()["total"] == 2
    r = await client.patch(f"{P}/assistants/{owners['id']}", json={"name": "x"}, headers=h_member)
    assert r.status_code == 403
    r = await client.delete(f"{P}/assistants/{owners['id']}", headers=h_member)
    assert r.status_code == 403
    r = await client.patch(
        f"{P}/assistants/{members['id']}", json={"name": "renamed by owner"}, headers=h_owner
    )
    assert r.status_code == 200
    r = await client.patch(
        f"{P}/assistants/{members['id']}", json={"name": "renamed by creator"}, headers=h_member
    )
    assert r.status_code == 200
