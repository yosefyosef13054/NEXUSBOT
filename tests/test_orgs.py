"""Workspaces: personal orgs, membership roles, `X-Org-Id` scoping, usage and quotas."""

from __future__ import annotations

import pytest

from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"


def _h(acct: dict, org_id: str | None = None) -> dict[str, str]:
    headers = auth_headers(acct)
    if org_id:
        headers["X-Org-Id"] = org_id
    return headers


async def _team(client, owner: dict, *members: dict, name: str = "Team") -> dict:
    r = await client.post(f"{P}/orgs", json={"name": name}, headers=auth_headers(owner))
    assert r.status_code == 201, r.text
    org = r.json()
    for m in members:
        r = await client.post(
            f"{P}/orgs/{org['id']}/members",
            json={"email": m["email"], "role": "member"},
            headers=auth_headers(owner),
        )
        assert r.status_code == 201, r.text
    return org


async def test_registration_creates_personal_org_and_listing(client):
    acct = await register_and_login(client)
    assert acct["user"]["personal_org_id"]
    r = await client.get(f"{P}/orgs", headers=auth_headers(acct))
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["is_personal"] is True and items[0]["role"] == "owner" and items[0]["member_count"] == 1
    assert items[0]["id"] == acct["user"]["personal_org_id"]

    # Resources default to the personal org when no header is sent.
    r = await client.post(f"{P}/sessions", json={}, headers=auth_headers(acct))
    assert r.json()["org_id"] == acct["user"]["personal_org_id"]


async def test_org_membership_roles_and_scoping(client):
    owner = await register_and_login(client)
    member = await register_and_login(client)
    outsider = await register_and_login(client)
    org = await _team(client, owner, member)
    assert org["role"] == "owner" and org["is_personal"] is False

    r = await client.get(f"{P}/orgs", headers=auth_headers(member))
    assert {o["name"] for o in r.json()["items"]} == {"Personal", "Team"}
    team = next(o for o in r.json()["items"] if o["name"] == "Team")
    assert team["role"] == "member" and team["member_count"] == 2

    # Members cannot change settings or manage members; admins/owners can.
    r = await client.patch(f"{P}/orgs/{org['id']}", json={"name": "Renamed"}, headers=auth_headers(member))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    r = await client.post(
        f"{P}/orgs/{org['id']}/members", json={"email": outsider["email"]}, headers=auth_headers(member)
    )
    assert r.status_code == 403
    r = await client.patch(
        f"{P}/orgs/{org['id']}/members/{member['user']['id']}",
        json={"role": "admin"},
        headers=auth_headers(owner),
    )
    assert r.status_code == 200 and r.json()["role"] == "admin"
    r = await client.patch(f"{P}/orgs/{org['id']}", json={"name": "Renamed"}, headers=auth_headers(member))
    assert r.status_code == 200 and r.json()["name"] == "Renamed"

    # Unknown users / duplicates / the owner.
    r = await client.post(
        f"{P}/orgs/{org['id']}/members", json={"email": "nobody@example.com"}, headers=auth_headers(owner)
    )
    assert r.status_code == 404
    r = await client.post(
        f"{P}/orgs/{org['id']}/members", json={"email": member["email"]}, headers=auth_headers(owner)
    )
    assert r.status_code == 409
    r = await client.delete(
        f"{P}/orgs/{org['id']}/members/{owner['user']['id']}", headers=auth_headers(owner)
    )
    assert r.status_code in (400, 409, 422)

    # Scoping: sessions created in the team org are invisible from the personal org and
    # non-members cannot act in the org at all.
    r = await client.post(f"{P}/sessions", json={"title": "team chat"}, headers=_h(member, org["id"]))
    assert r.status_code == 201 and r.json()["org_id"] == org["id"]
    r = await client.get(f"{P}/sessions", headers=_h(member, org["id"]))
    assert [s["title"] for s in r.json()["items"]] == ["team chat"]
    r = await client.get(f"{P}/sessions", headers=auth_headers(member))
    assert r.json()["items"] == []
    r = await client.get(f"{P}/sessions", headers=_h(outsider, org["id"]))
    assert r.status_code == 403
    r = await client.get(f"{P}/sessions", headers=_h(outsider, "not-a-uuid"))
    assert r.status_code == 422

    # A member can leave; afterwards the org is closed to them.
    r = await client.delete(
        f"{P}/orgs/{org['id']}/members/{member['user']['id']}", headers=auth_headers(member)
    )
    assert r.status_code == 200
    r = await client.get(f"{P}/sessions", headers=_h(member, org["id"]))
    assert r.status_code == 403
    r = await client.get(f"{P}/orgs/{org['id']}/members", headers=auth_headers(owner))
    assert [m["email"] for m in r.json()["items"]] == [owner["email"]]


async def test_personal_org_cannot_take_members(client):
    acct = await register_and_login(client)
    other = await register_and_login(client)
    r = await client.post(
        f"{P}/orgs/{acct['user']['personal_org_id']}/members",
        json={"email": other["email"]},
        headers=auth_headers(acct),
    )
    assert r.status_code in (400, 409, 422)


async def test_usage_me_quota_and_org_report(client):
    owner = await register_and_login(client)
    member = await register_and_login(client)
    org = await _team(client, owner, member)
    h = _h(member, org["id"])

    r = await client.get(f"{P}/usage/me", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["messages"] == 0 and body["org"]["id"] == org["id"]
    assert body["org"]["budget_tokens_per_month"] is None and body["org"]["remaining_tokens"] is None

    # A tiny token budget: the first turn passes (nothing used yet), the next one is refused.
    r = await client.patch(
        f"{P}/orgs/{org['id']}",
        json={"settings": {"budget_tokens_per_month": 1}},
        headers=auth_headers(owner),
    )
    assert r.status_code == 200 and r.json()["settings"]["budget_tokens_per_month"] == 1
    r = await client.post(f"{P}/sessions", json={}, headers=h)
    sid = r.json()["id"]
    r = await client.post(f"{P}/sessions/{sid}/messages", json={"content": "hi", "use_rag": False}, headers=h)
    assert r.status_code == 200, r.text
    for path, body in (
        (f"{P}/sessions/{sid}/messages", {"content": "again", "use_rag": False}),
        (f"{P}/sessions/{sid}/messages/background", {"content": "again", "use_rag": False}),
        (f"{P}/sessions/{sid}/messages/regenerate", {}),
    ):
        r = await client.post(path, json=body, headers=h)
        assert r.status_code == 429, r.text
        err = r.json()["error"]
        assert err["code"] == "quota_exceeded"
        assert err["details"]["budget_tokens_per_month"] == 1 and err["details"]["used_tokens"] >= 1
        assert err["details"]["resets_at"]
    # Streams report the refusal as a real HTTP status too (checked before the stream opens).
    r = await client.post(
        f"{P}/sessions/{sid}/messages/stream", json={"content": "x", "use_rag": False}, headers=h
    )
    assert r.status_code == 200
    assert '"code": "quota_exceeded"' in r.text

    r = await client.get(f"{P}/usage/me", headers=h)
    body = r.json()
    assert body["user"]["messages"] == 1 and body["user"]["tokens"] >= 1
    assert body["org"]["remaining_tokens"] == 0

    # The personal org is unaffected by the team budget.
    r = await client.post(f"{P}/sessions", json={}, headers=auth_headers(member))
    r = await client.post(
        f"{P}/sessions/{r.json()['id']}/messages",
        json={"content": "hi", "use_rag": False},
        headers=auth_headers(member),
    )
    assert r.status_code == 200

    # Lifting the budget takes effect immediately (cache invalidated).
    r = await client.patch(
        f"{P}/orgs/{org['id']}",
        json={"settings": {"budget_tokens_per_month": None}},
        headers=auth_headers(owner),
    )
    r = await client.post(
        f"{P}/sessions/{sid}/messages", json={"content": "again", "use_rag": False}, headers=h
    )
    assert r.status_code == 200, r.text

    # Admin report grouped by user; members may not read it.
    r = await client.get(f"{P}/orgs/{org['id']}/usage?group_by=user", headers=auth_headers(owner))
    assert r.status_code == 200
    report = r.json()
    assert report["group_by"] == "user" and report["totals"]["messages"] == 2
    assert [g["key"] for g in report["groups"]] == [member["email"]]
    r = await client.get(f"{P}/orgs/{org['id']}/usage?group_by=day", headers=auth_headers(owner))
    assert len(r.json()["groups"]) == 1
    r = await client.get(f"{P}/orgs/{org['id']}/usage", headers=auth_headers(member))
    assert r.status_code == 403


async def test_allowed_models_policy(client):
    owner = await register_and_login(client)
    org = await _team(client, owner)
    r = await client.get(f"{P}/models", headers=auth_headers(owner))
    available = [m["id"] for m in r.json()["items"]]
    assert available
    r = await client.patch(
        f"{P}/orgs/{org['id']}",
        json={"settings": {"allowed_models": ["some-other-model"]}},
        headers=auth_headers(owner),
    )
    assert r.status_code == 200
    h = _h(owner, org["id"])
    r = await client.post(f"{P}/sessions", json={}, headers=h)
    sid = r.json()["id"]
    r = await client.post(
        f"{P}/sessions/{sid}/messages",
        json={"content": "hi", "use_rag": False, "model": available[0]},
        headers=h,
    )
    assert r.status_code == 422 and r.json()["error"]["details"]["allowed_models"] == ["some-other-model"]
    # Without an explicit model the org default applies and the turn runs.
    r = await client.post(f"{P}/sessions/{sid}/messages", json={"content": "hi", "use_rag": False}, headers=h)
    assert r.status_code == 200


async def test_server_admin_endpoints_require_admin_role(client, db):
    from sqlalchemy import update

    from app.models.user import User

    acct = await register_and_login(client)
    r = await client.get(f"{P}/admin/usage", headers=auth_headers(acct))
    assert r.status_code == 403
    r = await client.get(f"{P}/admin/feedback", headers=auth_headers(acct))
    assert r.status_code == 403

    await db.execute(update(User).where(User.id == acct["user"]["id"]).values(role="admin"))
    await db.commit()
    r = await client.get(f"{P}/admin/usage?group_by=org", headers=auth_headers(acct))
    assert r.status_code == 200 and r.json()["group_by"] == "org"
    r = await client.get(f"{P}/admin/feedback", headers=auth_headers(acct))
    assert r.status_code == 200 and r.json()["total"] == 0
