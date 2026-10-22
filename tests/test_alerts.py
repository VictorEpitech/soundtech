# ruff: noqa: F811
import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.alerts import router as alerts_router
from app.alerts import teams
from app.alerts.teams import GraphTeamsNotifier, IncidentNotice, LogTeamsNotifier, TeamsError
from app.config import get_settings
from app.ha import queue, service
from app.main import app
from app.models import Announcement, Priority, Role
from tests.helpers import audit_actions
from tests.test_ha import _ha_settings, _speaker, ha_api, mock_ha, redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_db")

BODY = {"type": "incendie", "title": "Alerte incendie", "message": "Évacuez le bâtiment"}
GO = {"confirm": "ALERTE"}


class FakeTeams:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.notices: list[IncidentNotice] = []

    async def notify(self, notice: IncidentNotice) -> dict[str, Any]:
        self.notices.append(notice)
        if self.fail:
            raise TeamsError("teams http 500")
        return {"activity_id": "a1"}


@pytest.fixture
def fake_teams(ha_api) -> FakeTeams:
    t = FakeTeams()
    app.dependency_overrides[alerts_router.get_teams_notifier] = lambda: t
    return t


async def _draft(client, **over: Any) -> dict[str, Any]:
    r = await client.post("/incidents", json={**BODY, **over})
    assert r.status_code == 201, r.text
    return r.json()


async def _login(make_user, login_as, role: Role = Role.admin):
    u = await make_user(role)
    await login_as(u)
    return u


async def _ann(db, ann_id: str) -> Announcement:
    async with db() as s:
        ann = await s.get(Announcement, uuid.UUID(ann_id))
    assert ann is not None
    return ann


async def test_lifecycle_and_audit(ha_api, fake_teams, make_user, login_as, db) -> None:
    u = await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.al_one", notify_service="notify.al_one")
    inc = await _draft(ha_api)
    assert inc["status"] == "draft" and inc["is_test"] is False
    assert (await ha_api.post(f"/incidents/{inc['id']}/close")).status_code == 409

    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "sent" and out["sent_at"]
    assert out["delivery"]["tts"]["ok"] and out["delivery"]["teams"]["ok"]
    assert fake_teams.notices[0].message == BODY["message"]
    assert (await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)).status_code == 409

    r = await ha_api.post(f"/incidents/{inc['id']}/close")
    assert r.status_code == 200 and r.json()["status"] == "closed"
    assert (await ha_api.post(f"/incidents/{inc['id']}/close")).status_code == 409
    assert (await ha_api.get(f"/incidents/{inc['id']}")).json()["closed_by_email"] == u.email
    assert inc["id"] in [i["id"] for i in (await ha_api.get("/incidents")).json()]
    acts = await audit_actions(db, u.email)
    for a in ("incident_create", "incident_send", "incident_delivery", "incident_close"):
        assert a in acts
    assert acts.index("incident_send") < acts.index("incident_close")


async def test_double_confirmation(ha_api, fake_teams, make_user, login_as, redis) -> None:
    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.al_conf")
    inc = await _draft(ha_api)
    for bad in ("alerte", "ALERTE ", "TEST", "", "oui"):
        r = await ha_api.post(f"/incidents/{inc['id']}/send", json={"confirm": bad})
        assert r.status_code == 422, bad
    assert (await ha_api.post(f"/incidents/{inc['id']}/send", json={})).status_code == 422
    assert (await ha_api.get(f"/incidents/{inc['id']}")).json()["status"] == "draft"
    assert fake_teams.notices == [] and await redis.zcard(queue.QUEUE_KEY) == 0


async def test_test_mode_forced_prefix(ha_api, fake_teams, make_user, login_as, db) -> None:
    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.al_test", notify_service="notify.al_test")
    inc = await _draft(ha_api, is_test=True)
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    assert r.status_code == 422
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json={"confirm": "TEST"})
    assert r.status_code == 200
    ann = await _ann(db, r.json()["announcement_id"])
    assert ann.is_test and ann.text.startswith(_ha_settings().test_prefix)
    assert fake_teams.notices[0].is_test
    real = await _draft(ha_api)
    r = await ha_api.post(f"/incidents/{real['id']}/send", json={"confirm": "TEST"})
    assert r.status_code == 422


async def test_alert_priority_preempts_queue(
    ha_api, fake_teams, make_user, login_as, db, redis
) -> None:
    await _login(make_user, login_as)
    sid = await _speaker(ha_api, "media_player.al_prio", notify_service="notify.al_prio")
    r = await ha_api.post("/announcements", json={"text": "staff", "speaker_ids": [sid]})
    assert r.status_code == 202
    inc = await _draft(ha_api)
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    ann_id = r.json()["announcement_id"]
    assert await queue.pop(redis) == ann_id
    ann = await _ann(db, ann_id)
    assert ann.priority == Priority.alert == 0


async def test_alert_targets_only_active_speakers(
    ha_api, fake_teams, make_user, login_as, db
) -> None:
    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.al_on")
    await _speaker(ha_api, "media_player.al_off", is_active=False)
    inc = await _draft(ha_api)
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    ann = await _ann(db, r.json()["announcement_id"])
    ents = {t["entity_id"] for t in ann.targets}
    assert "media_player.al_on" in ents and "media_player.al_off" not in ents


async def test_teams_failure_does_not_block_tts(
    ha_api, fake_teams, make_user, login_as, redis
) -> None:
    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.al_tf")
    fake_teams.fail = True
    inc = await _draft(ha_api)
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    assert r.status_code == 200
    d = r.json()["delivery"]
    assert d["tts"]["ok"] is True and d["teams"]["ok"] is False
    assert "500" in d["teams"]["error"]
    assert await redis.zcard(queue.QUEUE_KEY) >= 1


async def test_tts_failure_does_not_block_teams(ha_api, fake_teams, make_user, login_as) -> None:
    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.al_ttsf")
    app.dependency_overrides[get_settings] = lambda: _ha_settings().model_copy(
        update={"ha_url": ""}
    )
    inc = await _draft(ha_api)
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    assert r.status_code == 200
    d = r.json()["delivery"]
    assert d["tts"]["ok"] is False and d["teams"]["ok"] is True
    assert r.json()["status"] == "sent" and len(fake_teams.notices) == 1


async def test_queue_error_is_isolated(
    ha_api, fake_teams, make_user, login_as, monkeypatch
) -> None:
    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.al_qerr")

    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("redis down")

    monkeypatch.setattr(queue, "push", boom)
    inc = await _draft(ha_api)
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    assert r.status_code == 200
    d = r.json()["delivery"]
    assert d["tts"]["ok"] is False and d["teams"]["ok"] is True


async def test_role_access(ha_api, fake_teams, make_user, login_as, client) -> None:
    assert (await client.get("/incidents")).status_code == 401
    assert (await client.post("/incidents", json=BODY)).status_code == 401
    await _login(make_user, login_as, Role.admin)
    inc = await _draft(ha_api)
    assert (await ha_api.get(f"/incidents/{inc['id']}")).status_code == 200
    ha_api.cookies.clear()
    await _login(make_user, login_as, Role.staff)
    assert (await ha_api.get(f"/incidents/{inc['id']}")).status_code == 200
    assert (
        await ha_api.post(f"/incidents/{inc['id']}/send", json={"confirm": "x"})
    ).status_code == 422
    ha_api.cookies.clear()
    student = await make_user(Role.student)
    r = await ha_api.get("/incidents", headers={"authorization": "Bearer nope"})
    assert r.status_code == 401 and student.role == "student"


async def test_validation(ha_api, fake_teams, make_user, login_as) -> None:
    await _login(make_user, login_as)
    for bad in ({"title": "  "}, {"message": ""}, {"type": ""}, {"message": "x" * 501}):
        r = await ha_api.post("/incidents", json={**BODY, **bad})
        assert r.status_code == 422, bad
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await ha_api.get(f"/incidents/{missing}")).status_code == 404
    assert (await ha_api.post(f"/incidents/{missing}/close")).status_code == 404


async def test_alerts_ui(ha_api, fake_teams, make_user, login_as) -> None:
    await _login(make_user, login_as)
    inc = await _draft(ha_api, title="<b>x</b>", is_test=True)
    page = await ha_api.get("/alerts")
    assert page.status_code == 200 and "test-banner" in page.text and "alert-grid" in page.text
    assert 'id="incident-form"' not in page.text and 'name="title"' not in page.text
    frag = (await ha_api.get("/ui/alerts/list")).text
    assert f"/alerts/{inc['id']}" in frag and "&lt;b&gt;x" in frag and "TEST" in frag


async def test_alerts_ui_anonymous(client) -> None:
    for url in ("/alerts", "/ui/alerts/list"):
        r = await client.get(url)
        assert r.status_code == 302 and r.headers["location"] == "/login"


# --- Teams -------------------------------------------------------------------

NOTICE = IncidentNotice("id-1", "Titre", "Message", False)


def test_card_has_three_answers_and_optional_text() -> None:
    card = teams.build_card(NOTICE)
    choice = next(b for b in card["body"] if b["type"] == "Input.ChoiceSet")
    assert [c["value"] for c in choice["choices"]] == ["safe_campus", "safe_offsite", "danger"]
    text = next(b for b in card["body"] if b["type"] == "Input.Text")
    assert not text.get("isRequired")
    assert card["actions"][0]["data"]["incident_id"] == "id-1"
    assert teams.build_card(IncidentNotice("i", "T", "M", True))["body"][0]["text"].startswith("[")


async def test_log_notifier_default() -> None:
    assert "skipped" in await LogTeamsNotifier().notify(NOTICE)
    assert isinstance(teams.get_notifier(_ha_settings()), LogTeamsNotifier)


def _graph_settings():
    return _ha_settings().model_copy(
        update={
            "teams_app_id": "app",
            "teams_app_secret": SecretStr("sec"),
            "teams_tenant_id": "ten",
            "teams_service_url": "https://bot.test/emea/",
            "teams_conversation_id": "conv",
        }
    )


async def test_graph_notifier_posts_card() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if "oauth2" in req.url.path:
            return httpx.Response(200, json={"access_token": "tok"})
        return httpx.Response(201, json={"id": "act-1"})

    s = _graph_settings()
    assert isinstance(teams.get_notifier(s), GraphTeamsNotifier)
    n = GraphTeamsNotifier(s, httpx.MockTransport(handler))
    assert await n.notify(NOTICE) == {"activity_id": "act-1"}
    post = seen[1]
    assert post.url.path == "/emea/v3/conversations/conv/activities"
    assert post.headers["authorization"] == "Bearer tok"
    body = json.loads(post.content)
    assert body["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"


async def test_graph_notifier_errors_are_teams_errors() -> None:
    n = GraphTeamsNotifier(
        _graph_settings(), httpx.MockTransport(lambda r: httpx.Response(503, json={}))
    )
    with pytest.raises(TeamsError):
        await n.notify(NOTICE)

    def conn(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x")

    with pytest.raises(TeamsError):
        await GraphTeamsNotifier(_graph_settings(), httpx.MockTransport(conn)).notify(NOTICE)


async def test_alert_ducking_does_not_block(monkeypatch, db, redis) -> None:
    from app.ma import service as ma_service

    async def slow(*a: Any, timeout: float = 30, **k: Any) -> list[str]:  # noqa: ASYNC109
        assert timeout == 0.1
        await asyncio.sleep(0.2)
        return []

    monkeypatch.setattr(ma_service, "duck", slow)
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: _ha_settings().model_copy(update={"alert_duck_timeout": 0.1}),
    )
    ha, calls = mock_ha()
    ann = service.build_announcement(
        _ha_settings(), text="x", speakers=[], is_test=False, priority=Priority.alert, author=None
    )
    ann.targets = [
        {
            "id": "1",
            "entity_id": "media_player.al_duck",
            "notify_service": None,
            "ma_player_id": "p",
        }
    ]
    async with db() as s:
        s.add(ann)
        await s.commit()
    await queue.push(redis, str(ann.id), Priority.alert)
    assert await asyncio.wait_for(service.process_one(db, redis, ha, object()), 5)  # type: ignore[arg-type]
    assert calls


async def _both_fail(ha_api, fake_teams, monkeypatch) -> dict[str, Any]:
    fake_teams.fail = True
    app.dependency_overrides[get_settings] = lambda: _ha_settings().model_copy(
        update={"ha_url": ""}
    )
    inc = await _draft(ha_api)
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    assert r.status_code == 502, r.text
    return inc


async def test_both_channels_failed_then_resend(ha_api, fake_teams, make_user, login_as) -> None:
    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.al_bf")
    inc = await _both_fail(ha_api, fake_teams, None)
    got = (await ha_api.get(f"/incidents/{inc['id']}")).json()
    assert got["status"] == "failed" and got["delivery"]["tts"]["ok"] is False
    fake_teams.fail = False
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert r.json()["delivery"]["teams"]["ok"] is True
    assert (await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)).status_code == 409


async def test_resend_after_crash_requires_stale_lease(
    ha_api, fake_teams, make_user, login_as, db
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.models import Incident

    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.al_crash")
    inc = await _draft(ha_api)
    async with db() as s:
        await s.execute(
            update(Incident)
            .where(Incident.id == uuid.UUID(inc["id"]))
            .values(status="sent", sent_at=datetime.now(UTC), delivery=None)
        )
        await s.commit()
    assert (await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)).status_code == 409
    async with db() as s:
        await s.execute(
            update(Incident)
            .where(Incident.id == uuid.UUID(inc["id"]))
            .values(sent_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await s.commit()
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    assert r.status_code == 200 and r.json()["delivery"]["teams"]["ok"]


async def test_concurrent_resend_single_winner(ha_api, fake_teams, make_user, login_as) -> None:
    await _login(make_user, login_as)
    await _speaker(ha_api, "media_player.al_conc")
    inc = await _both_fail(ha_api, fake_teams, None)
    fake_teams.fail = False
    rs = await asyncio.gather(
        *[ha_api.post(f"/incidents/{inc['id']}/send", json=GO) for _ in range(4)]
    )
    codes = sorted(r.status_code for r in rs)
    assert codes.count(200) == 1 and codes.count(409) == 3
    assert len(fake_teams.notices) == 2  # 1 échec initial + 1 renvoi


async def test_failed_incident_can_be_closed(ha_api, fake_teams, make_user, login_as) -> None:
    await _login(make_user, login_as)
    inc = await _both_fail(ha_api, fake_teams, None)
    assert (await ha_api.post(f"/incidents/{inc['id']}/close")).status_code == 200


async def test_rate_limit_429(ha_api, fake_teams, make_user, login_as, redis) -> None:
    await _login(make_user, login_as)
    await redis.flushdb()
    app.dependency_overrides[get_settings] = lambda: _ha_settings().model_copy(
        update={"alert_rate_limit": 2}
    )
    assert (await ha_api.post("/incidents", json=BODY)).status_code == 201
    assert (await ha_api.post("/incidents", json=BODY)).status_code == 201
    assert (await ha_api.post("/incidents", json=BODY)).status_code == 429


async def test_incident_type_enum(ha_api, fake_teams, make_user, login_as) -> None:
    await _login(make_user, login_as)
    for t in ("evacuation", "confinement", "intrusion", "incendie", "meteo", "autre"):
        assert (await ha_api.post("/incidents", json={**BODY, "type": t})).status_code == 201
    assert (await ha_api.post("/incidents", json={**BODY, "type": "pizza"})).status_code == 422


async def test_delivery_persisted_after_session_failure(
    ha_api, fake_teams, make_user, login_as, monkeypatch
) -> None:
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    await _login(make_user, login_as)
    inc = await _draft(ha_api)
    real = AsyncSession.commit
    calls = {"n": 0}

    async def flaky(self) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # commit final de la diffusion
            raise SQLAlchemyError("boom")
        await real(self)

    monkeypatch.setattr(AsyncSession, "commit", flaky)
    r = await ha_api.post(f"/incidents/{inc['id']}/send", json=GO)
    monkeypatch.undo()
    assert r.status_code in (200, 502), r.text
    got = (await ha_api.get(f"/incidents/{inc['id']}")).json()
    assert got["delivery"] is not None and got["status"] in ("sent", "failed")
