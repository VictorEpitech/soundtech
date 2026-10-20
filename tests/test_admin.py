import io

import pytest
from sqlalchemy import select

from app.bootstrap import ensure_bootstrap_admin
from app.models import AuditLog, Role, User
from tests.helpers import _settings, audit_actions

pytestmark = pytest.mark.usefixtures("migrated_db")


async def test_requires_auth_and_role(client, make_user, login_as) -> None:
    assert (await client.get("/admin/users")).status_code == 401
    staff = await make_user(Role.staff)
    await login_as(staff)
    assert (await client.get("/admin/users")).status_code == 403


async def test_crud_and_audit(client, make_user, login_as, db) -> None:
    admin = await make_user(Role.admin)
    await login_as(admin)
    r = await client.post("/admin/users", json={"email": "New@Epitech.eu", "role": "staff"})
    assert r.status_code == 201 and r.json()["email"] == "new@epitech.eu"
    uid = r.json()["id"]
    assert (await client.post("/admin/users", json={"email": "new@epitech.eu"})).status_code == 409
    p = await client.patch(f"/admin/users/{uid}", json={"role": "admin", "is_active": False})
    assert p.json()["role"] == "admin" and p.json()["is_active"] is False
    assert (await client.get("/admin/users", params={"role": "admin"})).status_code == 200
    assert (await client.delete(f"/admin/users/{uid}")).status_code == 204
    assert (await client.get(f"/admin/users/{uid}")).status_code == 404
    acts = await audit_actions(db, admin.email)
    assert {"user_create", "user_update", "user_delete"} <= set(acts)


async def test_last_admin_protected(client, make_user, login_as, db) -> None:
    async with db() as s:  # neutralise les admins laissés par d'autres tests
        for u in (await s.scalars(select(User).where(User.role == "admin"))).all():
            u.is_active = False
        await s.commit()
    admin = await make_user(Role.admin)
    await login_as(admin)
    url = f"/admin/users/{admin.id}"
    assert (await client.patch(url, json={"role": "staff"})).status_code == 409
    assert (await client.patch(url, json={"is_active": False})).status_code == 409
    assert (await client.delete(url)).status_code == 409
    other = await make_user(Role.admin)
    assert (await client.patch(url, json={"role": "staff"})).status_code == 200
    assert other.id  # un autre admin actif existait


async def test_csv_import(client, make_user, login_as, db) -> None:
    admin = await make_user(Role.admin)
    await login_as(admin)
    body = (
        "email,nom,role\n"
        "a1@epitech.eu,Alice,\n"
        "b1@epitech.eu,Bob,staff\n"
        "a1@epitech.eu,Dup,student\n"
        "bad,Oops,student\n"
        "c1@epitech.eu,Carl,root\n"
    )
    r = await client.post("/admin/users/import", content=io.BytesIO(body.encode()).read())
    assert r.status_code == 200
    j = r.json()
    assert (j["created"], j["skipped"], len(j["errors"])) == (2, 1, 2)
    assert {e["line"] for e in j["errors"]} == {5, 6}
    users = (await client.get("/admin/users", params={"role": "student"})).json()
    assert "a1@epitech.eu" in {u["email"] for u in users}
    assert "user_import" in await audit_actions(db, admin.email)
    assert (await client.post("/admin/users/import", content=b"nom\nx")).status_code == 400


async def test_bootstrap_admin_idempotent(db, monkeypatch) -> None:
    import app.bootstrap as b

    monkeypatch.setattr(b, "SessionLocal", db)
    s = _settings().model_copy(update={"bootstrap_admin_email": "Boot@Epitech.eu"})
    await ensure_bootstrap_admin(s)
    await ensure_bootstrap_admin(s)
    async with db() as sess:
        users = (await sess.scalars(select(User).where(User.email == "boot@epitech.eu"))).all()
        logs = await sess.scalars(select(AuditLog).where(AuditLog.actor == "boot@epitech.eu"))
    assert len(users) == 1 and users[0].role == "admin"
    assert len(list(logs)) == 1
