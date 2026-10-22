# ruff: noqa: F811
import pytest
from sqlalchemy import select

from app.models import AlertTemplate, AuditLog, Incident, IncidentType, Role
from tests.helpers import audit_actions
from tests.test_alerts import _ann, _login, fake_teams  # noqa: F401
from tests.test_ha import _ha_settings, _speaker, ha_api, redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_db")

FROM_TPL = "/incidents/from-template"


async def _by_type(api, type_: str) -> dict:
    rows = (await api.get("/alert-templates")).json()
    return next(t for t in rows if t["type"] == type_)


async def test_defaults_cover_every_type(ha_api, make_user, login_as) -> None:
    await _login(make_user, login_as, Role.staff)
    rows = (await ha_api.get("/alert-templates")).json()
    assert [t["type"] for t in rows] == [t.value for t in IncidentType]
    assert all(t["is_active"] and t["label"] and t["title"] and t["message"] for t in rows)
    conf = next(t for t in rows if t["type"] == "confinement")
    assert conf["message"].startswith("Le plan de Mise à l'abri et de confinement est déclenché")


async def test_staff_read_only_admin_writes(ha_api, make_user, login_as, db) -> None:
    admin = await _login(make_user, login_as, Role.admin)
    tpl = await _by_type(ha_api, "meteo")
    r = await ha_api.patch(
        f"/alert-templates/{tpl['id']}",
        json={"title": "Titre secret XYZ", "color": "#aabbcc", "icon": "air"},
    )
    assert r.status_code == 200 and r.json()["color"] == "#aabbcc"
    async with db() as s:
        q = select(AuditLog.detail).where(AuditLog.action == "alert_template_update")
        details = (await s.scalars(q)).all()
    assert details and all("XYZ" not in str(d) for d in details)
    assert "alert_template_update" in await audit_actions(db, admin.email)
    restore = {"title": tpl["title"], "color": None, "icon": tpl["icon"]}
    assert (await ha_api.patch(f"/alert-templates/{tpl['id']}", json=restore)).status_code == 200

    await login_as(await make_user(Role.staff))
    url = f"/alert-templates/{tpl['id']}"
    assert (await ha_api.get(url)).status_code == 200
    assert (await ha_api.patch(url, json={"title": "x"})).status_code == 403
    assert (await ha_api.delete(url)).status_code == 403
    body = {"type": "autre", "label": "a", "title": "b", "message": "c"}
    assert (await ha_api.post("/alert-templates", json=body)).status_code == 403
    assert (await ha_api.get("/admin")).status_code == 403
    assert (await ha_api.get("/ui/admin/alert-templates")).status_code == 403


async def test_validation_unique_and_delete(ha_api, make_user, login_as, db) -> None:
    await _login(make_user, login_as)
    tpl = await _by_type(ha_api, "autre")
    p = f"/alert-templates/{tpl['id']}"
    for bad in (
        {"title": ""},
        {"title": "   "},
        {"label": "x" * 61},
        {"title": "x" * 256},
        {"message": "x" * 501},
        {"color": "red"},
        {"icon": "Bad Icon"},
        {"is_active": None},
        {"message": None},
    ):
        assert (await ha_api.patch(p, json=bad)).status_code == 422, bad
    dup = {"type": "autre", "label": "a", "title": "b", "message": "c"}
    assert (await ha_api.post("/alert-templates", json=dup)).status_code == 409
    assert (await ha_api.post("/alert-templates", json={**dup, "type": "nope"})).status_code == 422
    assert (await ha_api.delete(p)).status_code == 204
    assert (await ha_api.get(p)).status_code == 404
    r = await ha_api.post("/alert-templates", json=dup)
    assert r.status_code == 201 and r.json()["is_active"] is True
    async with db() as s:
        rows = (await s.scalars(select(AlertTemplate).where(AlertTemplate.type == "autre"))).all()
    assert len(rows) == 1


async def test_admin_ui_and_alerts_page_use_templates(ha_api, make_user, login_as) -> None:
    await _login(make_user, login_as)
    frag = await ha_api.get("/ui/admin/alert-templates")
    assert frag.status_code == 200 and "/alert-templates/" in frag.text
    assert "Modèles d'alerte" in (await ha_api.get("/admin")).text
    tpl = await _by_type(ha_api, "intrusion")
    url = f"/alert-templates/{tpl['id']}"
    await ha_api.patch(url, json={"is_active": False})
    try:
        page = (await ha_api.get("/alerts")).text
        assert 'data-type="intrusion"' not in page and 'data-type="incendie"' in page
        assert 'data-test-word="TEST"' in page and 'role="switch"' in page
    finally:
        await ha_api.patch(url, json={"is_active": True})
    assert 'data-type="intrusion"' in (await ha_api.get("/alerts")).text


async def test_send_from_template_real(ha_api, fake_teams, make_user, login_as, db, redis) -> None:
    u = await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.tpl_real", notify_service="notify.tpl_real")
    tpl = await _by_type(ha_api, "confinement")
    r = await ha_api.post(
        FROM_TPL, json={"type": "confinement", "is_test": False, "confirm": "ALERTE"}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "sent" and not out["is_test"]
    assert out["title"] == tpl["title"] and out["message"] == tpl["message"]
    assert fake_teams.notices[0].message == tpl["message"]
    ann = await _ann(db, out["announcement_id"])
    assert not ann.is_test and ann.priority == 0
    acts = await audit_actions(db, u.email)
    assert {"incident_create", "incident_send", "incident_delivery"} <= set(acts)


async def test_exercise_vs_real_confirmation(
    ha_api, fake_teams, make_user, login_as, db, redis
) -> None:
    u = await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.tpl_test", notify_service="notify.tpl_test")
    for is_test, word in ((False, "TEST"), (True, "ALERTE"), (False, ""), (True, "test")):
        r = await ha_api.post(
            FROM_TPL, json={"type": "incendie", "is_test": is_test, "confirm": word}
        )
        assert r.status_code == 422, (is_test, word)
    assert (
        await ha_api.post(FROM_TPL, json={"type": "incendie", "is_test": False})
    ).status_code == 422
    assert fake_teams.notices == []
    async with db() as s:
        q = select(Incident).where(Incident.created_by_email == u.email)
        assert not (await s.scalars(q)).all()
    assert "incident_send_refused" in await audit_actions(db, u.email)

    r = await ha_api.post(FROM_TPL, json={"type": "incendie", "is_test": True, "confirm": "TEST"})
    assert r.status_code == 200 and r.json()["is_test"] is True
    ann = await _ann(db, r.json()["announcement_id"])
    assert ann.is_test and ann.text.startswith(_ha_settings().test_prefix)
    assert fake_teams.notices[0].is_test


async def test_from_template_inactive_unknown_anonymous(
    ha_api, fake_teams, make_user, login_as, client
) -> None:
    body = {"type": "meteo", "is_test": True, "confirm": "TEST"}
    assert (await client.post(FROM_TPL, json=body)).status_code == 401
    await _login(make_user, login_as)
    url = f"/alert-templates/{(await _by_type(ha_api, 'meteo'))['id']}"
    await ha_api.patch(url, json={"is_active": False})
    try:
        assert (await ha_api.post(FROM_TPL, json=body)).status_code == 404
    finally:
        await ha_api.patch(url, json={"is_active": True})
    assert (await ha_api.post(FROM_TPL, json={**body, "type": "x"})).status_code == 422
    assert fake_teams.notices == []


async def test_from_template_rate_limited(ha_api, fake_teams, make_user, login_as, redis) -> None:
    await _login(make_user, login_as)
    body = {"type": "autre", "is_test": True, "confirm": "TEST"}
    codes = [(await ha_api.post(FROM_TPL, json=body)).status_code for _ in range(12)]
    assert 429 in codes


async def test_history_table_and_recap(ha_api, fake_teams, make_user, login_as) -> None:
    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.tpl_hist")
    r = await ha_api.post(FROM_TPL, json={"type": "meteo", "is_test": True, "confirm": "TEST"})
    iid = r.json()["id"]
    frag = (await ha_api.get("/ui/alerts/list")).text
    assert f'href="/alerts/{iid}"' in frag and "TEST" in frag and "<table" in frag
    assert "Alerte météorologique" in frag
    page = await ha_api.get(f"/alerts/{iid}")
    assert page.status_code == 200
    for needle in ("Chronologie", "Message diffusé", "export.csv", "tpl_hist", "/close"):
        assert needle in page.text, needle
    assert (await ha_api.post(f"/incidents/{iid}/close")).status_code == 200
    assert "/close" not in (await ha_api.get(f"/alerts/{iid}")).text


async def test_from_template_dedup_lock(ha_api, fake_teams, make_user, login_as, db, redis) -> None:
    u = await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.tpl_dup", notify_service="notify.tpl_dup")
    body = {"type": "meteo", "is_test": False, "confirm": "ALERTE"}
    assert (await ha_api.post(FROM_TPL, json=body)).status_code == 200
    r = await ha_api.post(FROM_TPL, json=body)
    assert r.status_code == 409 and "déjà en cours/récent" in r.json()["detail"]
    async with db() as s:
        q = select(Incident).where(Incident.created_by_email == u.email)
        assert len((await s.scalars(q)).all()) == 1
    other = {**body, "is_test": True, "confirm": "TEST"}
    assert (await ha_api.post(FROM_TPL, json=other)).status_code == 200


async def test_from_template_dedup_fail_open(
    ha_api, fake_teams, make_user, login_as, redis
) -> None:
    from app.ha.router import get_redis
    from app.main import app

    class Broken:
        def __getattr__(self, name):
            async def boom(*a, **k):
                raise ConnectionError("redis down")

            return boom

    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.tpl_fo", notify_service="notify.tpl_fo")
    app.dependency_overrides[get_redis] = lambda: Broken()
    body = {"type": "meteo", "is_test": True, "confirm": "TEST"}
    for _ in range(2):
        assert (await ha_api.post(FROM_TPL, json=body)).status_code == 200


async def test_from_template_dedup_released_on_failure(
    ha_api, fake_teams, make_user, login_as, redis
) -> None:
    from app.config import get_settings
    from app.main import app

    await _login(make_user, login_as)
    fake_teams.fail = True
    ok_settings = app.dependency_overrides[get_settings]
    app.dependency_overrides[get_settings] = lambda: _ha_settings().model_copy(
        update={"ha_url": ""}
    )
    body = {"type": "meteo", "is_test": True, "confirm": "TEST"}
    assert (await ha_api.post(FROM_TPL, json=body)).status_code == 502
    fake_teams.fail = False
    app.dependency_overrides[get_settings] = ok_settings
    await _speaker(ha_api, "media_player.tpl_retry", notify_service="notify.tpl_retry")
    assert (await ha_api.post(FROM_TPL, json=body)).status_code == 200
