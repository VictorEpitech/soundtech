# ruff: noqa: F811
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pywebpush import WebPushException
from sqlalchemy import delete, select

from app import push
from app.alerts import autoremind, responses, watch
from app.alerts.roster import Member
from app.alerts.teams import IncidentNotice, TeamsError
from app.config import Settings, get_settings
from app.main import app
from app.models import (
    AuditLog,
    Incident,
    IncidentNotification,
    PushSubscription,
    Role,
)
from tests.helpers import _settings
from tests.test_alerts import FakeTeams
from tests.test_ha import redis  # noqa: F401
from tests.test_responses import U1, U2, U3, FakeRoster

pytestmark = pytest.mark.usefixtures("migrated_db")


@pytest.fixture(autouse=True)
async def _clean(db):
    """Les ticks balayent tous les incidents `sent` : base vide au départ."""
    async with db() as s:
        await s.execute(delete(Incident))
        await s.execute(delete(PushSubscription))
        await s.commit()


EP = "https://fcm.googleapis.com/fcm/send/abc123"
KEYS = {"p256dh": "B" * 87, "auth": "a" * 22}


def cfg(**over: Any) -> Settings:
    base = {
        "auto_remind_interval": 600,
        "auto_remind_max": 3,
        "remind_cooldown": 0,
        "vapid_private_key": "k" * 43,
        "vapid_public_key": "P" * 87,
        "vapid_subject": "mailto:ops@example.org",
        "teams_timeout": 1.0,
        "push_timeout": 1.0,
        "rl_auth_limit": 0,
        **over,
    }
    return Settings(**{**dict(_settings()), **base})


class FollowTeams(FakeTeams):
    def __init__(self, fail: bool = False) -> None:
        super().__init__(fail)
        self.reminded: list[tuple[IncidentNotice, list[Member]]] = []
        self.direct: list[tuple[str, str]] = []

    async def remind(self, notice: IncidentNotice, targets: list[Member]) -> dict[str, Any]:
        if self.fail:
            raise TeamsError("teams http 500")
        self.reminded.append((notice, targets))
        return {"activity_id": "r"}

    async def notify_user(self, aad_id: str, text: str) -> dict[str, Any]:
        if self.fail:
            raise TeamsError("teams http 500")
        self.direct.append((aad_id, text))
        return {}


class FakePush:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status: dict[str, int] = {}  # endpoint -> code d'erreur HTTP
        self.crash = False

    def __call__(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self.crash:
            raise RuntimeError("boom")
        code = self.status.get(kw["subscription_info"]["endpoint"])
        if code:
            raise WebPushException("x", response=SimpleNamespace(status_code=code))
        return SimpleNamespace(status_code=201)


@pytest.fixture
def webpush(monkeypatch: pytest.MonkeyPatch) -> FakePush:
    f = FakePush()
    monkeypatch.setattr(push, "webpush", f)
    monkeypatch.setattr(push, "_resolves_public", lambda host: True)
    return f


async def mk_incident(db, *, minutes_ago: float = 0, status: str = "sent", **over: Any) -> Incident:
    async with db() as s:
        i = Incident(
            id=uuid.uuid4(),
            type="incendie",
            title="Alerte incendie",
            message="Évacuez",
            status=status,
            sent_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
            **over,
        )
        s.add(i)
        await s.commit()
        return i


async def respond(db, incident: Incident, aad: str, status: str = "safe_campus", text=None):
    async with db() as s:
        await responses.record(
            s, incident.id, aad_id=aad, name="Secret Nom", email=None, status=status, text=text
        )
        await s.commit()


async def reload(db, incident: Incident) -> Incident:
    async with db() as s:
        i = await s.get(Incident, incident.id, populate_existing=True)
        assert i is not None
        return i


async def audit_rows(db, action: str, incident: Incident | None = None) -> list[AuditLog]:
    async with db() as s:
        rows = (await s.scalars(select(AuditLog).where(AuditLog.action == action))).all()
    if incident is None:
        return list(rows)
    return [r for r in rows if (r.detail or {}).get("incident_id") == str(incident.id)]


async def audits(db, action: str, incident: Incident | None = None) -> int:
    return len(await audit_rows(db, action, incident))


async def add_sub(db, user, endpoint: str = EP) -> None:
    async with db() as s:
        s.add(PushSubscription(user_id=user.id, endpoint=endpoint, p256dh="p" * 20, auth="a" * 16))
        await s.commit()


def after(i: Incident, minutes: float) -> datetime:
    assert i.sent_at is not None
    return i.sent_at + timedelta(minutes=minutes, seconds=1)


# ---------- relances automatiques ----------


async def test_auto_remind_interval_and_cap(db, redis):
    inc = await mk_incident(db)
    roster, teams, s = FakeRoster(), FollowTeams(), cfg()
    await respond(db, inc, U1)
    run = lambda m: autoremind.tick(db, redis, s, roster, teams, now=after(inc, m))  # noqa: E731
    assert await run(9) == 0 and not teams.reminded  # intervalle pas atteint
    assert await run(10) == 1
    notice, targets = teams.reminded[0]
    assert {m.aad_id for m in targets} == {U2, U3}  # le répondant n'est pas relancé
    assert await run(10.5) == 0  # même échéance : pas de doublon
    assert await run(20) == 1 and await run(30) == 1
    assert await run(40) == 0 and await run(500) == 0  # plafond de 3
    got = await reload(db, inc)
    assert got.auto_remind_count == 3
    assert await audits(db, "auto_remind", inc) == 3


async def test_auto_remind_stops_on_close_and_ignores_non_sent(db, redis):
    roster, teams, s = FakeRoster(), FollowTeams(), cfg()
    closed = await mk_incident(db, status="closed")
    draft = await mk_incident(db, status="draft")
    failed = await mk_incident(db, status="failed")
    assert await autoremind.tick(db, redis, s, roster, teams, now=after(closed, 60)) == 0
    assert not teams.reminded
    del draft, failed
    inc = await mk_incident(db)
    assert await autoremind.tick(db, redis, s, roster, teams, now=after(inc, 11)) == 1
    async with db() as x:
        row = await x.get(Incident, inc.id)
        row.status = "closed"
        await x.commit()
    assert await autoremind.tick(db, redis, s, roster, teams, now=after(inc, 25)) == 0


async def test_auto_remind_disabled_and_override(db, redis):
    roster, teams = FakeRoster(), FollowTeams()
    inc = await mk_incident(db)
    off = cfg(auto_remind_interval=0)
    assert await autoremind.tick(db, redis, off, roster, teams, now=after(inc, 60)) == 0
    off_max = cfg(auto_remind_max=0)
    assert await autoremind.tick(db, redis, off_max, roster, teams, now=after(inc, 60)) == 0
    async with db() as x:
        (await x.get(Incident, inc.id)).status = "closed"
        await x.commit()
    ov = await mk_incident(db, auto_remind_interval=60, auto_remind_max=1)
    on = cfg()
    assert await autoremind.tick(db, redis, on, roster, teams, now=after(ov, 2)) == 1
    assert await autoremind.tick(db, redis, on, roster, teams, now=after(ov, 5)) == 0  # max 1
    zero = await mk_incident(db, auto_remind_interval=0)
    assert await autoremind.tick(db, redis, on, roster, teams, now=after(zero, 60)) == 0


async def test_auto_remind_test_incident_is_marked(db, redis):
    inc = await mk_incident(db, is_test=True)
    teams = FollowTeams()
    assert await autoremind.tick(db, redis, cfg(), FakeRoster(), teams, now=after(inc, 11)) == 1
    assert teams.reminded[0][0].is_test is True
    (a,) = await audit_rows(db, "auto_remind", inc)
    assert a.detail["is_test"] is True
    assert "title" not in a.detail and "text" not in a.detail


async def test_auto_remind_idempotent_concurrent(db, redis):
    inc = await mk_incident(db)
    roster, teams, s = FakeRoster(), FollowTeams(), cfg()
    now = after(inc, 11)
    res = await asyncio.gather(
        *(autoremind.tick(db, redis, s, roster, teams, now=now) for _ in range(4))
    )
    assert sum(res) == 1 and len(teams.reminded) == 1
    assert (await reload(db, inc)).auto_remind_count == 1


async def test_auto_remind_idempotent_without_lock(db, redis):
    """Même sans verrou Redis (expiré/perdu), le compteur en base empêche le doublon."""
    inc = await mk_incident(db)
    roster, teams, s = FakeRoster(), FollowTeams(), cfg()
    now = after(inc, 11)
    first = await autoremind.tick(db, redis, s, roster, teams, now=now)
    second = await autoremind.tick(db, redis, s, roster, teams, now=now)
    assert (first, second) == (1, 0)


async def clear_backoff(redis, inc) -> None:
    await redis.delete(f"autoremind:backoff:{inc.id}")


async def test_auto_remind_roster_down_backoff_cap_and_resume(db, redis):
    inc = await mk_incident(db)
    teams, s = FollowTeams(), cfg()
    bad = FakeRoster(fail=True)
    assert await autoremind.tick(db, redis, s, bad, teams, now=after(inc, 11)) == 0
    assert await autoremind.tick(db, redis, s, bad, teams, now=after(inc, 12)) == 0  # backoff
    assert (await reload(db, inc)).auto_remind_failures == 1
    for m in (13, 14):
        await clear_backoff(redis, inc)
        await autoremind.tick(db, redis, s, bad, teams, now=after(inc, m))
    got = await reload(db, inc)
    assert got.auto_remind_count == 0 and got.auto_remind_failures == autoremind.MAX_FAILURES
    assert await audits(db, "auto_remind_failed", inc) == 1
    assert await audits(db, "auto_remind_stopped", inc) == 1
    await clear_backoff(redis, inc)  # plafond atteint : plus aucune tentative, même roster rétabli
    assert await autoremind.tick(db, redis, s, FakeRoster(), teams, now=after(inc, 30)) == 0
    assert not teams.reminded


async def test_auto_remind_patch_resets_failures(ha_api_staff, db, redis):
    c, _ = ha_api_staff
    inc = await mk_incident(db, auto_remind_failures=autoremind.MAX_FAILURES)
    assert not autoremind.is_due(inc, cfg(), after(inc, 60))
    r = await c.patch(f"/incidents/{inc.id}/auto-remind", json={"interval": 60, "max": 2})
    assert r.status_code == 200
    got = await reload(db, inc)
    assert got.auto_remind_failures == 0 and autoremind.is_due(got, cfg(), after(inc, 60))


async def test_auto_remind_teams_down_reverts_then_retries(db, redis):
    inc = await mk_incident(db)
    roster, s = FakeRoster(), cfg(remind_cooldown=300)
    down = FollowTeams(fail=True)
    assert await autoremind.tick(db, redis, s, roster, down, now=after(inc, 11)) == 0
    got = await reload(db, inc)
    assert got.auto_remind_count == 0 and got.auto_remind_last_at is None
    assert await audits(db, "auto_remind_failed", inc) == 1
    up = FollowTeams()
    assert await autoremind.tick(db, redis, s, roster, up, now=after(inc, 12)) == 0  # backoff
    await clear_backoff(redis, inc)
    assert await autoremind.tick(db, redis, s, roster, up, now=after(inc, 13)) == 1
    got = await reload(db, inc)
    assert got.auto_remind_count == 1 and got.auto_remind_failures == 0


async def test_auto_remind_partial_failure_consumes_tick(db, redis):
    inc = await mk_incident(db)

    class Partial(FollowTeams):
        async def remind(self, notice, targets):
            self.reminded.append((notice, targets))
            raise TeamsError("teams http 500", mentioned_ids=[targets[0].aad_id])

    teams, s = Partial(), cfg()
    assert await autoremind.tick(db, redis, s, FakeRoster(), teams, now=after(inc, 11)) == 0
    got = await reload(db, inc)
    assert got.auto_remind_count == 1 and got.auto_remind_failures == 1  # tick consommé
    await clear_backoff(redis, inc)
    assert await autoremind.tick(db, redis, s, FakeRoster(), teams, now=after(inc, 12)) == 0
    assert len(teams.reminded) == 1  # pas de repost avant la prochaine échéance
    (a,) = await audit_rows(db, "auto_remind", inc)
    assert a.detail["partial"] is True and a.detail["mentioned"] == 1


async def test_teams_partial_failure_reports_mentioned():
    import httpx

    from app.alerts.teams import GraphTeamsNotifier

    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in str(req.url):
            return httpx.Response(200, json={"access_token": "t"})
        calls["n"] += 1
        return httpx.Response(200 if calls["n"] == 1 else 500, json={"id": "x"})

    s = cfg(
        teams_app_id="a",
        teams_app_secret="s",
        teams_tenant_id="t",
        teams_service_url="https://smba.example/",
        teams_conversation_id="c",
    )
    n = GraphTeamsNotifier(s, transport=httpx.MockTransport(handler))
    targets = [Member(f"{i:08d}-0000-0000-0000-000000000000", None, f"n{i}") for i in range(45)]
    with pytest.raises(TeamsError) as ei:
        await n.remind(IncidentNotice("i", "t", "m", False), targets)
    assert ei.value.mentioned_ids == [m.aad_id for m in targets[:20]]


async def test_auto_remind_respects_cooldown_and_roster_required(db, redis):
    inc = await mk_incident(db)
    teams, s = FollowTeams(), cfg(remind_cooldown=300)
    await redis.set(f"remind:cooldown:{inc.id}", "1", ex=60)  # relance manuelle récente
    assert await autoremind.tick(db, redis, s, FakeRoster(), teams, now=after(inc, 11)) == 0
    await redis.delete(f"remind:cooldown:{inc.id}")
    no_roster = FakeRoster()
    no_roster.configured = False
    assert await autoremind.tick(db, redis, s, no_roster, teams, now=after(inc, 11)) == 0
    assert await autoremind.tick(db, redis, s, FakeRoster([]), teams, now=after(inc, 11)) == 0
    assert not teams.reminded


async def test_auto_remind_nobody_missing(db, redis):
    inc = await mk_incident(db)
    for u in (U1, U2, U3):
        await respond(db, inc, u)
    teams = FollowTeams()
    assert await autoremind.tick(db, redis, cfg(), FakeRoster(), teams, now=after(inc, 11)) == 0
    assert (await reload(db, inc)).auto_remind_count == 0


async def test_auto_remind_config_api(ha_api_staff, db):
    c, _ = ha_api_staff
    r = await c.post(
        "/incidents",
        json={
            "type": "incendie",
            "title": "T",
            "message": "M",
            "auto_remind_interval": 120,
            "auto_remind_max": 2,
        },
    )
    assert r.status_code == 201
    ar = r.json()["auto_remind"]
    assert (ar["interval"], ar["max"], ar["count"], ar["overridden"]) == (120, 2, 0, True)
    iid = r.json()["id"]
    r = await c.patch(f"/incidents/{iid}/auto-remind", json={"interval": None, "max": None})
    assert r.status_code == 200 and r.json()["auto_remind"]["overridden"] is False
    assert (
        await c.patch(f"/incidents/{iid}/auto-remind", json={"interval": -1})
    ).status_code == 422
    rows = await audit_rows(db, "incident_auto_remind_config")
    assert [r for r in rows if r.detail["incident_id"] == iid]


# ---------- déclencheurs de notification ----------


async def test_no_response_trigger_once(db, redis, webpush, make_user):
    admin = await make_user(Role.admin)
    await add_sub(db, admin)
    inc = await mk_incident(db)
    teams, s, roster = FollowTeams(), cfg(notify_rate_threshold=0), FakeRoster()
    assert await watch.tick(db, s, roster, teams, now=after(inc, 4)) == 0 and not webpush.calls
    assert await watch.tick(db, s, roster, teams, now=after(inc, 5)) == 1
    assert await watch.tick(db, s, roster, teams, now=after(inc, 6)) == 0  # une seule fois
    assert len(webpush.calls) == 1
    p = json.loads(webpush.calls[0]["data"])
    assert p["url"] == f"/alerts/{inc.id}" and "récap" in p["body"]
    assert set(p) == {"title", "body", "url", "tag"}


async def test_no_response_not_when_someone_answered(db, webpush, make_user):
    await add_sub(db, await make_user(Role.staff))
    inc = await mk_incident(db)
    await respond(db, inc, U1)
    s = cfg(notify_rate_threshold=0)
    assert await watch.tick(db, s, FakeRoster(), FollowTeams(), now=after(inc, 30)) == 0
    assert not webpush.calls


async def test_danger_once_per_response_no_personal_data(db, webpush, make_user):
    await add_sub(db, await make_user(Role.admin))
    inc = await mk_incident(db)
    s = cfg(notify_no_response_min=0, notify_rate_threshold=0)
    roster, teams = FakeRoster(), FollowTeams()
    await respond(db, inc, U1, "danger", text="Je suis bloqué au 3e étage")
    await respond(db, inc, U2, "safe_campus")
    now = datetime.now(UTC)
    assert await watch.tick(db, s, roster, teams, now=now) == 1  # immédiat
    assert await watch.tick(db, s, roster, teams, now=now + timedelta(minutes=1)) == 0
    assert len(webpush.calls) == 1
    blob = webpush.calls[0]["data"]
    assert json.loads(blob)["body"] == 'Réponse "en danger" reçue — ouvrir le récap'
    for secret in ("Secret Nom", "bloqué", U1, "epitech"):
        assert secret not in blob
    await asyncio.sleep(0.01)
    await respond(db, inc, U3, "danger")  # nouvelle réponse danger -> nouvelle notification
    assert await watch.tick(db, s, roster, teams, now=now + timedelta(minutes=2)) == 1
    await respond(db, inc, U1, "danger")  # même personne qui répond de nouveau danger
    assert await watch.tick(db, s, roster, teams, now=now + timedelta(minutes=3)) == 1
    assert len(webpush.calls) == 3


async def test_notifications_concurrent_single_send(db, webpush, make_user):
    await add_sub(db, await make_user(Role.admin))
    inc = await mk_incident(db)
    await respond(db, inc, U1, "danger")
    s = cfg(notify_no_response_min=0, notify_rate_threshold=0)
    now = datetime.now(UTC)
    res = await asyncio.gather(
        *(watch.tick(db, s, FakeRoster(), FollowTeams(), now=now) for _ in range(4))
    )
    assert sum(res) == 1 and len(webpush.calls) == 1


async def test_rate_threshold_trigger(db, webpush, make_user):
    await add_sub(db, await make_user(Role.admin))
    inc = await mk_incident(db)
    await respond(db, inc, U1)  # 1/3 = 33 % < 50 %
    s = cfg(notify_no_response_min=0, notify_rate_threshold=50, notify_rate_after_min=15)
    roster, teams = FakeRoster(), FollowTeams()
    assert await watch.tick(db, s, roster, teams, now=after(inc, 14)) == 0
    assert await watch.tick(db, s, roster, teams, now=after(inc, 15)) == 1
    assert await watch.tick(db, s, roster, teams, now=after(inc, 16)) == 0
    ok = await mk_incident(db)
    for u in (U1, U2):
        await respond(db, ok, u)  # 2/3 = 66 %
    assert await watch.tick(db, s, roster, teams, now=after(ok, 30)) == 0


async def test_closed_incident_not_notified(db, webpush, make_user):
    await add_sub(db, await make_user(Role.admin))
    inc = await mk_incident(db, status="closed")
    s = cfg(notify_rate_threshold=0)
    assert await watch.tick(db, s, FakeRoster(), FollowTeams(), now=after(inc, 30)) == 0
    assert not webpush.calls


# ---------- push : nettoyage, repli, isolation ----------


async def test_gone_subscriptions_are_purged(db, webpush, make_user):
    admin = await make_user(Role.admin)
    await add_sub(db, admin, EP)
    await add_sub(db, admin, "https://push.example.net/gone")
    await add_sub(db, admin, "https://push.example.net/err")
    webpush.status["https://push.example.net/gone"] = 410
    webpush.status["https://push.example.net/err"] = 500
    async with db() as s:
        res = await push.send_to_staff(s, cfg(), {"title": "t"})
    assert (res.attempted, res.delivered, res.removed, res.failed) == (3, 1, 1, 1)
    async with db() as s:
        eps = set((await s.scalars(select(PushSubscription.endpoint))).all())
        ok = await s.scalar(select(PushSubscription).where(PushSubscription.endpoint == EP))
    assert "https://push.example.net/gone" not in eps and "https://push.example.net/err" in eps
    assert ok.last_ok_at is not None
    webpush.status[EP] = 404
    async with db() as s:
        await push.send_to_staff(s, cfg(), {"title": "t"})
        assert (
            await s.scalar(select(PushSubscription).where(PushSubscription.endpoint == EP)) is None
        )


async def test_push_ignores_demoted_and_inactive_users(db, webpush, make_user):
    await add_sub(db, await make_user(Role.student), "https://push.example.net/stud")
    await add_sub(db, await make_user(Role.admin, active=False), "https://push.example.net/off")
    async with db() as s:
        res = await push.send_to_staff(s, cfg(), {"title": "t"})
    assert res.attempted == 0 and not webpush.calls


async def test_push_disabled_without_keys(db, webpush, make_user):
    await add_sub(db, await make_user(Role.admin))
    off = cfg(vapid_private_key="", vapid_public_key="", vapid_subject="")
    assert not off.push_configured
    async with db() as s:
        res = await push.send_to_staff(s, off, {"title": "t"})
    assert res.attempted == 0 and not webpush.calls
    inc = await mk_incident(db)
    s2 = cfg(vapid_private_key="", vapid_public_key="", vapid_subject="", notify_rate_threshold=0)
    # rien à notifier (aucun canal) : traité sans erreur
    assert await watch.tick(db, s2, FakeRoster(), FollowTeams(), now=after(inc, 30)) == 1


async def test_teams_fallback_when_push_unavailable(db, webpush, make_user):
    inc = await mk_incident(db)
    off = cfg(
        vapid_private_key="",
        notify_teams_user_ids=f"{U1}, {U2}",
        notify_rate_threshold=0,
        public_base_url="https://sound.test",
    )
    teams = FollowTeams()
    assert await watch.tick(db, off, FakeRoster(), teams, now=after(inc, 6)) == 1
    assert [d[0] for d in teams.direct] == [U1, U2]
    text = teams.direct[0][1]
    assert f"/alerts/{inc.id}" in text and "Secret" not in text
    # push livré : pas de repli Teams
    await add_sub(db, await make_user(Role.admin))
    inc2 = await mk_incident(db)
    on = cfg(notify_teams_user_ids=f"{U1}, {U2}", notify_rate_threshold=0)
    teams2 = FollowTeams()
    assert await watch.tick(db, on, FakeRoster(), teams2, now=after(inc2, 6)) >= 1
    assert not [d for d in teams2.direct if str(inc2.id) in d[1]]


async def test_push_failure_never_raises_and_retries_bounded(db, webpush, make_user):
    await add_sub(db, await make_user(Role.admin))
    inc = await mk_incident(db)
    webpush.crash = True
    s = cfg(notify_rate_threshold=0)
    t0 = after(inc, 6)
    handled = 0
    for k in range(6):  # échecs répétés : jamais d'exception, 3 tentatives max
        handled += await watch.tick(
            db, s, FakeRoster(), FollowTeams(), now=t0 + timedelta(minutes=k)
        )
    assert handled == 0 and len(webpush.calls) == watch.MAX_ATTEMPTS
    async with db() as x:
        n = await x.scalar(select(IncidentNotification.attempts))
    assert n == watch.MAX_ATTEMPTS


async def test_push_failure_then_success_delivers_once(db, webpush, make_user):
    await add_sub(db, await make_user(Role.admin))
    inc = await mk_incident(db)
    s = cfg(notify_rate_threshold=0)
    t0 = after(inc, 6)
    webpush.crash = True
    assert await watch.tick(db, s, FakeRoster(), FollowTeams(), now=t0) == 0
    webpush.crash = False
    assert await watch.tick(db, s, FakeRoster(), FollowTeams(), now=t0 + timedelta(minutes=1)) == 1
    assert await watch.tick(db, s, FakeRoster(), FollowTeams(), now=t0 + timedelta(minutes=2)) == 0


# ---------- endpoints /push ----------


@pytest.fixture
async def push_api(client, redis, monkeypatch):
    from app.ha.router import get_redis

    app.dependency_overrides[get_settings] = lambda: cfg()
    app.dependency_overrides[get_redis] = lambda: redis
    had = hasattr(app.state, "redis")
    app.state.redis = redis
    yield client
    if not had:  # sans app.state.redis, les rate limits des autres tests restent en fail-open
        del app.state.redis
    for k in await redis.keys("rl:*"):
        await redis.delete(k)


@pytest.fixture
async def ha_api_staff(push_api, make_user, login_as):
    u = await make_user(Role.staff)
    await login_as(u)
    return push_api, u


SUB = {"endpoint": EP, "keys": KEYS}


async def test_push_endpoints_require_auth(push_api):
    c = push_api
    assert (await c.get("/push/vapid-public-key")).status_code == 401
    assert (await c.post("/push/subscribe", json=SUB)).status_code == 401
    assert (await c.request("DELETE", "/push/subscribe", json={"endpoint": EP})).status_code == 401


async def test_push_subscribe_roundtrip(ha_api_staff, db):
    c, u = ha_api_staff
    r = await c.get("/push/vapid-public-key")
    assert r.json() == {"enabled": True, "public_key": "P" * 87}
    assert (
        await c.post("/push/subscribe", json=SUB, headers={"user-agent": "UA/1"})
    ).status_code == 201
    assert (
        await c.post("/push/subscribe", json=SUB)
    ).status_code == 201  # idempotent (endpoint unique)
    async with db() as s:
        rows = (
            await s.scalars(select(PushSubscription).where(PushSubscription.user_id == u.id))
        ).all()
        assert len(rows) == 1 and rows[0].p256dh == KEYS["p256dh"]
    r = await c.request("DELETE", "/push/subscribe", json={"endpoint": EP})
    assert r.json() == {"deleted": 1}
    assert (await c.request("DELETE", "/push/subscribe", json={"endpoint": EP})).json() == {
        "deleted": 0
    }
    await_rows = {a: await audit_rows(db, a) for a in ("push_subscribe", "push_unsubscribe")}
    mine = lambda a: [r for r in await_rows[a] if r.user_id == u.id]  # noqa: E731
    assert len(mine("push_subscribe")) == 2 and len(mine("push_unsubscribe")) == 1


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://localhost/x",
        "https://intranet/x",
        "https://user:pw@fcm.googleapis.com/x",
        "https://fcm.googleapis.com:8443/x",
        "javascript:alert(1)",
        "https://fcm.googleapis.com/" + "a" * 2100,
        "https://127.1/x",
        "https://0x7f.1/x",
        "https://localhost./x",
        "https://x.localhost/x",
        "https://evil.example.com/x",
        "https://fcm.googleapis.com.evil.com/x",
        "https://fcm.googleapis.com./x",
    ],
    ids=[
        "http",
        "ipv4",
        "ipv6",
        "localhost",
        "no-dot",
        "userinfo",
        "port",
        "js",
        "too-long",
        "127.1",
        "hex",
        "localhost-dot",
        "x.localhost",
        "not-allowed",
        "suffix-trick",
        "trailing-dot",
    ],
)
async def test_push_subscribe_rejects_bad_endpoints(ha_api_staff, endpoint):
    c, _ = ha_api_staff
    r = await c.post("/push/subscribe", json={"endpoint": endpoint, "keys": KEYS})
    assert r.status_code == 422


async def test_push_subscribe_rejects_bad_keys_and_disabled(ha_api_staff):
    c, _ = ha_api_staff
    bad = {"endpoint": EP, "keys": {"p256dh": "short", "auth": "a" * 22}}
    assert (await c.post("/push/subscribe", json=bad)).status_code == 422
    app.dependency_overrides[get_settings] = lambda: cfg(vapid_private_key="")
    assert (await c.get("/push/vapid-public-key")).json() == {"enabled": False, "public_key": None}
    assert (await c.post("/push/subscribe", json=SUB)).status_code == 503


async def test_push_cannot_delete_other_users_subscription(ha_api_staff, db, make_user):
    c, _ = ha_api_staff
    other = await make_user(Role.admin)
    await add_sub(db, other, "https://push.example.net/other")
    r = await c.request(
        "DELETE", "/push/subscribe", json={"endpoint": "https://push.example.net/other"}
    )
    assert r.json() == {"deleted": 0}


async def test_push_rate_limit(ha_api_staff):
    c, _ = ha_api_staff
    app.dependency_overrides[get_settings] = lambda: cfg(rl_push_limit=2, rl_window=60)
    codes = [(await c.get("/push/vapid-public-key")).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and codes[-1] == 429


def test_valid_endpoint_unit():
    ok = (
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://fcm.googleapis.com/fcm/send/x",
        "https://wns2-par02p.notify.windows.com/w/?token=x",
        "https://web.push.apple.com/Qx",
        "https://api.push.apple.com/3/device/x",
    )
    assert all(push.valid_endpoint(u) for u in ok)
    assert not push.valid_endpoint("https://10.0.0.1/x")
    assert not push.valid_endpoint("https://push.corp.example/x")
    extra = push.allowed_hosts(cfg(push_allowed_hosts="corp.example, .other.org"))
    assert push.valid_endpoint("https://push.corp.example/x", extra)
    assert push.valid_endpoint("https://a.other.org/x", extra)


def test_resolves_public_rejects_private(monkeypatch):
    import socket

    def fake(addr):
        return lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 443))]

    for bad in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1", "0.0.0.0"):
        monkeypatch.setattr(socket, "getaddrinfo", fake(bad))
        assert not push._resolves_public("x.push.apple.com")
    monkeypatch.setattr(socket, "getaddrinfo", fake("8.8.8.8"))
    assert push._resolves_public("x.push.apple.com")


async def test_send_refuses_private_resolution(db, monkeypatch, make_user):
    import socket

    f = FakePush()
    monkeypatch.setattr(push, "webpush", f)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("10.0.0.5", 443))]
    )
    await add_sub(db, await make_user(Role.admin), "https://fcm.googleapis.com/fcm/send/z")
    async with db() as s:
        res = await push.send_to_staff(s, cfg(), {"title": "t"})
    assert (res.attempted, res.delivered, res.failed) == (1, 0, 1) and not f.calls


def test_push_session_never_follows_redirects(monkeypatch):
    import requests

    seen: dict[str, Any] = {}

    def fake(self, method, url, **kw):
        seen.update(kw)
        return "resp"

    monkeypatch.setattr(requests.Session, "request", fake)
    push._NoRedirectSession().post("https://fcm.googleapis.com/x", allow_redirects=True)
    assert seen["allow_redirects"] is False


async def test_redirect_response_counts_as_failure_not_removal(db, webpush, make_user):
    await add_sub(db, await make_user(Role.admin), EP)
    webpush.status[EP] = 302
    async with db() as s:
        res = await push.send_to_staff(s, cfg(), {"title": "t"})
    assert (res.delivered, res.removed, res.failed) == (0, 0, 1)


async def test_subscribe_cannot_hijack_other_users_endpoint(ha_api_staff, db, make_user):
    c, _ = ha_api_staff
    other = await make_user(Role.admin)
    await add_sub(db, other, EP)
    r = await c.post("/push/subscribe", json=SUB)
    assert r.status_code == 409
    async with db() as s:
        row = await s.scalar(select(PushSubscription).where(PushSubscription.endpoint == EP))
    assert row.user_id == other.id and row.p256dh != KEYS["p256dh"]


def test_sw_has_push_handlers():
    from app.ui import pwa

    pwa.sw_source.cache_clear()
    src = pwa.sw_source()
    assert 'addEventListener("push"' in src and 'addEventListener("notificationclick"' in src
    assert "showNotification" in src and "/static/icons/icon-192.png" in src
    assert "caches.put" not in src and "cache.put" not in src
