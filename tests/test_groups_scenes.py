# ruff: noqa: F811
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.config import get_settings
from app.ha.router import get_redis
from app.ma.client import MAClient, MAConnectionError
from app.main import app
from app.models import Announcement, AuditLog, Incident, Preset, Role, Speaker
from tests.test_ha import _eid, redis  # noqa: F401
from tests.test_ma import (  # noqa: F401
    FakeMA,
    _ma_settings,
    declared,
    fake_ma,
)

pytestmark = pytest.mark.usefixtures("migrated_db")


def _name() -> str:
    return "g-" + uuid.uuid4().hex[:8]


@pytest.fixture
async def api(client: AsyncClient, fake_ma: FakeMA, declared: None, redis, db) -> AsyncClient:
    """MA + HA configurés, Redis éphémère, aucun incident en cours."""
    s = _ma_settings(fake_ma).model_copy(
        update={
            "ha_url": "http://ha.test",
            "ha_token": SecretStr("tok"),
            "scene_announce_wait": 0.0,
        }
    )
    app.dependency_overrides[get_settings] = lambda: s
    app.dependency_overrides[get_redis] = lambda: redis
    async with db() as sess:
        await sess.execute(
            update(Incident).where(Incident.status == "sent").values(status="closed")
        )
        await sess.commit()
    return client


@pytest.fixture
async def admin(api: AsyncClient, make_user, login_as) -> AsyncClient:
    await login_as(await make_user(Role.admin))
    return api


async def _speakers(db, *specs: tuple[str, str | None, bool]) -> list[str]:
    """specs : (nom, ma_player_id, actif) ; renvoie les ids."""
    async with db() as s:
        rows = [Speaker(entity_id=_eid(), name=n, ma_player_id=p, is_active=a) for n, p, a in specs]
        s.add_all(rows)
        await s.commit()
        return [str(r.id) for r in rows]


async def _group(c: AsyncClient, members: list[str], **kw: Any) -> dict[str, Any]:
    r = await c.post("/speaker-groups", json={"name": _name(), "member_ids": members, **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def _scene(c: AsyncClient, **kw: Any) -> dict[str, Any]:
    body = {"name": _name(), "media_uri": "library://playlist/1", "media_type": "playlist", **kw}
    r = await c.post("/scenes", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _actions(db, action: str) -> list[AuditLog]:
    async with db() as s:
        return list(await s.scalars(select(AuditLog).where(AuditLog.action == action)))


async def _launches(db, scene_id: str) -> list[AuditLog]:
    return [x for x in await _actions(db, "scene_launch") if x.detail["scene_id"] == scene_id]


# --- groupes ----------------------------------------------------------------


async def test_group_crud_audit_and_validation(admin, db) -> None:
    a, b = await _speakers(db, ("A", None, True), ("B", None, True))
    g = await _group(admin, [a, b, a], description="Étage")
    assert len(g["members"]) == 2 and g["active_count"] == 2
    assert (await admin.post("/speaker-groups", json={"name": g["name"]})).status_code == 409
    assert (await admin.post("/speaker-groups", json={"name": "  "})).status_code == 422
    bad = {"name": _name(), "member_ids": [str(uuid.uuid4())]}
    assert (await admin.post("/speaker-groups", json=bad)).status_code == 404
    url = f"/speaker-groups/{g['id']}"
    r = await admin.patch(url, json={"member_ids": [b], "is_active": False})
    assert r.json()["member_ids"] == [b] and r.json()["is_active"] is False
    assert (await admin.patch(url, json={"name": None})).status_code == 422
    assert (await admin.patch(url, json={"member_ids": []})).json()["members"] == []
    assert any(x["id"] == g["id"] for x in (await admin.get("/speaker-groups")).json())
    assert (await admin.delete(url)).status_code == 204
    assert (await admin.get(url)).status_code == 404
    for act in ("speaker_group_create", "speaker_group_update", "speaker_group_delete"):
        assert await _actions(db, act)


async def test_group_rights(api, make_user, login_as) -> None:
    assert (await api.get("/speaker-groups")).status_code == 401
    await login_as(await make_user(Role.staff))
    assert (await api.get("/speaker-groups")).status_code == 200
    assert (await api.post("/speaker-groups", json={"name": _name()})).status_code == 403
    gid = uuid.uuid4()
    assert (await api.patch(f"/speaker-groups/{gid}", json={"name": "x"})).status_code == 403
    assert (await api.delete(f"/speaker-groups/{gid}")).status_code == 403


async def test_group_resolution_ignores_inactive(admin, db) -> None:
    a, off, nomaa = await _speakers(
        db, ("Actif", "p1", True), ("Eteint", "p2", False), ("SansMA", None, True)
    )
    g = await _group(admin, [a, off, nomaa])
    r = await admin.get(f"/speaker-groups/{g['id']}/resolve")
    assert r.status_code == 200
    assert sorted(s["name"] for s in r.json()["speakers"]) == ["Actif", "SansMA"]
    assert r.json()["player_ids"] == ["p1"]  # seules celles avec ma_player_id comptent


async def test_group_resolution_errors(admin, db) -> None:
    (off,) = await _speakers(db, ("Eteint", None, False))
    empty = await _group(admin, [off])
    r = await admin.get(f"/speaker-groups/{empty['id']}/resolve")
    assert r.status_code == 409 and "no active speaker" in r.json()["detail"]
    (ok,) = await _speakers(db, ("Ok", None, True))
    inactive = await _group(admin, [ok], is_active=False)
    assert (await admin.get(f"/speaker-groups/{inactive['id']}/resolve")).status_code == 409
    assert (await admin.get(f"/speaker-groups/{uuid.uuid4()}/resolve")).status_code == 404


# --- annonces ---------------------------------------------------------------


async def _announcement(db, ann_id: str) -> Announcement:
    async with db() as s:
        return await s.get(Announcement, uuid.UUID(ann_id))


async def test_announcement_group_union_deduped(admin, db) -> None:
    a, b, off = await _speakers(db, ("A", None, True), ("B", None, True), ("Off", None, False))
    g1 = await _group(admin, [a, b, off])
    g2 = await _group(admin, [b])
    r = await admin.post(
        "/announcements",
        json={"text": "Bonjour", "speaker_ids": [a], "group_ids": [g1["id"], g2["id"]]},
    )
    assert r.status_code == 202, r.text
    ann = await _announcement(db, r.json()["id"])
    assert sorted(t["name"] for t in ann.targets) == ["A", "B"]  # dédoublonné, inactive ignorée


async def test_announcement_group_errors(admin, db) -> None:
    (off,) = await _speakers(db, ("Off", None, False))
    empty = await _group(admin, [off])
    r = await admin.post("/announcements", json={"text": "x", "group_ids": [empty["id"]]})
    assert r.status_code == 409
    r = await admin.post("/announcements", json={"text": "x", "group_ids": [str(uuid.uuid4())]})
    assert r.status_code == 404
    assert (await admin.post("/announcements", json={"text": "x"})).status_code == 422


# --- scènes : CRUD et droits ------------------------------------------------


async def test_scene_crud_audit_and_validation(admin, db) -> None:
    (a,) = await _speakers(db, ("A", "p1", True))
    g = await _group(admin, [a])
    sc = await _scene(admin, group_id=g["id"], volume=40, shuffle=True, repeat="all", icon="album")
    assert sc["group_name"] == g["name"] and sc["mode"] == "play" and sc["volume"] == 40
    base = {"media_uri": "library://album/2", "media_type": "album"}
    for bad in (
        {"name": _name(), **base},  # aucune cible
        {"name": _name(), **base, "group_id": g["id"], "volume": 101},
        {"name": _name(), **base, "group_id": g["id"], "media_type": "artist"},
        {"name": _name(), **base, "group_id": g["id"], "mode": "next"},
        {"name": _name(), **base, "group_id": g["id"], "color": "red"},
        {"name": _name(), **base, "group_id": g["id"], "media_uri": "a b"},
        {"name": _name(), **base, "group_id": str(uuid.uuid4())},
    ):
        assert (await admin.post("/scenes", json=bad)).status_code in (404, 422), bad
    dup = {"name": sc["name"], **base, "group_id": g["id"]}
    assert (await admin.post("/scenes", json=dup)).status_code == 409
    url = f"/scenes/{sc['id']}"
    r = await admin.patch(url, json={"volume": None, "mode": "replace"})
    assert r.json()["volume"] is None and r.json()["mode"] == "replace"
    assert (await admin.patch(url, json={"group_id": None})).status_code == 422  # plus de cible
    r = await admin.patch(url, json={"speaker_ids": [a], "group_id": None})
    assert r.status_code == 200 and r.json()["speaker_ids"] == [a]
    assert (await admin.delete(url)).status_code == 204
    for act in ("scene_create", "scene_update", "scene_delete"):
        assert await _actions(db, act)


async def test_group_used_by_scene_cannot_be_deleted(admin, db) -> None:
    (a,) = await _speakers(db, ("A", "p1", True))
    g = await _group(admin, [a])
    await _scene(admin, group_id=g["id"])
    assert (await admin.delete(f"/speaker-groups/{g['id']}")).status_code == 409


async def test_scene_rights(api, make_user, login_as, db) -> None:
    (a,) = await _speakers(db, ("A", "p1", True))
    await login_as(await make_user(Role.admin))
    sc = await _scene(api, speaker_ids=[a])
    await login_as(await make_user(Role.staff))
    assert (await api.get("/scenes")).status_code == 200
    assert (await api.get(f"/scenes/{sc['id']}")).status_code == 200
    body = {"name": _name(), "media_uri": "library://track/1", "media_type": "track"}
    assert (await api.post("/scenes", json={**body, "speaker_ids": [a]})).status_code == 403
    assert (await api.patch(f"/scenes/{sc['id']}", json={"volume": 1})).status_code == 403
    assert (await api.delete(f"/scenes/{sc['id']}")).status_code == 403


async def test_scene_launch_requires_auth_and_staff(api, make_user, login_as) -> None:
    sid = uuid.uuid4()
    assert (await api.post(f"/scenes/{sid}/launch")).status_code == 401
    await login_as(await make_user(Role.staff))
    assert (await api.post(f"/scenes/{sid}/launch")).status_code == 404


# --- scènes : lancement -----------------------------------------------------


async def test_scene_launch_commands_and_filtering(admin, db, fake_ma, make_user, login_as) -> None:
    ok, off, nomaa, dup = await _speakers(
        db,
        ("Ok", "p1", True),
        ("Off", "p2", False),
        ("SansMA", None, True),
        ("Meme player", "p1", True),
    )
    g = await _group(admin, [ok, off, nomaa, dup])
    sc = await _scene(
        admin,
        group_id=g["id"],
        speaker_ids=[ok],  # doublon avec le groupe
        volume=35,
        shuffle=True,
        repeat="off",
        mode="replace",
        media_uri="library://playlist/42",
    )
    await login_as(await make_user(Role.staff))  # le staff peut lancer
    fake_ma.cmds.clear()
    r = await admin.post(f"/scenes/{sc['id']}/launch")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["ok"]) == 1 and body["failed"] == [] and body["partial"] is False
    cmds = [(c, a) for c, a in fake_ma.cmds if c != "players/all"]
    assert [c for c, _ in cmds] == [
        "players/cmd/volume_set",
        "player_queues/play_media",
        "player_queues/shuffle",
        "player_queues/repeat",
    ]  # un seul player (p1), actif et déclaré
    assert cmds[0][1] == {"player_id": "p1", "volume_level": 35}
    assert cmds[1][1] == {"queue_id": "p1", "media": "library://playlist/42", "option": "replace"}
    assert cmds[2][1] == {"queue_id": "p1", "shuffle_enabled": True}
    assert cmds[3][1] == {"queue_id": "p1", "repeat_mode": "off"}
    (log,) = await _launches(db, sc["id"])
    assert log.detail["name"] == sc["name"] and log.detail["players"] == ["p1"]
    assert log.detail["ok"] == 1 and "text" not in log.detail and log.actor


async def test_scene_launch_without_optionals_sends_only_play(admin, db, fake_ma) -> None:
    (a,) = await _speakers(db, ("A", "p1", True))
    sc = await _scene(admin, speaker_ids=[a])
    fake_ma.cmds.clear()
    assert (await admin.post(f"/scenes/{sc['id']}/launch")).status_code == 200
    assert [c for c, _ in fake_ma.cmds] == ["player_queues/play_media"]
    assert fake_ma.cmds[0][1]["option"] == "play"


async def test_scene_launch_partial_and_total_failure(admin, db, monkeypatch) -> None:
    a, b = await _speakers(db, ("Alpha", "p1", True), ("Beta", "p2", True))
    sc = await _scene(admin, speaker_ids=[a, b])
    real = MAClient.play_media

    async def flaky(self, queue_id, uri, option=None):
        if queue_id == "p2":
            raise MAConnectionError("down")
        return await real(self, queue_id, uri, option)

    monkeypatch.setattr(MAClient, "play_media", flaky)
    r = await admin.post(f"/scenes/{sc['id']}/launch")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] == ["Alpha"] and r.json()["partial"] is True
    assert r.json()["failed"] == [{"player": "Beta", "error": "unreachable"}]

    async def down(self, queue_id, uri, option=None):
        raise MAConnectionError("down")

    monkeypatch.setattr(MAClient, "play_media", down)
    assert (await admin.post(f"/scenes/{sc['id']}/launch")).status_code == 502
    launches = await _launches(db, sc["id"])
    assert len(launches) == 2 and launches[-1].detail["ok"] == 0  # l'échec total est audité


async def test_scene_launch_blocked_during_incident(admin, db, fake_ma) -> None:
    (a,) = await _speakers(db, ("A", "p1", True))
    sc = await _scene(admin, speaker_ids=[a])
    async with db() as s:
        inc = Incident(type="autre", title="t", message="m", status="sent")
        s.add(inc)
        await s.commit()
    fake_ma.cmds.clear()
    r = await admin.post(f"/scenes/{sc['id']}/launch")
    assert r.status_code == 409 and "incident" in r.json()["detail"]
    assert fake_ma.cmds == []
    async with db() as s:
        await s.execute(update(Incident).where(Incident.id == inc.id).values(status="closed"))
        await s.commit()
    assert (await admin.post(f"/scenes/{sc['id']}/launch")).status_code == 200


async def test_scene_launch_aborted_if_incident_during_announcement_wait(
    admin, db, fake_ma, monkeypatch
) -> None:
    from app import scenes

    (a,) = await _speakers(db, ("A", "p1", True))
    async with db() as s:
        p = Preset(title="Annonce", text="Texte")
        s.add(p)
        await s.commit()
    sc = await _scene(admin, speaker_ids=[a], preset_id=str(p.id))

    async def wait_then_alert(session, ann_id, timeout):  # noqa: ASYNC109
        async with db() as s:
            s.add(Incident(type="autre", title="t", message="m", status="sent"))
            await s.commit()
        return "done"

    monkeypatch.setattr(scenes, "_wait_announcement", wait_then_alert)
    fake_ma.cmds.clear()
    r = await admin.post(f"/scenes/{sc['id']}/launch")
    assert r.status_code == 409 and "incident" in r.json()["detail"]
    assert fake_ma.cmds == []  # aucune lecture
    assert [
        x for x in await _actions(db, "scene_launch_refused") if x.detail["scene_id"] == sc["id"]
    ]


async def test_scene_launch_clear_errors(admin, db) -> None:
    (off,) = await _speakers(db, ("Off", "p1", False))
    empty = await _group(admin, [off])
    sc = await _scene(admin, group_id=empty["id"])
    r = await admin.post(f"/scenes/{sc['id']}/launch")
    assert r.status_code == 409 and "no active speaker" in r.json()["detail"]
    (nomaa,) = await _speakers(db, ("SansMA", None, True))
    sc2 = await _scene(admin, speaker_ids=[nomaa])
    r = await admin.post(f"/scenes/{sc2['id']}/launch")
    assert r.status_code == 409 and "music player" in r.json()["detail"]
    await admin.patch(f"/scenes/{sc2['id']}", json={"is_active": False})
    assert (await admin.post(f"/scenes/{sc2['id']}/launch")).status_code == 409
    assert (await admin.post(f"/scenes/{uuid.uuid4()}/launch")).status_code == 404


async def test_scene_launch_with_announcement(admin, db) -> None:
    (a,) = await _speakers(db, ("A", "p1", True))
    async with db() as s:
        p = Preset(title="Bienvenue", text="Bienvenue à tous")
        s.add(p)
        await s.commit()
    sc = await _scene(admin, speaker_ids=[a], preset_id=str(p.id))
    r = await admin.post(f"/scenes/{sc['id']}/launch")
    assert r.status_code == 200, r.text
    assert r.json()["announcement"] == "pending"  # pas de worker dans le test
    (log,) = await _launches(db, sc["id"])
    ann = await _announcement(db, log.detail["announcement_id"])
    assert ann.is_test is False and ann.text == "Bienvenue à tous"
    assert [t["name"] for t in ann.targets] == ["A"]
    assert "Bienvenue" not in str(log.detail)  # le texte n'est jamais journalisé ici


# --- UI ---------------------------------------------------------------------


async def test_ui_pages(admin, db) -> None:
    ok, off = await _speakers(db, ("Salon", "p1", True), ("Eteinte", "p2", False))
    g = await _group(admin, [ok, off])
    sc = await _scene(admin, group_id=g["id"], icon="album", color="#336699")
    hidden = await _scene(admin, group_id=g["id"], is_active=False)
    r = await admin.get("/music")
    assert r.status_code == 200
    assert f'data-scene="{sc["id"]}"' in r.text and "--tone: #336699" in r.text
    assert hidden["id"] not in r.text
    assert f'data-pick-group="{g["id"]}"' in r.text
    r = await admin.get("/announce")
    assert f'value="{g["id"]}"' in r.text and 'name="group_ids"' in r.text
    r = await admin.get("/ui/admin/speaker-groups")
    assert g["name"] in r.text and 'name="member_ids"' in r.text
    r = await admin.get("/ui/admin/scenes")
    assert sc["name"] in r.text and "Nouvelle scène" in r.text
    r = await admin.get("/admin/enceintes")
    assert "Groupes d'enceintes" in r.text and "ui/admin/speaker-groups" in r.text
    assert "ui/admin/scenes" in (await admin.get("/admin/musique")).text


async def test_ui_staff_cannot_open_admin_fragments(api, make_user, login_as) -> None:
    await login_as(await make_user(Role.staff))
    assert (await api.get("/ui/admin/speaker-groups")).status_code == 403
    assert (await api.get("/ui/admin/scenes")).status_code == 403
    assert (await api.get("/music")).status_code == 200
