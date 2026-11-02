"""Auth lifecycle against the real app + Postgres: rotation, revocation, deletion."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.exceptions import RateLimitError
from app.core.rate_limit import FixedWindowRateLimiter
from app.models.user import RefreshToken
from tests.conftest import auth_headers, register_and_login, requires_infra

pytestmark = [pytest.mark.asyncio(loop_scope="session"), requires_infra]

P = "/api/v1"


async def test_register_login_me(client):
    acct = await register_and_login(client)
    r = await client.get(f"{P}/auth/me", headers=auth_headers(acct))
    assert r.status_code == 200
    assert r.json()["email"] == acct["email"]
    assert r.json()["full_name"] == "Test User"


async def test_register_duplicate_is_conflict(client):
    acct = await register_and_login(client)
    r = await client.post(
        f"{P}/auth/register", json={"email": acct["email"].upper(), "password": "password123"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    assert r.json()["request_id"]


async def test_login_wrong_password(client):
    acct = await register_and_login(client)
    r = await client.post(f"{P}/auth/login", json={"email": acct["email"], "password": "nope-nope"})
    assert r.status_code == 401


async def test_refresh_rotates_and_retry_within_grace_is_tolerated(client, db):
    acct = await register_and_login(client)
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": acct["refresh_token"]})
    assert r.status_code == 200
    pair2 = r.json()
    assert pair2["refresh_token"] != acct["refresh_token"]

    # The same old token again right away looks like a client retry: a fresh pair is
    # issued and the orphaned pair2 refresh token is retired.
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": acct["refresh_token"]})
    assert r.status_code == 200
    pair3 = r.json()
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": pair2["refresh_token"]})
    assert r.status_code == 401
    # Reuse of a retired token revokes the whole family, pair3 included.
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": pair3["refresh_token"]})
    assert r.status_code == 401

    rows = (await db.execute(select(RefreshToken))).scalars().all()
    assert rows and all(row.revoked_at is not None for row in rows)
    assert len({row.family_id for row in rows}) == 1


async def test_refresh_reuse_outside_grace_revokes_family(client, monkeypatch):
    monkeypatch.setattr(settings, "REFRESH_TOKEN_REUSE_GRACE_SECONDS", 0)
    acct = await register_and_login(client)
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": acct["refresh_token"]})
    assert r.status_code == 200
    pair2 = r.json()
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": acct["refresh_token"]})
    assert r.status_code == 401
    assert "reuse" in r.json()["error"]["message"].lower()
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": pair2["refresh_token"]})
    assert r.status_code == 401


async def test_logout_revokes_only_that_refresh_token(client):
    acct = await register_and_login(client)
    r = await client.post(f"{P}/auth/login", json={"email": acct["email"], "password": acct["password"]})
    other = r.json()
    r = await client.post(f"{P}/auth/logout", json={"refresh_token": acct["refresh_token"]})
    assert r.status_code == 200
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": acct["refresh_token"]})
    assert r.status_code == 401
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": other["refresh_token"]})
    assert r.status_code == 200
    # Access tokens are stateless and keep working until they expire (documented).
    r = await client.get(f"{P}/auth/me", headers=auth_headers(acct))
    assert r.status_code == 200
    # Logout with garbage never errors.
    r = await client.post(f"{P}/auth/logout", json={"refresh_token": "garbage"})
    assert r.status_code == 200


async def test_logout_all_revokes_every_device(client):
    acct = await register_and_login(client)
    r = await client.post(f"{P}/auth/login", json={"email": acct["email"], "password": acct["password"]})
    other = r.json()
    r = await client.post(f"{P}/auth/logout-all", headers=auth_headers(acct))
    assert r.status_code == 200
    for tok in (acct["refresh_token"], other["refresh_token"]):
        r = await client.post(f"{P}/auth/refresh", json={"refresh_token": tok})
        assert r.status_code == 401


async def test_change_password_rotates_sessions(client):
    acct = await register_and_login(client)
    r = await client.post(
        f"{P}/auth/change-password",
        json={"current_password": "wrong-wrong", "new_password": "newpassword456"},
        headers=auth_headers(acct),
    )
    assert r.status_code == 401
    r = await client.post(
        f"{P}/auth/change-password",
        json={"current_password": acct["password"], "new_password": "newpassword456"},
        headers=auth_headers(acct),
    )
    assert r.status_code == 200
    pair = r.json()
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": acct["refresh_token"]})
    assert r.status_code == 401
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 200
    r = await client.post(f"{P}/auth/login", json={"email": acct["email"], "password": acct["password"]})
    assert r.status_code == 401
    r = await client.post(f"{P}/auth/login", json={"email": acct["email"], "password": "newpassword456"})
    assert r.status_code == 200


async def test_update_profile(client):
    acct = await register_and_login(client)
    r = await client.patch(f"{P}/auth/me", json={"full_name": "  Ada Lovelace "}, headers=auth_headers(acct))
    assert r.status_code == 200
    assert r.json()["full_name"] == "Ada Lovelace"


async def test_delete_account_requires_password_and_frees_email(client):
    acct = await register_and_login(client)
    r = await client.post(f"{P}/sessions", json={}, headers=auth_headers(acct))
    assert r.status_code == 201

    r = await client.request(
        "DELETE", f"{P}/auth/me", json={"password": "wrong-wrong"}, headers=auth_headers(acct)
    )
    assert r.status_code == 401
    r = await client.get(f"{P}/auth/me", headers=auth_headers(acct))
    assert r.status_code == 200

    r = await client.request(
        "DELETE", f"{P}/auth/me", json={"password": acct["password"]}, headers=auth_headers(acct)
    )
    assert r.status_code == 200
    r = await client.get(f"{P}/auth/me", headers=auth_headers(acct))
    assert r.status_code == 401
    r = await client.post(f"{P}/auth/login", json={"email": acct["email"], "password": acct["password"]})
    assert r.status_code == 401
    r = await client.post(f"{P}/auth/refresh", json={"refresh_token": acct["refresh_token"]})
    assert r.status_code == 401
    # The address can sign up again.
    r = await client.post(f"{P}/auth/register", json={"email": acct["email"], "password": "password123"})
    assert r.status_code == 201


async def test_api_key_auth(client):
    acct = await register_and_login(client)
    r = await client.post(f"{P}/auth/api-keys", json={"name": "ci"}, headers=auth_headers(acct))
    assert r.status_code == 201
    key = r.json()["plaintext_key"]
    key_id = r.json()["id"]
    r = await client.get(f"{P}/auth/me", headers={"X-API-Key": key})
    assert r.status_code == 200
    r = await client.delete(f"{P}/auth/api-keys/{key_id}", headers=auth_headers(acct))
    assert r.status_code == 200
    r = await client.get(f"{P}/auth/me", headers={"X-API-Key": key})
    assert r.status_code == 401


async def test_fixed_window_limiter_blocks_over_limit():
    limiter = FixedWindowRateLimiter(requests=2, window_seconds=60)
    key = "test:limiter:unique"
    await limiter.hit(key)
    await limiter.hit(key)
    with pytest.raises(RateLimitError) as exc:
        await limiter.hit(key)
    assert exc.value.details["limit"] == 2
    assert exc.value.details["retry_after_seconds"] >= 0
