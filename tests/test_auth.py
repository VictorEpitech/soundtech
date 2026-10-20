import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.auth import oidc
from app.config import Settings
from app.models import Role
from tests.helpers import CLIENT, TENANT, audit_actions, make_id_token

pytestmark = pytest.mark.usefixtures("migrated_db")


async def test_login_redirect_has_pkce_and_cookie(client: AsyncClient) -> None:
    r = await client.get("/auth/login")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize")
    assert "code_challenge=" in loc and f"client_id={CLIENT}" in loc
    assert "oidc_tx" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]


async def test_login_success_sets_cookies_and_me(client, do_login, make_user, db) -> None:
    u = await make_user(Role.staff)
    r = await do_login(preferred_username=u.email.upper(), name="Staff Person")
    assert r.status_code == 302
    cookies = r.headers.get_list("set-cookie")
    for name in ("access_token", "refresh_token"):
        c = next(c for c in cookies if c.startswith(name))
        assert "HttpOnly" in c and "Secure" in c and "SameSite=lax" in c
    me = await client.get("/auth/me")
    assert me.status_code == 200 and me.json()["role"] == "staff"
    assert me.json()["name"] == "Staff Person"
    assert "login" in await audit_actions(db, u.email)


async def test_me_accepts_bearer(client, do_login, make_user) -> None:
    u = await make_user(Role.admin)
    r = await do_login(preferred_username=u.email)
    access = client.cookies.get("access_token")
    assert r.status_code == 302 and access
    client.cookies.clear()
    assert (await client.get("/auth/me")).status_code == 401
    ok = await client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert ok.status_code == 200


@pytest.mark.parametrize("role,active", [(Role.student, True), (Role.staff, False)])
async def test_login_denied_role_or_disabled(client, do_login, make_user, db, role, active) -> None:
    u = await make_user(role, active)
    r = await do_login(preferred_username=u.email)
    assert r.status_code == 403
    assert "access_token" not in r.headers.get("set-cookie", "")
    assert "login_denied" in await audit_actions(db, u.email)


async def test_login_denied_unknown_email(client, do_login, db) -> None:
    r = await do_login(preferred_username="nobody@epitech.eu")
    assert r.status_code == 403
    assert "login_denied" in await audit_actions(db, "nobody@epitech.eu")


async def test_login_denied_oid_bound_to_other_account(client, do_login, make_user) -> None:
    u = await make_user(Role.staff)
    assert (await do_login(preferred_username=u.email, oid="oid-A")).status_code == 302
    client.cookies.clear()
    assert (await do_login(preferred_username=u.email, oid="oid-B")).status_code == 403


@pytest.mark.parametrize(
    "over",
    [
        {"aud": "other"},
        {"iss": "https://evil.example/v2.0"},
        {"tid": "33333333-3333-3333-3333-333333333333"},
        {"nonce": "wrong"},
        {"exp": 1},
        {"_key": "other"},
    ],
)
async def test_invalid_id_token_rejected(client, do_login, make_user, other_key, over) -> None:
    u = await make_user(Role.admin)
    if over.get("_key") == "other":
        over = {"_key": other_key}
    r = await do_login(preferred_username=u.email, **over)
    assert r.status_code == 401
    assert "access_token" not in r.headers.get("set-cookie", "")


async def test_state_mismatch(client: AsyncClient) -> None:
    await client.get("/auth/login")
    r = await client.get("/auth/callback", params={"code": "c", "state": "bad"})
    assert r.status_code == 400


async def test_refresh_rotates_and_logout_revokes(client, do_login, make_user, db) -> None:
    u = await make_user(Role.admin)
    await do_login(preferred_username=u.email)
    old = client.cookies.get("refresh_token")
    r = await client.post("/auth/refresh")
    assert r.status_code == 204
    new = client.cookies.get("refresh_token")
    assert new and new != old
    client.cookies.set("refresh_token", old, path="/auth", domain="sound.test")
    assert (await client.post("/auth/refresh")).status_code == 401
    client.cookies.set("refresh_token", new, path="/auth", domain="sound.test")
    assert (await client.post("/auth/logout")).status_code == 204
    assert (await client.post("/auth/refresh")).status_code == 401
    assert (await audit_actions(db, u.email))[-1] == "logout"


async def test_audit_log_is_append_only(db, client, do_login, make_user) -> None:
    u = await make_user(Role.admin)
    await do_login(preferred_username=u.email)
    async with db() as s:
        for stmt in (
            "UPDATE audit_log SET action='x'",
            "DELETE FROM audit_log",
            "TRUNCATE audit_log",
        ):
            with pytest.raises(Exception, match="append-only"):
                await s.execute(text(stmt))
            await s.rollback()


async def test_validate_id_token_unit(client) -> None:
    from tests.helpers import _settings

    s: Settings = _settings()
    claims = await oidc.validate_id_token(s, make_id_token("n1"), "n1")
    assert claims["tid"] == TENANT
