# ruff: noqa: F811
import asyncio
import time
import uuid
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings, get_settings
from app.ha import service, volume
from app.ha.client import HAConnectionError, HAError
from app.models import Announcement, AuditLog, Priority, Role, Speaker
from tests.helpers import _settings
from tests.test_ha import _eid, _speaker, ha_api, redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_db")

ORIG = 0.3


def _cfg(**over: Any) -> Settings:
    base = {
        "announce_wpm": 60000,
        "volume_restore_margin": 0.05,
        "volume_restore_max_wait": 1,
        "volume_settle": 0,
        "volume_restore_retries": 2,
        "alert_force_volume": True,
        "announce_force_volume": True,
    }
    return _settings().model_copy(update={**base, **over})


class FakeHA:
    """HA simulé : volumes par enceinte + journal d'appels ordonné."""

    def __init__(self, vol: float | None = ORIG) -> None:
        self.vol = vol
        self.log: list[tuple[str, Any]] = []
        self.fail_get = False
        self.fail_tts = False
        self.fail_restore = False
        self.hold_tts: asyncio.Event | None = None

    async def get_volume(self, entity: str) -> float | None:
        self.log.append(("get", entity))
        if self.fail_get:
            raise HAConnectionError("down")
        return self.vol

    async def set_volume(self, entity: str, level: float) -> None:
        self.log.append(("set", level))
        if self.fail_restore and level == ORIG:
            raise HAConnectionError("down")
        self.vol = level

    async def speak_many(self, entities, text, notify=None, ssml=None):  # type: ignore[no-untyped-def]
        self.log.append(("speak", text))
        if self.hold_tts is not None:
            await self.hold_tts.wait()
        if self.fail_tts:
            return {e: HAError("tts down") for e in entities}
        return {e: None for e in entities}

    def kinds(self) -> list[str]:
        return [k for k, _ in self.log]

    def sets(self) -> list[float]:
        return [v for k, v in self.log if k == "set"]


@pytest_asyncio.fixture(autouse=True)
async def _clean(redis: Redis):  # noqa: F811
    async def wipe() -> None:
        keys = [k async for k in redis.scan_iter("sound:vol:*")]
        if keys:
            await redis.delete(*keys)

    await wipe()
    yield
    await volume.wait_pending()
    await wipe()


async def _send(db, redis, settings: Settings, entity: str, text="salut", prio=Priority.admin,
                force: bool | None = None) -> uuid.UUID:  # fmt: skip
    sp = Speaker(id=uuid.uuid4(), entity_id=entity, name=entity, notify_service=None,
                 ma_player_id=None)  # fmt: skip
    ann = service.build_announcement(
        settings, text=text, speakers=[sp], is_test=False, priority=prio, author=None,
        force_volume=force,
    )  # fmt: skip
    async with db() as s:
        await service.submit(s, redis, ann)
    return ann.id


async def _ann(db, ann_id: uuid.UUID) -> Announcement:
    async with db() as s:
        a = await s.get(Announcement, ann_id)
        assert a is not None
        return a


async def _audits(db, action: str) -> int:
    async with db() as s:
        return len((await s.scalars(select(AuditLog).where(AuditLog.action == action))).all())


# --- séquence nominale -------------------------------------------------------


async def test_get_set_speak_restore(db, redis) -> None:
    cfg, ha, e = _cfg(), FakeHA(), _eid()
    ann_id = await _send(db, redis, cfg, e)
    assert await service.process_one(db, redis, ha, None, cfg) is True  # type: ignore[arg-type]
    assert ha.kinds() == ["get", "set", "speak"] and ha.sets() == [1.0]
    await volume.wait_pending()
    assert ha.kinds() == ["get", "set", "speak", "set"] and ha.sets() == [1.0, ORIG]
    assert ha.vol == ORIG
    assert await redis.exists(f"sound:vol:{e}:orig") == 0
    assert (await _ann(db, ann_id)).status == "done"


async def test_alert_uses_alert_level_and_config_level(db, redis) -> None:
    cfg, ha = (
        _cfg(announce_volume=40, alert_volume=90),
        FakeHA(),
    )
    ha, e = FakeHA(), _eid()
    await _send(db, redis, cfg, e)
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    await volume.wait_pending()
    assert ha.sets() == [0.4, ORIG]
    ha.log.clear()
    await _send(db, redis, cfg, e, prio=Priority.alert)
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    await volume.wait_pending()
    assert ha.sets() == [0.9, ORIG]


# --- échecs ------------------------------------------------------------------


async def test_tts_failure_still_restores(db, redis) -> None:
    cfg, ha, e = _cfg(), FakeHA(), _eid()
    ha.fail_tts = True
    ann_id = await _send(db, redis, cfg, e)
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    await volume.wait_pending()
    assert (await _ann(db, ann_id)).status == "failed"
    assert ha.vol == ORIG and ha.sets() == [1.0, ORIG]


async def test_exception_in_speak_still_restores(db, redis) -> None:
    cfg, ha, e = _cfg(), FakeHA(), _eid()

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("crash")

    ha.speak_many = boom  # type: ignore[method-assign]
    ann_id = await _send(db, redis, cfg, e)
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    await volume.wait_pending()
    assert (await _ann(db, ann_id)).status == "failed"
    assert ha.vol == ORIG


async def test_read_failure_still_speaks_no_invented_restore(db, redis, caplog) -> None:
    cfg, ha, e = _cfg(), FakeHA(), _eid()
    ha.fail_get = True
    unk0 = await _audits(db, "volume_restore_unknown")
    ann_id = await _send(db, redis, cfg, e, prio=Priority.alert)
    with caplog.at_level("WARNING"):
        await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
        await volume.wait_pending()
    assert (await _ann(db, ann_id)).status == "done"
    assert "speak" in ha.kinds() and ha.sets() == [1.0]  # forcé best-effort, rien restauré
    assert any("ERROR" == r.levelname and "inconnu" in r.message for r in caplog.records)
    assert await redis.exists(f"sound:vol:{e}:orig") == 0
    assert await _audits(db, "volume_restore_unknown") == unk0 + 1
    assert (await _ann(db, ann_id)).results["_volume"] == {e: "restore_unknown"}  # type: ignore[index]


async def test_unknown_original_announcement_not_forced(db, redis) -> None:
    cfg, ha, e = _cfg(), FakeHA(), _eid()
    ha.fail_get = True
    ann_id = await _send(db, redis, cfg, e)
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    await volume.wait_pending()
    assert ha.sets() == [] and (await _ann(db, ann_id)).status == "done"


async def test_alert_boost_budget_does_not_delay_speech(db, redis) -> None:
    cfg, ha, e = _cfg(), FakeHA(), _eid()

    async def slow_get(entity: str) -> float:
        await asyncio.sleep(30)
        return ORIG

    ha.get_volume = slow_get  # type: ignore[method-assign]
    await _send(db, redis, cfg, e, prio=Priority.alert)
    t0 = time.monotonic()
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    assert time.monotonic() - t0 < service.ALERT_BOOST_BUDGET + 0.5
    assert "speak" in ha.kinds()
    await volume.wait_pending()  # le boost d'arrière-plan se termine, bornée par le step


async def test_alert_does_not_wait_for_lock(db, redis) -> None:
    cfg, ha, e = _cfg(), FakeHA(), _eid()
    ann_id = await _send(db, redis, cfg, e, prio=Priority.alert)
    async with redis.lock(f"sound:vol:{e}:lock", timeout=30):
        t0 = time.monotonic()
        await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
        assert time.monotonic() - t0 < 1.0
    await volume.wait_pending()
    assert ha.kinds() == ["speak"] and (await _ann(db, ann_id)).status == "done"


async def test_cancel_pending_keeps_lease_reaper_restores_at_deadline(
    db, redis, monkeypatch
) -> None:
    cfg, ha, e = _cfg(announce_wpm=60, volume_restore_max_wait=30), FakeHA(), _eid()
    await _send(db, redis, cfg, e, text="un deux trois quatre")
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    await volume.cancel_pending()  # arrêt du worker pendant la lecture
    assert ha.vol == 1.0 and ha.sets() == [1.0]  # pas de restauration immédiate
    assert await redis.hlen(f"sound:vol:{e}:leases") == 1
    assert await volume.reap(redis, ha, cfg) == []  # bail pas encore échu  # type: ignore[arg-type]
    assert ha.vol == 1.0
    real = time.time
    monkeypatch.setattr(volume.time, "time", lambda: real() + 1000)
    out = await volume.reap(redis, ha, cfg)  # type: ignore[arg-type]
    assert [o.status for o in out] == [volume.RESTORED] and ha.vol == ORIG


async def test_missing_volume_attribute_is_unknown(db, redis) -> None:
    cfg, ha, e = _cfg(), FakeHA(vol=None), _eid()
    await _send(db, redis, cfg, e)
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    await volume.wait_pending()
    assert ha.kinds() == ["get", "speak"]  # annonce : volume inconnu => pas de forçage
    assert await redis.exists(f"sound:vol:{e}:leases") == 0


async def test_set_failure_does_not_block_speech(db, redis) -> None:
    cfg, ha, e = _cfg(), FakeHA(), _eid()

    async def bad_set(entity: str, level: float) -> None:
        raise HAConnectionError("down")

    ha.set_volume = bad_set  # type: ignore[method-assign]
    ann_id = await _send(db, redis, cfg, e, prio=Priority.alert)
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    await volume.wait_pending()
    assert "speak" in ha.kinds() and (await _ann(db, ann_id)).status == "done"


async def test_restore_failure_is_audited_marked_then_reaped(db, redis, caplog) -> None:
    cfg, ha, e = _cfg(), FakeHA(), _eid()
    ha.fail_restore = True
    audits0 = await _audits(db, "volume_restore_failed")
    ann_id = await _send(db, redis, cfg, e)
    with caplog.at_level("ERROR"):
        await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
        await volume.wait_pending()
    assert ha.vol == 1.0  # restée forcée...
    assert any("RESTAURATION ECHOUEE" in r.message for r in caplog.records)  # ... mais signalée
    assert await _audits(db, "volume_restore_failed") == audits0 + 1
    assert (await _ann(db, ann_id)).results["_volume"] == {e: "restore_failed"}  # type: ignore[index]
    assert await redis.get(f"sound:vol:{e}:orig") == b"0.3"  # état conservé pour retry
    ha.fail_restore = False  # HA revient : le reaper répare
    outcomes = await volume.reap(redis, ha, cfg)  # type: ignore[arg-type]
    assert [o.status for o in outcomes] == [volume.RESTORED] and ha.vol == ORIG
    assert await redis.exists(f"sound:vol:{e}:orig") == 0
    assert await _audits(db, "volume_restore_failed") == audits0 + 1


async def test_reaper_releases_abandoned_lease(db, redis, monkeypatch) -> None:
    monkeypatch.setattr(volume, "LEASE_SLACK", -1.0)  # bail déjà expiré : worker « crashé »
    cfg, ha, e = _cfg(), FakeHA(), _eid()
    lease = volume.new_lease(e, str(uuid.uuid4()), 0)
    await volume.acquire(redis, ha, cfg, lease, 100)  # type: ignore[arg-type]
    assert not lease.skipped and ha.vol == 1.0
    outcomes = await volume.reap(redis, ha, cfg)  # type: ignore[arg-type]
    assert outcomes[0].status == volume.RESTORED and ha.vol == ORIG


# --- concurrence -------------------------------------------------------------


async def test_concurrent_announcements_single_set_single_restore(db, redis) -> None:
    cfg, ha, e = _cfg(volume_restore_max_wait=1, announce_wpm=60), FakeHA(), _eid()
    await _send(db, redis, cfg, e, text="un deux")  # 2 mots / 60 wpm = 2 s, plafonné à 1 s
    await _send(db, redis, cfg, e, text="trois quatre")
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    assert ha.kinds().count("get") == 1  # original lu une seule fois
    assert ha.sets() == [1.0, 1.0]  # jamais restauré entre les deux
    assert await redis.get(f"sound:vol:{e}:orig") == b"0.3"  # jamais le volume boosté
    assert await redis.hlen(f"sound:vol:{e}:leases") == 2
    await volume.wait_pending()
    assert ha.sets() == [1.0, 1.0, ORIG] and ha.vol == ORIG  # restauration unique


async def test_alert_during_announcement_restores_real_original(db, redis) -> None:
    cfg = _cfg(announce_volume=70, announce_wpm=60, volume_restore_max_wait=1)
    ha, e = FakeHA(), _eid()
    await _send(db, redis, cfg, e, text="un deux")
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    assert ha.vol == 0.7  # annonce en cours de lecture
    await _send(db, redis, cfg, e, text="alerte", prio=Priority.alert)
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    assert ha.vol == 1.0 and ha.kinds().count("get") == 1
    await volume.wait_pending()
    assert ha.sets() == [0.7, 1.0, ORIG] and ha.vol == ORIG


# --- désactivation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "prio", "force"),
    [
        ({"announce_force_volume": False}, Priority.admin, None),
        ({"alert_force_volume": False}, Priority.alert, None),
        ({"announce_volume": 0}, Priority.staff, None),
        ({}, Priority.staff, False),  # case « Forcer le volume » décochée
    ],
)
async def test_disabled_means_no_ha_volume_calls(db, redis, over, prio, force) -> None:
    cfg, ha, e = _cfg(**over), FakeHA(), _eid()
    ann_id = await _send(db, redis, cfg, e, prio=prio, force=force)
    await service.process_one(db, redis, ha, None, cfg)  # type: ignore[arg-type]
    await volume.wait_pending()
    assert ha.kinds() == ["speak"]
    assert "force_volume" not in (await _ann(db, ann_id)).targets[0]


def test_volume_level_rules() -> None:
    cfg = _cfg(announce_volume=55)
    assert service.volume_level(cfg, Priority.staff) == 55
    assert service.volume_level(cfg, Priority.alert) == 100
    assert service.volume_level(cfg, Priority.staff, False) == 0
    assert service.volume_level(cfg, Priority.staff, True) == 55
    assert service.volume_level(_cfg(announce_force_volume=False), Priority.staff, True) == 0
    with pytest.raises(ValueError):
        Settings(announce_volume=101)
    with pytest.raises(ValueError):
        Settings(alert_volume=-1)


def test_hold_seconds_bounded() -> None:
    cfg = _settings().model_copy(
        update={"announce_wpm": 150, "volume_restore_margin": 3.0, "volume_restore_max_wait": 10}
    )
    assert volume.hold_seconds(cfg, "") == 3.0
    assert volume.hold_seconds(cfg, "mot " * 1000) == 10


# --- API / UI ----------------------------------------------------------------


async def test_api_force_volume_checkbox_and_defaults(ha_api, make_user, login_as, db) -> None:
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: _cfg(
        ha_url="http://ha.test",
        ha_token=SecretStr("tok"),
        announce_volume=80,  # type: ignore[arg-type]
    )
    await login_as(await make_user(Role.admin))
    sid = await _speaker(ha_api, _eid())
    r = await ha_api.post("/announcements", json={"text": "a", "speaker_ids": [sid]})
    assert r.status_code == 202 and r.json()["targets"][0]["force_volume"] == 80
    r = await ha_api.post(
        "/announcements", json={"text": "b", "speaker_ids": [sid], "force_volume": False}
    )
    assert r.status_code == 202 and "force_volume" not in r.json()["targets"][0]
    r = await ha_api.post(
        "/announcements/preview", json={"text": "b", "speaker_ids": [sid], "is_test": True}
    )
    assert r.json()["volume"] == 80  # test : volume forcé aussi
    page = await ha_api.get("/announce")
    assert 'name="force_volume"' in page.text and "80 %" in page.text
