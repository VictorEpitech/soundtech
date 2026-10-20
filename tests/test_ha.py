import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis

from app.auth import deps
from app.config import Settings, get_settings
from app.ha import queue, service
from app.ha.client import (
    HAAuthError,
    HAClient,
    HAConfigError,
    HAConnectionError,
    HARequestError,
    HAServerError,
)
from app.ha.router import get_redis
from app.main import app
from app.models import Announcement, Priority, Role
from tests.helpers import _settings, audit_actions

TEST_REDIS = os.environ.get("TEST_REDIS_URL")
pytestmark = pytest.mark.usefixtures("migrated_db")


def _ha_settings() -> Settings:
    return _settings().model_copy(update={"ha_url": "http://ha.test", "ha_token": SecretStr("tok")})


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    if not TEST_REDIS:
        pytest.skip("TEST_REDIS_URL not set")
    r = Redis.from_url(TEST_REDIS)
    keys = (queue.QUEUE_KEY, queue.SEQ_KEY, queue.LOCK_KEY)
    await r.delete(*keys)
    yield r
    await r.delete(*keys)
    await r.aclose()


@pytest.fixture
def ha_api(client: AsyncClient, redis: Redis) -> AsyncClient:
    app.dependency_overrides[get_settings] = _ha_settings
    app.dependency_overrides[get_redis] = lambda: redis
    return client


def mock_ha(fail: set[str] | None = None) -> tuple[HAClient, list[Any]]:
    calls: list[Any] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append((req.url.path, req.headers["authorization"], body))
        entity = body["target"][0] if "target" in body else req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(500 if entity in (fail or set()) else 200, json=[])

    ha = HAClient(
        "http://ha.test",
        "tok",
        "notify.alexa_media",
        retries=1,
        backoff=0,
        transport=httpx.MockTransport(handler),
    )
    return ha, calls


async def _speaker(client: AsyncClient, entity: str, **kw: Any) -> str:
    r = await client.post("/speakers", json={"entity_id": entity, "name": entity, **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _eid() -> str:
    return f"media_player.t{uuid.uuid4().hex[:8]}"


# --- client / config --------------------------------------------------------


async def test_client_call_and_errors() -> None:
    ha, calls = mock_ha()
    await ha.speak("media_player.a", "salut")
    path, auth, body = calls[0]
    assert path == "/api/services/notify/alexa_media" and auth == "Bearer tok"
    assert body == {"message": "salut", "target": ["media_player.a"], "data": {"type": "tts"}}
    with pytest.raises(HAConfigError):
        HAClient("", "", "notify.x")

    def mk(handler: Callable[[httpx.Request], httpx.Response]) -> HAClient:
        return HAClient(
            "http://h",
            "t",
            "tts.speak",
            retries=2,
            backoff=0,
            transport=httpx.MockTransport(handler),
        )

    n = {"c": 0}

    def flaky(_: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(503 if n["c"] < 3 else 200)

    await mk(flaky).speak("media_player.a", "x")
    assert n["c"] == 3
    with pytest.raises(HAAuthError):
        await mk(lambda _: httpx.Response(401)).speak("media_player.a", "x")
    with pytest.raises(HARequestError):
        await mk(lambda _: httpx.Response(404)).speak("media_player.a", "x")
    with pytest.raises(HAServerError):
        await mk(lambda _: httpx.Response(500)).speak("media_player.a", "x")

    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(HAConnectionError):
        await mk(boom).speak("media_player.a", "x")


def test_settings_without_ha_ok_and_service_validated() -> None:
    s = Settings()
    assert not s.ha_configured and s.ha_tts_service == "notify.alexa_media"
    with pytest.raises(ValueError):
        Settings(ha_tts_service="nodot")


# --- API --------------------------------------------------------------------


async def test_roles(ha_api, make_user, login_as) -> None:
    assert (await ha_api.get("/speakers")).status_code == 401
    staff = await make_user(Role.staff)
    await login_as(staff)
    assert (await ha_api.get("/speakers")).status_code == 200
    assert (await ha_api.get("/presets")).status_code == 200
    r = await ha_api.post("/speakers", json={"entity_id": "media_player.x", "name": "x"})
    assert r.status_code == 403
    assert (await ha_api.post("/presets", json={"title": "t", "text": "x"})).status_code == 403


async def test_student_refused(ha_api, make_user) -> None:
    student = await make_user(Role.student)
    body = {"text": "a", "speaker_ids": [str(uuid.uuid4())]}
    assert (await ha_api.post("/announcements", json=body)).status_code == 401

    # les students ne peuvent pas se connecter ; on force l'utilisateur pour atteindre require_role.
    async def _cu() -> Any:
        return student

    app.dependency_overrides[deps.current_user] = _cu
    try:
        assert (await ha_api.post("/announcements", json=body)).status_code == 403
        assert (await ha_api.get("/announcements")).status_code == 403
        assert (await ha_api.get("/speakers")).status_code == 403
    finally:
        app.dependency_overrides.pop(deps.current_user)


async def test_crud_and_validation(ha_api, make_user, login_as, db) -> None:
    admin = await make_user(Role.admin)
    await login_as(admin)
    e = _eid()
    sid = await _speaker(ha_api, e, group="g")
    assert (await ha_api.post("/speakers", json={"entity_id": e, "name": "d"})).status_code == 409
    bad = {"entity_id": "BAD", "name": "d"}
    assert (await ha_api.post("/speakers", json=bad)).status_code == 422
    blank = {"entity_id": _eid(), "name": "  "}
    assert (await ha_api.post("/speakers", json=blank)).status_code == 422
    p = await ha_api.patch(f"/speakers/{sid}", json={"is_active": False, "group": None})
    assert p.json()["is_active"] is False and p.json()["group"] is None
    assert (await ha_api.patch(f"/speakers/{sid}", json={"name": None})).status_code == 422
    r = await ha_api.post("/presets", json={"title": "Feu", "text": "Evacuation"})
    pid = r.json()["id"]
    assert (await ha_api.post("/presets", json={"title": "x", "text": ""})).status_code == 422
    assert (await ha_api.patch(f"/presets/{pid}", json={"text": "Autre"})).json()["text"] == "Autre"
    assert (await ha_api.delete(f"/presets/{pid}")).status_code == 204
    assert (await ha_api.get(f"/presets/{pid}")).status_code == 404
    assert (await ha_api.delete(f"/speakers/{sid}")).status_code == 204
    acts = set(await audit_actions(db, admin.email))
    assert {"speaker_create", "speaker_update", "speaker_delete", "preset_create"} <= acts


async def test_announcement_validation(ha_api, make_user, login_as) -> None:
    await login_as(await make_user(Role.admin))
    sid = await _speaker(ha_api, _eid())
    off = await _speaker(ha_api, _eid(), is_active=False)
    url = "/announcements"

    async def post(**body: Any) -> int:
        return (await ha_api.post(url, json=body)).status_code

    assert await post(speaker_ids=[sid]) == 422
    assert await post(text="a", preset_id=str(uuid.uuid4()), speaker_ids=[sid]) == 422
    assert await post(text="a", speaker_ids=[]) == 422
    assert await post(text="   ", speaker_ids=[sid]) == 422
    assert await post(text="a" * 501, speaker_ids=[sid]) == 422
    assert await post(text="a", speaker_ids=["nope"]) == 422
    assert await post(text="a", speaker_ids=[str(uuid.uuid4())]) == 404
    assert await post(text="a", speaker_ids=[off]) == 409
    assert await post(preset_id=str(uuid.uuid4()), speaker_ids=[sid]) == 404
    # le client ne peut pas imposer de priorité
    r = await ha_api.post(url, json={"text": "a", "speaker_ids": [sid], "priority": 0})
    assert r.status_code == 202 and r.json()["priority"] == Priority.admin


async def test_ha_not_configured_503(client, redis, make_user, login_as) -> None:
    app.dependency_overrides[get_redis] = lambda: redis
    await login_as(await make_user(Role.admin))
    sid = await _speaker(client, _eid())
    r = await client.post("/announcements", json={"text": "a", "speaker_ids": [sid]})
    assert r.status_code == 503


async def test_test_prefix_forced_and_audited(ha_api, make_user, login_as, db, redis) -> None:
    staff = await make_user(Role.staff)
    await login_as(await make_user(Role.admin))
    sid = await _speaker(ha_api, _eid())
    pid = (await ha_api.post("/presets", json={"title": "p", "text": "Bonjour"})).json()["id"]
    await login_as(staff)
    r = await ha_api.post(
        "/announcements", json={"preset_id": pid, "speaker_ids": [sid], "is_test": True}
    )
    assert r.status_code == 202
    body = r.json()
    assert body["text"] == "Ceci est un test. Bonjour" and body["is_test"] is True
    assert body["priority"] == Priority.staff and body["status"] == "queued"
    r2 = await ha_api.post("/announcements", json={"text": "Réel", "speaker_ids": [sid]})
    assert r2.json()["text"] == "Réel" and r2.json()["is_test"] is False
    got = await ha_api.get(f"/announcements/{body['id']}")
    assert got.status_code == 200 and got.json()["id"] == body["id"]
    assert (await ha_api.get("/announcements")).status_code == 200
    assert (await ha_api.get(f"/announcements/{uuid.uuid4()}")).status_code == 404
    assert "announcement_send" in await audit_actions(db, staff.email)
    # texte vocal effectivement émis par le worker
    ha, calls = mock_ha()
    await service.drain(db, redis, ha)
    assert {c[2]["message"] for c in calls} == {"Ceci est un test. Bonjour", "Réel"}
    async with db() as s:
        done = await s.get(Announcement, uuid.UUID(body["id"]))
    assert done.status == "done" and done.is_test


# --- file / worker ----------------------------------------------------------


async def test_queue_order(redis) -> None:
    await queue.push(redis, "s1", Priority.staff)
    await queue.push(redis, "a1", Priority.admin)
    await queue.push(redis, "s2", Priority.staff)
    await queue.push(redis, "al", Priority.alert)
    await queue.push(redis, "a2", Priority.admin)
    order = [await queue.pop(redis) for _ in range(6)]
    assert order == ["al", "a1", "a2", "s1", "s2", None]


async def _mk_ann(db, speakers, priority, text="t") -> Announcement:
    ann = Announcement(
        id=uuid.uuid4(),
        text=text,
        priority=int(priority),
        status="queued",
        is_test=False,
        targets=[{"id": "x", "entity_id": e, "name": e} for e in speakers],
    )
    async with db() as s:
        s.add(ann)
        await s.commit()
    return ann


async def test_worker_priority_and_partial_failure(db, redis) -> None:
    a, b = _eid(), _eid()
    low = await _mk_ann(db, [a], Priority.staff, "bas")
    high = await _mk_ann(db, [a, b], Priority.alert, "haut")
    await queue.push(redis, str(low.id), Priority.staff)
    await queue.push(redis, str(high.id), Priority.alert)
    ha, calls = mock_ha(fail={b})
    assert await service.drain(db, redis, ha) == 2
    texts = [c[2]["message"] for c in calls]
    assert texts[0] == "haut" and texts[-1] == "bas"
    async with db() as s:
        h = await s.get(Announcement, high.id)
        lo = await s.get(Announcement, low.id)
    assert h.status == "failed" and h.results[a]["ok"] is True and h.results[b]["ok"] is False
    assert h.started_at and h.finished_at
    assert lo.status == "done" and lo.results[a] == {"ok": True}


async def test_notify_service_per_speaker(ha_api, make_user, login_as, db, redis) -> None:
    await login_as(await make_user(Role.admin))
    e1, e2 = _eid(), _eid()
    s1 = await _speaker(ha_api, e1, notify_service="notify.alexa_media_epitech_001")
    s2 = await _speaker(ha_api, e2)
    assert (await ha_api.get(f"/speakers/{s1}")).json()["notify_service"] == (
        "notify.alexa_media_epitech_001"
    )
    for bad in ("nodot", "Notify.X", "a.b.c", ""):
        r = await ha_api.post(
            "/speakers", json={"entity_id": _eid(), "name": "n", "notify_service": bad}
        )
        assert r.status_code == 422, bad
    p = await ha_api.patch(f"/speakers/{s2}", json={"notify_service": "notify.alexa_media_x"})
    assert p.status_code == 200 and p.json()["notify_service"] == "notify.alexa_media_x"
    assert (
        await ha_api.patch(f"/speakers/{s2}", json={"notify_service": "bad"})
    ).status_code == 422
    p = await ha_api.patch(f"/speakers/{s2}", json={"notify_service": None})
    assert p.json()["notify_service"] is None
    r = await ha_api.post("/announcements", json={"text": "yo", "speaker_ids": [s1, s2]})
    assert r.status_code == 202, r.text
    ha, calls = mock_ha()
    assert await service.drain(db, redis, ha) == 1
    by_path = {c[0]: c[2] for c in calls}
    assert by_path["/api/services/notify/alexa_media_epitech_001"] == {
        "message": "yo",
        "data": {"type": "tts"},
    }
    assert by_path["/api/services/notify/alexa_media"] == {
        "message": "yo",
        "target": [e2],
        "data": {"type": "tts"},
    }


async def test_drain_single_consumer(db, redis) -> None:
    ann = await _mk_ann(db, [_eid()], Priority.staff)
    await queue.push(redis, str(ann.id), Priority.staff)
    lock = redis.lock(queue.LOCK_KEY, timeout=30)
    assert await lock.acquire()
    ha, calls = mock_ha()
    assert await service.drain(db, redis, ha) == 0 and not calls
    await lock.release()
    assert await service.drain(db, redis, ha) == 1


async def test_priority_for_role(make_user) -> None:
    assert service.priority_for(await make_user(Role.admin)) == Priority.admin
    assert service.priority_for(await make_user(Role.staff)) == Priority.staff
    assert service.priority_for(await make_user(Role.student)) != Priority.alert


async def test_drain_reconciles_orphans(db, redis) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    old = datetime.now(UTC) - timedelta(minutes=5)
    stuck = await _mk_ann(db, [_eid()], Priority.staff)
    lost = await _mk_ann(db, [_eid()], Priority.admin, "perdue")
    queued_ok = await _mk_ann(db, [_eid()], Priority.staff, "enfile")
    await queue.push(redis, str(queued_ok.id), Priority.staff)
    async with db() as s:
        await s.execute(
            update(Announcement)
            .where(Announcement.id == stuck.id)
            .values(status="sending", started_at=old)
        )
        await s.execute(
            update(Announcement)
            .where(Announcement.id.in_([lost.id, queued_ok.id]))
            .values(created_at=old)
        )
        await s.commit()
    ha, calls = mock_ha()
    await service.reconcile(db, redis)
    await service.reconcile(db, redis)  # idempotent, sans doublon
    assert await redis.zcard(queue.QUEUE_KEY) == 2
    assert await redis.zscore(queue.QUEUE_KEY, str(lost.id)) < queue.SCALE * 2
    assert await service.drain(db, redis, ha) == 2
    async with db() as s:
        st = await s.get(Announcement, stuck.id)
        assert st.status == "failed" and st.results == {"_error": "interrompue"}
        assert (await s.get(Announcement, lost.id)).status == "done"
        assert (await s.get(Announcement, queued_ok.id)).status == "done"
    assert calls[0][2]["message"] == "perdue"


async def test_cancelled_announcement_never_stays_sending(db, redis) -> None:
    import asyncio

    ann = await _mk_ann(db, [_eid()], Priority.staff)
    await queue.push(redis, str(ann.id), Priority.staff)

    class Hang:
        async def speak_many(self, *a: Any, **k: Any) -> Any:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service.process_one(db, redis, Hang())  # type: ignore[arg-type]
    async with db() as s:
        a = await s.get(Announcement, ann.id)
    assert a.status == "failed" and a.results == {"_error": "interrompue"}


async def test_drain_renews_lock_and_releases(db, redis) -> None:
    for _ in range(2):
        ann = await _mk_ann(db, [_eid()], Priority.staff)
        await queue.push(redis, str(ann.id), Priority.staff)
    ha, _ = mock_ha()
    assert await service.drain(db, redis, ha) == 2
    assert await redis.exists(queue.LOCK_KEY) == 0
