from sqlalchemy import delete, func, select

from app.models import AuditLog, Role, Student
from tests.helpers import audit_actions

CSV = (
    "Email;Nom;Prénom;Promotion;Parcours\n"
    "Ada.Test@Example.org;Test;Ada;2027;PGE\n"
    "bob@example.org;Martin;Bob;2028;Bachelor\n"
    "pas-un-email;X;Y;2027;PGE\n"
    "ada.test@example.org;Dup;Ada;2027;PGE\n"
)


async def _reset(db) -> None:
    async with db() as s:
        await s.execute(delete(Student))
        await s.commit()


async def _seed(db, rows) -> None:
    await _reset(db)
    async with db() as s:
        for e, p, c in rows:
            s.add(Student(email=e, last_name="N", first_name="P", promo=p, cursus=c))
        await s.commit()


async def _count(db) -> int:
    async with db() as s:
        return await s.scalar(select(func.count()).select_from(Student))


async def test_access_admin_ok_staff_403(client, make_user, login_as, db) -> None:
    await login_as(await make_user(Role.staff))
    assert (await client.get("/admin/etudiants")).status_code == 403
    assert (
        await client.post("/admin/students/import/preview", json={"csv": "email\n"})
    ).status_code == 403
    assert (await client.post("/admin/students/delete", json={"scope": "all"})).status_code == 403
    await login_as(await make_user(Role.admin))
    page = await client.get("/admin/etudiants")
    assert page.status_code == 200 and "/admin/etudiants" in page.text
    assert (await client.get("/ui/admin/students")).status_code == 200


async def test_anonymous_rejected(client) -> None:
    r = await client.post("/admin/students/import/preview", json={"csv": "email\n"})
    assert r.status_code == 401


async def test_import_preview_then_apply_upsert(client, make_user, login_as, db) -> None:
    await _seed(db, [("bob@example.org", "2020", "Old"), ("gone@example.org", "2020", "Old")])
    await login_as(await make_user(Role.admin))
    p = (await client.post("/admin/students/import/preview", json={"csv": CSV})).json()
    assert (p["adds"], p["updates"], p["unchanged"]) == (1, 1, 0)
    assert [e["line"] for e in p["errors"]] == [4, 5]
    assert p["missing"] == ["gone@example.org"]
    assert await _count(db) == 2  # aperçu : aucune écriture

    r = await client.post("/admin/students/import/apply", json={"csv": CSV, "delete": []})
    assert r.json() == {"added": 1, "updated": 1, "deleted": 0, "errors": 2}
    async with db() as s:
        ada = await s.scalar(select(Student).where(Student.email == "ada.test@example.org"))
        bob = await s.scalar(select(Student).where(Student.email == "bob@example.org"))
        assert (ada.first_name, ada.promo, ada.cursus) == ("Ada", "2027", "PGE")
        assert (bob.promo, bob.cursus) == ("2028", "Bachelor")
    assert await _count(db) == 3  # absent non coché : conservé
    # réimport identique : plus rien à faire
    p2 = (await client.post("/admin/students/import/preview", json={"csv": CSV})).json()
    assert (p2["adds"], p2["updates"], p2["unchanged"]) == (0, 0, 2)


async def test_apply_deletes_only_checked_absent(client, make_user, login_as, db) -> None:
    await _seed(
        db,
        [
            ("gone@example.org", "1", "A"),
            ("keep@example.org", "1", "A"),
            ("bob@example.org", "1", "A"),
        ],
    )
    admin = await make_user(Role.admin)
    await login_as(admin)
    r = await client.post(
        "/admin/students/import/apply",
        # bob est dans le fichier : une suppression demandée pour lui doit être ignorée
        json={"csv": CSV, "delete": ["gone@example.org", "BOB@example.org", "inconnu@x.org"]},
    )
    assert r.json()["deleted"] == 1
    async with db() as s:
        emails = set(await s.scalars(select(Student.email)))
    assert emails == {"keep@example.org", "bob@example.org", "ada.test@example.org"}
    assert "students_import" in await audit_actions(db, admin.email)


async def test_ui_preview_lists_missing(client, make_user, login_as, db) -> None:
    await _seed(db, [("gone@example.org", "1", "A")])
    await login_as(await make_user(Role.admin))
    r = await client.post("/ui/admin/students/preview", json={"csv": CSV})
    assert r.status_code == 200
    assert 'name="delete" value="gone@example.org"' in r.text
    assert "Ligne 4" in r.text


async def test_invalid_csv(client, make_user, login_as, db) -> None:
    await login_as(await make_user(Role.admin))
    for body in ("", "nom,prenom\nA,B\n"):
        r = await client.post("/admin/students/import/preview", json={"csv": body})
        assert r.status_code == 400
    big = "email\n" + "a@b.co\n" * 200_000
    assert (await client.post("/admin/students/import/preview", json={"csv": big})).status_code in (
        413,
        422,
    )
    comma = "email,nom\nz@example.org,Z\n"
    ok = await client.post("/admin/students/import/preview", json={"csv": comma})
    assert ok.json()["adds"] == 1


async def test_delete_by_scope(client, make_user, login_as, db) -> None:
    rows = [("a@x.org", "2027", "PGE"), ("b@x.org", "2027", "Bachelor"), ("c@x.org", "2028", "PGE")]
    await _seed(db, rows)
    admin = await make_user(Role.admin)
    await login_as(admin)
    r = await client.post("/admin/students/delete", json={"scope": "promo", "value": "2027"})
    assert r.json() == {"deleted": 2} and await _count(db) == 1
    await _seed(db, rows)
    r = await client.post("/admin/students/delete", json={"scope": "cursus", "value": "PGE"})
    assert r.json() == {"deleted": 2} and await _count(db) == 1
    assert (await client.post("/admin/students/delete", json={"scope": "promo"})).status_code == 422
    await _seed(db, rows)
    # globale : confirmation saisie obligatoire
    assert (await client.post("/admin/students/delete", json={"scope": "all"})).status_code == 422
    assert (
        await client.post("/admin/students/delete", json={"scope": "all", "confirm": "non"})
    ).status_code == 422
    assert await _count(db) == 3
    r = await client.post("/admin/students/delete", json={"scope": "all", "confirm": "supprimer"})
    assert r.json() == {"deleted": 3} and await _count(db) == 0
    async with db() as s:
        logs = (await s.scalars(select(AuditLog).where(AuditLog.action == "students_delete"))).all()
    mine = [log.detail for log in logs if log.actor == admin.email]
    assert {"scope": "all", "value": None, "count": 3} in mine
    assert {"scope": "promo", "value": "2027", "count": 2} in mine


async def test_list_filters_pagination(client, make_user, login_as, db) -> None:
    await _seed(db, [(f"s{i:03d}@x.org", "2027" if i % 2 else "2028", "PGE") for i in range(120)])
    await login_as(await make_user(Role.admin))
    t = (await client.get("/ui/admin/students")).text
    assert "120 étudiants au total" in t and "Page 1 / 3" in t
    t = (await client.get("/ui/admin/students", params={"promo": "2027"})).text
    assert "60 correspondants" in t
    t = (await client.get("/ui/admin/students", params={"q": "S007"})).text
    assert "s007@x.org" in t and "s008@x.org" not in t
    await _reset(db)
