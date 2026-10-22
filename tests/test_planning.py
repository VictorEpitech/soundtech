# ruff: noqa: F811
import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select, update

from app import worker
from app.config import get_settings
from app.ha import queue
from app.ha.router import get_redis
from app.ma.client import MAClient
from app.main import app
from app.models import (
    Announcement,
    AuditLog,
    Closure,
    Incident,
    PlanningSettings,
    Role,
    Scene,
    Schedule,
    ScheduleRun,
    Speaker,
)
from app.planning import service
from tests.test_ha import _eid, redis  # noqa: F401
from tests.test_ma import TOKEN, FakeMA, _ma_settings, fake_ma  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_db")

# lundi 2026-01-05 08:00 Europe/Paris (CET) = 07:00 UTC
SLOT = datetime(2026, 1, 5, 7, 0, tzinfo=UTC)
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
async def env(db, redis, fake_ma):
    async with db() as s:
        await s.execute(delete(Schedule))
        await s.execute(delete(Closure))
        await s.execute(delete(PlanningSettings))
        await s.execute(update(Incident).where(Incident.status == "sent").values(status="closed"))
        await s.commit()
    await redis.delete(service.TICK_LOCK)
    settings = _ma_settings(fake_ma).model_copy(
        update={"ha_url": "http://ha.test", "ha_token": SecretStr("tok")}
    )
    return settings


def _ma(fake: FakeMA) -> MAClient:
    return MAClient(fake.url, TOKEN, timeout=2)


async def _speaker(db, **kw: Any) -> Speaker:
    async with db() as s:
        sp = Speaker(entity_id=_eid(), name="Salle", notify_service="notify.alexa_x", **kw)
        s.add(sp)
        await s.commit()
        return sp


async def _mk(db, sp: Speaker | None = None, **kw: Any) -> Schedule:
    base: dict[str, Any] = {
        "name": "s-" + uuid.uuid4().hex[:6],
        "days": [1, 2, 3, 4, 5, 6, 7],
        "time_of_day": "08:00",
        "action": "announcement",
        "text": "Fin des cours",
        "speaker_ids": [str(sp.id)] if sp else [],
        "armed_at": LONG_AGO,
        "created_by_email": "boss@epitech.eu",
    }
    async with db() as s:
        sc = Schedule(**{**base, **kw})
        s.add(sc)
        await s.commit()
        return sc


async def _runs(db) -> list[ScheduleRun]:
    async with db() as s:
        return list(await s.scalars(select(ScheduleRun).order_by(ScheduleRun.scheduled_for)))


async def _count(db, model: Any) -> int:
    async with db() as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


async def _tick(db, redis, env, fake, now: datetime) -> int:
    return await service.tick(db, redis, env, _ma(fake), now=now)


# --- exécution ----------------------------------------------------------------


async def test_tick_runs_announcement_staff_priority(db, redis, env, fake_ma) -> None:
    sp = await _speaker(db)
    sc = await _mk(db, sp)
    assert await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=20)) == 1
    [run] = await _runs(db)
    assert (run.status, run.scheduled_for, run.trigger) == ("ok", SLOT, "schedule")
    assert run.executed_at is not None and "Fin des cours" not in (run.detail or "")
    async with db() as s:
        ann = await s.scalar(select(Announcement).order_by(Announcement.created_at.desc()))
        assert ann is not None
        assert (ann.priority, ann.is_test, ann.author_email) == (2, False, "boss@epitech.eu")
        assert ann.text == "Fin des cours"
        assert await s.scalar(select(Schedule.last_run_at).where(Schedule.id == sc.id))
        audit = await s.scalar(select(AuditLog).where(AuditLog.action == "schedule_run"))
        assert audit is not None and audit.actor == "scheduler"
        assert audit.detail["status"] == "ok" and "Fin des cours" not in str(audit.detail)
    score = await redis.zscore(queue.QUEUE_KEY, str(ann.id))
    assert score is not None and score >= 2 * queue.SCALE  # jamais devant une alerte (niveau 0)


async def test_double_tick_is_idempotent(db, redis, env, fake_ma) -> None:
    await _mk(db, await _speaker(db))
    now = SLOT + timedelta(seconds=30)
    assert await _tick(db, redis, env, fake_ma, now) == 1
    assert await _tick(db, redis, env, fake_ma, now + timedelta(seconds=30)) == 0
    assert len(await _runs(db)) == 1 and await _count(db, Announcement) >= 1
    before = await _count(db, Announcement)
    await _tick(db, redis, env, fake_ma, now + timedelta(minutes=2))
    assert await _count(db, Announcement) == before


async def test_concurrent_ticks_run_once(db, redis, env, fake_ma) -> None:
    await _mk(db, await _speaker(db))
    now = SLOT + timedelta(seconds=30)
    before = await _count(db, Announcement)
    await asyncio.gather(*(_tick(db, redis, env, fake_ma, now) for _ in range(4)))
    assert len(await _runs(db)) == 1
    assert await _count(db, Announcement) == before + 1


async def test_unique_constraint_blocks_duplicate_even_without_lock(db, env) -> None:
    sc = await _mk(db, await _speaker(db))
    async with db() as a, db() as b:
        assert await service._claim(a, sc, SLOT, "schedule") is not None
        assert await service._claim(b, sc, SLOT, "schedule") is None


async def test_tick_skipped_when_lock_held(db, redis, env, fake_ma) -> None:
    await _mk(db, await _speaker(db))
    await redis.set(service.TICK_LOCK, "other", ex=30)
    assert await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=5)) == 0
    assert await _runs(db) == []


async def test_catchup_within_5_minutes_then_missed(db, redis, env, fake_ma) -> None:
    sp = await _speaker(db)
    await _mk(db, sp, days=[1])
    assert await _tick(db, redis, env, fake_ma, SLOT + timedelta(minutes=4, seconds=59)) == 1
    assert [r.status for r in await _runs(db)] == ["ok"]
    await _mk(db, sp, days=[1], time_of_day="09:00", name="late")  # 08:00 UTC+1 -> 08:00 UTC
    before = await _count(db, Announcement)
    await _tick(db, redis, env, fake_ma, datetime(2026, 1, 5, 8, 6, tzinfo=UTC))
    runs = {r.schedule_name: r for r in await _runs(db)}
    assert runs["late"].status == "missed"
    assert await _count(db, Announcement) == before  # rien exécuté


async def test_missed_after_worker_downtime_recorded_once(db, redis, env, fake_ma) -> None:
    await _mk(db, await _speaker(db), days=[1])
    later = SLOT + timedelta(hours=3)
    assert await _tick(db, redis, env, fake_ma, later) == 1
    assert [r.status for r in await _runs(db)] == ["missed"]
    assert await _tick(db, redis, env, fake_ma, later + timedelta(minutes=1)) == 0


async def test_no_catchup_before_armed_or_when_inactive(db, redis, env, fake_ma) -> None:
    sp = await _speaker(db)
    await _mk(db, sp, armed_at=SLOT + timedelta(minutes=1))
    await _mk(db, sp, is_active=False)
    assert await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=90)) == 0
    assert await _runs(db) == []


async def test_skipped_during_alert(db, redis, env, fake_ma) -> None:
    await _mk(db, await _speaker(db), days=[1])
    async with db() as s:
        s.add(Incident(type="incendie", title="t", message="m", status="sent"))
        await s.commit()
    before = await _count(db, Announcement)
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    [run] = await _runs(db)
    assert run.status == "skipped_alert"
    assert await _count(db, Announcement) == before


async def test_closures_and_holidays(db, redis, env, fake_ma) -> None:
    sp = await _speaker(db)
    await _mk(db, sp, days=[1], name="strict")
    await _mk(db, sp, days=[1], name="always", respect_closures=False)
    async with db() as s:
        s.add(
            Closure(
                label="Vacances",
                kind="vacances",
                start_date=date(2026, 1, 3),
                end_date=date(2026, 1, 9),
            )
        )
        await s.commit()
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    got = {r.schedule_name: r.status for r in await _runs(db)}
    assert got == {"strict": "skipped_closure", "always": "ok"}


async def test_public_holiday_switch(db, redis, env, fake_ma) -> None:
    sp = await _speaker(db)
    noel = datetime(2026, 12, 25, 7, 0, tzinfo=UTC)  # vendredi
    await _mk(db, sp, days=[5])
    await _tick(db, redis, env, fake_ma, noel + timedelta(seconds=10))
    assert [r.status for r in await _runs(db)] == ["skipped_closure"]
    async with db() as s:
        s.add(PlanningSettings(id=1, observe_holidays=False))
        await s.delete(await s.scalar(select(ScheduleRun)))
        await s.commit()
    await _tick(db, redis, env, fake_ma, noel + timedelta(seconds=40))
    assert [r.status for r in await _runs(db)] == ["ok"]


async def test_test_flag_prefixes_announcement(db, redis, env, fake_ma) -> None:
    sp = await _speaker(db)
    await _mk(db, sp, days=[1], is_test=True)
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    async with db() as s:
        ann = await s.scalar(select(Announcement).order_by(Announcement.created_at.desc()))
    assert ann is not None and ann.is_test and ann.text.startswith(env.test_prefix)


async def test_failed_is_recorded_not_raised(db, redis, env, fake_ma) -> None:
    await _mk(db, await _speaker(db), days=[1], speaker_ids=[])  # aucune cible
    assert await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10)) == 1
    [run] = await _runs(db)
    assert run.status == "failed" and run.detail == "aucune enceinte active ciblée"


async def test_scene_and_stop_actions(db, redis, env, fake_ma) -> None:
    sp = await _speaker(db, ma_player_id="p1")
    async with db() as s:
        scene = Scene(
            name="sc-" + uuid.uuid4().hex[:6],
            media_uri="library://playlist/1",
            media_type="playlist",
            speakers=[await s.get(Speaker, sp.id)],
        )
        s.add(scene)
        await s.commit()
    await _mk(db, days=[1], action="scene", scene_id=scene.id, text=None, name="scene")
    await _mk(db, sp, days=[1], action="stop", text=None, stop_volume=5, name="stop")
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    got = {r.schedule_name: r.status for r in await _runs(db)}
    assert got == {"scene": "ok", "stop": "ok"}
    cmds = [c for c, _ in fake_ma.cmds]
    assert "player_queues/play_media" in cmds and "player_queues/pause" in cmds
    assert ("players/cmd/volume_set", {"player_id": "p1", "volume_level": 5}) in fake_ma.cmds
    async with db() as s:
        acts = set(await s.scalars(select(AuditLog.actor).where(AuditLog.action == "scene_launch")))
    assert "scheduler" in acts


async def test_scene_deleted_fails(db, redis, env, fake_ma) -> None:
    await _mk(db, days=[1], action="scene", scene_id=None, text=None)
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    [run] = await _runs(db)
    assert (run.status, run.detail) == ("failed", "scène supprimée")


async def test_stale_running_is_reaped(db, redis, env, fake_ma) -> None:
    sc = await _mk(db, is_active=False)
    async with db() as s:
        s.add(ScheduleRun(schedule_id=sc.id, scheduled_for=SLOT, status="running"))
        await s.commit()
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(minutes=30))
    [run] = await _runs(db)
    assert (run.status, run.detail) == ("failed", "interrompue")


def test_worker_registers_minute_cron() -> None:
    names = {j.name for j in worker.WorkerSettings.cron_jobs}
    assert "cron:run_schedules" in names


# --- API ----------------------------------------------------------------------


@pytest.fixture
async def api(client: AsyncClient, env, redis, db) -> AsyncClient:
    app.dependency_overrides[get_settings] = lambda: env
    app.dependency_overrides[get_redis] = lambda: redis
    return client


@pytest.fixture
async def admin(api: AsyncClient, make_user, login_as) -> AsyncClient:
    await login_as(await make_user(Role.admin))
    return api


def _body(sp: Speaker | None = None, **kw: Any) -> dict[str, Any]:
    return {
        "name": "n-" + uuid.uuid4().hex[:6],
        "days": [1, 3],
        "time_of_day": "17:30",
        "action": "announcement",
        "text": "Bonsoir",
        "speaker_ids": [str(sp.id)] if sp else [],
        **kw,
    }


async def test_crud_audit_and_preview(admin, db) -> None:
    sp = await _speaker(db)
    r = await admin.post("/schedules", json=_body(sp))
    assert r.status_code == 201, r.text
    sc = r.json()
    assert sc["timezone"] == "Europe/Paris" and sc["respect_closures"] is True
    assert sc["next_run"] and sc["days"] == [1, 3]
    r = await admin.patch(f"/schedules/{sc['id']}", json={"time_of_day": "18:00", "days": [2]})
    assert r.status_code == 200 and r.json()["time_of_day"] == "18:00"
    r = await admin.patch(f"/schedules/{sc['id']}", json={"is_active": False})
    assert r.json()["is_active"] is False and r.json()["next_run"] is None
    await admin.patch(f"/schedules/{sc['id']}", json={"is_active": True})
    prev = (await admin.get(f"/schedules/{sc['id']}/preview")).json()
    assert len(prev["next"]) == 10 and prev["next"][0]["local"].endswith("18:00")
    assert len((await admin.get("/schedules")).json()) == 1
    assert (await admin.delete(f"/schedules/{sc['id']}")).status_code == 204
    assert (await admin.get(f"/schedules/{sc['id']}")).status_code == 404
    async with db() as s:
        acts = set(await s.scalars(select(AuditLog.action)))
    assert {"schedule_create", "schedule_update", "schedule_delete"} <= acts


async def test_preview_skips_closed_days(admin, db) -> None:
    sp = await _speaker(db)
    today = datetime.now(UTC).date()
    sc = (await admin.post("/schedules", json=_body(sp, days=[1, 2, 3, 4, 5, 6, 7]))).json()
    await admin.post(
        "/closures",
        json={
            "label": "Fermé",
            "kind": "fermeture",
            "start_date": str(today),
            "end_date": str(today + timedelta(days=20)),
        },
    )
    prev = (await admin.get(f"/schedules/{sc['id']}/preview")).json()["next"]
    assert len(prev) == 10
    assert min(n["local"][:10] for n in prev) > str(today + timedelta(days=20))
    sc2 = (
        await admin.post(
            "/schedules", json=_body(sp, days=[1, 2, 3, 4, 5, 6, 7], respect_closures=False)
        )
    ).json()
    first = (await admin.get(f"/schedules/{sc2['id']}/preview")).json()["next"][0]["local"][:10]
    assert first <= str(today + timedelta(days=1))


@pytest.mark.parametrize(
    "patch",
    [
        {"time_of_day": "24:00"},
        {"time_of_day": "8:00"},
        {"days": [0]},
        {"days": [8]},
        {"days": []},
        {"timezone": "Mars/Phobos"},
        {"once_date": "2099-01-02"},  # jours ET date unique
        {"text": None, "preset_id": None},
        {"text": "x", "preset_id": str(uuid.uuid4())},
        {"speaker_ids": []},
        {"speaker_ids": [str(uuid.uuid4())]},
        {"action": "scene"},
        {"action": "scene", "scene_id": str(uuid.uuid4())},
        {"action": "stop", "stop_volume": 101},
        {"voice": "Nope"},
        {"days": [], "once_date": "2001-01-01"},
        {"name": "   "},
    ],
)
async def test_validation_rejected(admin, db, patch) -> None:
    sp = await _speaker(db)
    r = await admin.post("/schedules", json={**_body(sp), **patch})
    assert r.status_code == 422, (patch, r.text)


async def test_once_date_and_scene_ok(admin, db) -> None:
    sp = await _speaker(db)
    r = await admin.post("/schedules", json=_body(sp, days=[], once_date="2099-01-02"))
    assert r.status_code == 201 and r.json()["next_run"].startswith("2099-01-02T")
    async with db() as s:
        scene = Scene(
            name="sc-" + uuid.uuid4().hex[:6], media_uri="library://x/1", media_type="track"
        )
        s.add(scene)
        await s.commit()
    r = await admin.post(
        "/schedules",
        json={
            "name": "sc",
            "days": [2],
            "time_of_day": "07:00",
            "action": "scene",
            "scene_id": str(scene.id),
            "text": "ignoré",
        },
    )
    assert r.status_code == 201 and r.json()["text"] is None


async def test_rights_staff_read_only(api, db, make_user, login_as) -> None:
    sp = await _speaker(db)
    sc = await _mk(db, sp)
    await login_as(await make_user(Role.staff))
    assert (await api.get("/schedules")).status_code == 200
    assert (await api.get(f"/schedules/{sc.id}/preview")).status_code == 200
    assert (await api.get("/schedules/runs")).status_code == 200
    assert (await api.get("/closures")).status_code == 200
    assert (await api.post("/schedules", json=_body(sp))).status_code == 403
    assert (await api.patch(f"/schedules/{sc.id}", json={"is_active": False})).status_code == 403
    assert (await api.delete(f"/schedules/{sc.id}")).status_code == 403
    assert (await api.post(f"/schedules/{sc.id}/run")).status_code == 403
    assert (await api.post("/closures", json={})).status_code == 403
    assert (
        await api.put("/closures/settings", json={"observe_holidays": False})
    ).status_code == 403
    assert (await api.get("/admin/planning")).status_code == 403


async def test_anonymous_rejected(api) -> None:
    assert (await api.get("/schedules")).status_code == 401
    assert (await api.get("/closures")).status_code == 401


async def test_run_now_and_alert_refusal(admin, db, redis) -> None:
    sp = await _speaker(db)
    sc = await _mk(db, sp)
    r = await admin.post(f"/schedules/{sc.id}/run")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["trigger"] == "manual"
    async with db() as s:
        s.add(Incident(type="incendie", title="t", message="m", status="sent"))
        await s.commit()
    r = await admin.post(f"/schedules/{sc.id}/run")
    assert r.status_code == 409
    runs = (await admin.get("/schedules/runs", params={"schedule_id": str(sc.id)})).json()
    assert [x["status"] for x in runs] == ["skipped_alert", "ok"]
    only = (await admin.get("/schedules/runs", params={"status": "ok"})).json()
    assert [x["status"] for x in only] == ["ok"]
    assert (await admin.get("/schedules/runs", params={"status": "bogus"})).status_code == 422


async def test_closures_api(admin) -> None:
    cal = (await admin.get("/closures", params={"year": 2026})).json()
    assert cal["observe_holidays"] is True and len(cal["holidays"]) == 11
    r = await admin.post(
        "/closures",
        json={
            "label": "Noël",
            "kind": "vacances",
            "start_date": "2026-12-19",
            "end_date": "2027-01-03",
        },
    )
    assert r.status_code == 201
    cid = r.json()["id"]
    assert (
        await admin.post(
            "/closures",
            json={
                "label": "x",
                "kind": "vacances",
                "start_date": "2026-02-02",
                "end_date": "2026-02-01",
            },
        )
    ).status_code == 422
    assert (
        await admin.post(
            "/closures",
            json={
                "label": "x",
                "kind": "zzz",
                "start_date": "2026-02-01",
                "end_date": "2026-02-02",
            },
        )
    ).status_code == 422
    assert (
        await admin.post(
            "/closures",
            json={
                "label": "x",
                "kind": "vacances",
                "start_date": "2026-01-01",
                "end_date": "2028-01-01",
            },
        )
    ).status_code == 422
    r = await admin.patch(f"/closures/{cid}", json={"is_active": False, "end_date": "2027-01-04"})
    assert r.json()["is_active"] is False and r.json()["end_date"] == "2027-01-04"
    assert (
        await admin.patch(f"/closures/{cid}", json={"end_date": "2026-01-01"})
    ).status_code == 422
    assert (await admin.put("/closures/settings", json={"observe_holidays": False})).json() == {
        "observe_holidays": False
    }
    assert (await admin.get("/closures")).json()["observe_holidays"] is False
    assert (
        await admin.put("/closures/settings", json={"observe_holidays": True})
    ).status_code == 200
    assert (await admin.delete(f"/closures/{cid}")).status_code == 204
    assert (await admin.delete(f"/closures/{cid}")).status_code == 404


async def test_ui_pages(admin, db) -> None:
    sp = await _speaker(db)
    sc = await _mk(db, sp)
    await admin.post(f"/schedules/{sc.id}/run")
    page = await admin.get("/admin/planning")
    assert page.status_code == 200 and "Planification" in page.text
    frag = await admin.get("/ui/planning/schedules")
    assert frag.status_code == 200 and sc.name in frag.text and 'name="days"' in frag.text
    assert (await admin.get(f"/ui/planning/preview/{sc.id}")).status_code == 200
    assert (await admin.get("/ui/planning/closures")).status_code == 200
    runs = await admin.get("/ui/planning/runs", params={"schedule_id": str(sc.id), "status": "ok"})
    assert runs.status_code == 200 and "manuel" in runs.text
    assert (await admin.get("/ui/planning/runs", params={"status": "zzz"})).status_code == 422
    assert (await admin.get("/ui/planning/runs", params={"schedule_id": ""})).status_code == 200


# --- robustesse du tick -------------------------------------------------------


async def test_parallel_execution_is_bounded(db, redis, env, fake_ma, monkeypatch) -> None:
    sp = await _speaker(db)
    for i in range(6):
        await _mk(db, sp, days=[1], name=f"p{i}")
    live = peak = 0

    async def slow(*a: Any, **k: Any) -> tuple[str, str]:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.3)
        live -= 1
        return "ok", "x"

    monkeypatch.setattr(service, "execute", slow)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10)) == 6
    assert 2 <= peak <= service.MAX_PARALLEL
    assert loop.time() - t0 < 1.5  # 6 x 0.3 s en série = 1.8 s
    assert {r.status for r in await _runs(db)} == {"ok"}


async def test_tick_budget_leaves_rest_for_next_tick(db, redis, env, fake_ma, monkeypatch) -> None:
    await _mk(db, await _speaker(db), days=[1])

    async def hang(*a: Any, **k: Any) -> tuple[str, str]:
        await asyncio.sleep(30)
        return "ok", "x"

    monkeypatch.setattr(service, "execute", hang)
    monkeypatch.setattr(service, "TICK_BUDGET", 0.2)
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    assert [r.status for r in await _runs(db)] == ["running"]
    assert not await redis.exists(service.TICK_LOCK)  # verrou relâché


async def test_reap_covers_manual_runs(db, redis, env, fake_ma) -> None:
    sc = await _mk(db, is_active=False)
    async with db() as s:
        s.add(
            ScheduleRun(schedule_id=sc.id, scheduled_for=SLOT, status="running", trigger="manual")
        )
        await s.commit()
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(minutes=30))
    [run] = await _runs(db)
    assert (run.status, run.detail) == ("failed", "interrompue")


async def test_alert_during_scene_is_skipped_alert(db, redis, env, fake_ma, monkeypatch) -> None:
    from fastapi import HTTPException

    from app import scenes

    async def boom(*a: Any, **k: Any) -> None:
        raise HTTPException(409, scenes.ALERT_IN_PROGRESS)

    monkeypatch.setattr(scenes, "launch_scene", boom)
    async with db() as s:
        scene = Scene(
            name="sc-" + uuid.uuid4().hex[:6], media_uri="library://x/1", media_type="track"
        )
        s.add(scene)
        await s.commit()
    await _mk(db, days=[1], action="scene", scene_id=scene.id, text=None)
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    [run] = await _runs(db)
    assert run.status == "skipped_alert"


@pytest.mark.parametrize("action", ["announcement", "stop"])
async def test_alert_recheck_just_before_send(db, redis, env, fake_ma, monkeypatch, action) -> None:
    from app import scenes

    sp = await _speaker(db, ma_player_id="p1")
    await _mk(db, sp, days=[1], action=action)
    calls = 0

    async def flaky(_: Any) -> bool:
        nonlocal calls
        calls += 1
        return calls > 1  # libre au contrôle initial, alerte juste avant l'envoi

    monkeypatch.setattr(scenes, "_incident_in_progress", flaky)
    before = await _count(db, Announcement)
    fake_ma.cmds.clear()
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    [run] = await _runs(db)
    assert run.status == "skipped_alert"
    assert await _count(db, Announcement) == before
    assert not [c for c, _ in fake_ma.cmds if c == "player_queues/pause"]


async def test_invalid_voice_is_clean_failure(db, redis, env, fake_ma, caplog) -> None:
    await _mk(db, await _speaker(db), days=[1], voice="Disparue")
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    [run] = await _runs(db)
    assert (run.status, run.detail) == ("failed", "voix/langue invalide")
    assert not [r for r in caplog.records if r.exc_info]


async def test_schedule_disabled_after_scan_is_skipped(
    db, redis, env, fake_ma, monkeypatch
) -> None:
    sc = await _mk(db, await _speaker(db), days=[1])
    orig = service.closure_checker

    async def hook(session: Any, a: Any, b: Any) -> Any:
        async with db() as s:  # désactivée entre le scan et l'exécution
            await s.execute(update(Schedule).where(Schedule.id == sc.id).values(is_active=False))
            await s.commit()
        return await orig(session, a, b)

    monkeypatch.setattr(service, "closure_checker", hook)
    before = await _count(db, Announcement)
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    assert await _runs(db) == [] and await _count(db, Announcement) == before


async def test_schedule_deleted_during_execution_keeps_audit(
    db, redis, env, fake_ma, monkeypatch
) -> None:
    sc = await _mk(db, await _speaker(db), days=[1])

    async def delete_then_ok(*a: Any, **k: Any) -> tuple[str, str]:
        async with db() as s:
            await s.execute(delete(Schedule).where(Schedule.id == sc.id))
            await s.commit()
        return "ok", "fait"

    monkeypatch.setattr(service, "execute", delete_then_ok)
    await _tick(db, redis, env, fake_ma, SLOT + timedelta(seconds=10))
    assert await _runs(db) == []
    async with db() as s:
        audits = list(await s.scalars(select(AuditLog).where(AuditLog.action == "schedule_run")))
    mine = [a for a in audits if a.detail["schedule_id"] == str(sc.id)]
    assert len(mine) == 1 and mine[0].detail["status"] == "ok"


async def test_run_now_scene_audit_attributed_to_admin(api, db, make_user, login_as) -> None:
    admin_user = await make_user(Role.admin)
    await login_as(admin_user)
    sp = await _speaker(db, ma_player_id="p1")
    async with db() as s:
        scene = Scene(
            name="sc-" + uuid.uuid4().hex[:6],
            media_uri="library://x/1",
            media_type="track",
            speakers=[await s.get(Speaker, sp.id)],
        )
        s.add(scene)
        await s.commit()
    sc = await _mk(db, days=[1], action="scene", scene_id=scene.id, text=None)
    r = await api.post(f"/schedules/{sc.id}/run")
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    async with db() as s:
        rows = await s.scalars(select(AuditLog).where(AuditLog.action == "scene_launch"))
        row = next((x for x in rows if x.detail.get("schedule_id") == str(sc.id)), None)
    assert row is not None and row.actor == admin_user.email and row.user_id == admin_user.id
