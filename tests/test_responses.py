# ruff: noqa: F811
import json
import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import select

from app.alerts import botauth, responses
from app.alerts import router as alerts_router
from app.alerts.roster import GraphRoster, LogRoster, Member, RosterError, _cache
from app.alerts.teams import GraphTeamsNotifier, IncidentNotice, TeamsError
from app.config import get_settings
from app.main import app
from app.models import AuditLog, Incident, Response, Role
from tests.helpers import _KEY, KID, _settings, audit_actions
from tests.test_alerts import FakeTeams
from tests.test_ha import _ha_settings, ha_api, redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_db")

APP_ID = "33333333-3333-3333-3333-333333333333"
U1 = "aaaaaaaa-0000-0000-0000-000000000001"
U2 = "aaaaaaaa-0000-0000-0000-000000000002"
U3 = "aaaaaaaa-0000-0000-0000-000000000003"


class FakeRoster:
    def __init__(self, members: list[Member] | None = None, fail: bool = False) -> None:
        self.configured = True
        self._m = (
            members
            if members is not None
            else [
                Member(U1, "a@epitech.eu", "Alice"),
                Member(U2, "=cmd@epitech.eu", "Bob"),
                Member(U3, "c@epitech.eu", "Carl"),
            ]
        )
        self._fail = fail

    async def members(self) -> list[Member]:
        if self._fail:
            raise RosterError("graph http 500")
        return self._m


class RemindTeams(FakeTeams):
    def __init__(self, fail: bool = False) -> None:
        super().__init__(fail)
        self.reminded: list[list[Member]] = []

    async def remind(self, notice: IncidentNotice, targets: list[Member]) -> dict[str, Any]:
        if self.fail:
            raise TeamsError("teams http 500")
        self.reminded.append(targets)
        return {"activity_id": "r1"}


def _bot_settings():
    return _ha_settings().model_copy(update={"teams_app_id": APP_ID})


def bot_token(key: Any = _KEY, **over: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": botauth.ISSUER,
        "aud": APP_ID,
        "iat": now,
        "exp": now + 300,
        "serviceurl": "https://smba.test/",
        **over,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture
def bot(ha_api, monkeypatch: pytest.MonkeyPatch):
    app.dependency_overrides[get_settings] = _bot_settings

    async def _jwks() -> list[dict[str, Any]]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
        return [{**jwk, "kid": KID, "alg": "RS256", "use": "sig"}]

    monkeypatch.setattr(botauth, "fetch_jwks", _jwks)
    botauth._cache.update(keys=None, at=0.0)
    return ha_api


@pytest.fixture
def staff_api(ha_api):
    teams = RemindTeams()
    roster = FakeRoster()
    app.dependency_overrides[alerts_router.get_teams_notifier] = lambda: teams
    app.dependency_overrides[alerts_router.get_roster] = lambda: roster
    return ha_api, teams, roster


async def _sent_incident(db, status: str = "sent") -> uuid.UUID:
    async with db() as s:
        i = Incident(id=uuid.uuid4(), type="autre", title="T <b>", message="M", status=status)
        s.add(i)
        await s.commit()
        return i.id


def activity(incident_id: Any, status: str = "safe_campus", aad: str | None = U1, **data: Any):
    return {
        "type": "invoke",
        "name": "adaptiveCard/action",
        "serviceUrl": "https://smba.test/",
        "from": {"aadObjectId": aad, "name": "Alice"},
        "value": {
            "action": {
                "type": "Action.Execute",
                "verb": "incident_response",
                "data": {"incident_id": str(incident_id), "status": status, **data},
            }
        },
    }


def auth(tok: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok or bot_token()}"}


# --- webhook ---------------------------------------------------------------


async def test_webhook_503_without_app_id(ha_api) -> None:
    r = await ha_api.post("/teams/messages", json={"type": "message"})
    assert r.status_code == 503


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer garbage"},
        auth(bot_token(aud="other")),
        auth(bot_token(iss="https://evil.test")),
        auth(bot_token(exp=int(time.time()) - 3600)),
    ],
)
async def test_webhook_rejects_bad_jwt(bot, db, headers) -> None:
    r = await bot.post("/teams/messages", json=activity(uuid.uuid4()), headers=headers)
    assert r.status_code == 401


async def test_webhook_rejects_foreign_key_and_service_url(bot, other_key) -> None:
    r = await bot.post(
        "/teams/messages", json=activity(uuid.uuid4()), headers=auth(bot_token(other_key))
    )
    assert r.status_code == 401
    tok = bot_token(serviceurl="https://other.test/")
    r = await bot.post("/teams/messages", json=activity(uuid.uuid4()), headers=auth(tok))
    assert r.status_code == 401


async def test_webhook_records_upserts_and_publishes(bot, db, redis) -> None:
    iid = await _sent_incident(db)
    pubsub = redis.pubsub()
    await pubsub.subscribe(responses.channel(iid))
    r = await bot.post("/teams/messages", json=activity(iid, details="  ok \x00 "), headers=auth())
    assert r.status_code == 200 and r.json()["statusCode"] == 200
    assert r.json()["type"] == "application/vnd.microsoft.activity.message"
    r = await bot.post("/teams/messages", json=activity(iid, "danger"), headers=auth())
    assert r.json()["statusCode"] == 200
    async with db() as s:
        rows = (await s.scalars(select(Response).where(Response.incident_id == iid))).all()
    assert len(rows) == 1 and rows[0].status == "danger" and rows[0].text is None
    msgs = []
    for _ in range(3):
        m = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
        if m:
            msgs.append(m)
    assert len(msgs) == 2
    await pubsub.aclose()
    async with db() as s:
        acts = list(await s.scalars(select(AuditLog.action).where(AuditLog.actor == U1)))
    assert acts.count("incident_response") >= 2


async def test_webhook_text_is_stored_cleaned(bot, db) -> None:
    iid = await _sent_incident(db)
    await bot.post(
        "/teams/messages", json=activity(iid, details="  ok \x00 " + "x" * 600), headers=auth()
    )
    async with db() as s:
        row = await s.scalar(select(Response).where(Response.incident_id == iid))
    assert row is not None and row.text.startswith("ok ") and len(row.text) == 500


@pytest.mark.parametrize("status", ["closed", "draft", "failed"])
async def test_webhook_rejects_non_sent_incident(bot, db, status) -> None:
    iid = await _sent_incident(db, status)
    r = await bot.post("/teams/messages", json=activity(iid), headers=auth())
    assert r.status_code == 200 and r.json()["statusCode"] == 409
    async with db() as s:
        assert await s.scalar(select(Response).where(Response.incident_id == iid)) is None


async def test_webhook_rejects_unknown_and_invalid(bot, db) -> None:
    r = await bot.post("/teams/messages", json=activity(uuid.uuid4()), headers=auth())
    assert r.json()["statusCode"] == 404
    iid = await _sent_incident(db)
    for body in (
        activity(iid, "bogus"),
        activity(iid, aad="not-a-uuid"),
        activity("nope"),
    ):
        r = await bot.post("/teams/messages", json=body, headers=auth())
        assert r.json()["statusCode"] == 400
    bad = activity(iid)
    bad["value"]["action"]["verb"] = "other"
    assert (await bot.post("/teams/messages", json=bad, headers=auth())).json()["statusCode"] == 400
    r = await bot.post(
        "/teams/messages",
        json={"type": "message", "serviceUrl": "https://smba.test/"},
        headers=auth(),
    )
    assert r.status_code == 200
    r = await bot.post("/teams/messages", content=b"[1]", headers=auth())
    assert r.status_code == 400


# --- API staff ---------------------------------------------------------------


async def _answer(db, iid, aad, status="safe_campus", text=None, name="N", email=None) -> None:
    async with db() as s:
        await responses.record(s, iid, aad_id=aad, name=name, email=email, status=status, text=text)
        await s.commit()


async def test_responses_report_and_roles(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    iid = await _sent_incident(db)
    assert (await api.get(f"/incidents/{iid}/responses")).status_code == 401
    await login_as(await make_user(Role.staff))
    await _answer(db, iid, U1, "danger", "aide")
    await _answer(db, iid, U1, "safe_offsite", "ok")
    r = await api.get(f"/incidents/{iid}/responses")
    assert r.status_code == 200
    d = r.json()
    assert d["counts"] == {
        "safe_campus": 0,
        "safe_offsite": 1,
        "danger": 0,
        "responded": 1,
        "not_responded": 2,
        "roster_total": 3,
    }
    assert d["respondents"][0]["email"] == "a@epitech.eu"
    assert {m["aad_id"] for m in d["non_respondents"]} == {U2, U3}
    assert (await api.get(f"/incidents/{uuid.uuid4()}/responses")).status_code == 404


async def test_responses_roster_failure_and_default(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    await login_as(await make_user(Role.admin))
    iid = await _sent_incident(db)
    await _answer(db, iid, U1)
    app.dependency_overrides[alerts_router.get_roster] = lambda: FakeRoster(fail=True)
    d = (await api.get(f"/incidents/{iid}/responses")).json()
    assert d["roster"]["error"] and d["non_respondents"] == [] and d["counts"]["responded"] == 1
    app.dependency_overrides[alerts_router.get_roster] = lambda: LogRoster()
    d = (await api.get(f"/incidents/{iid}/responses")).json()
    assert d["roster"] == {"configured": False, "error": None}


async def test_export_csv_neutralizes_formulas(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    u = await make_user(Role.staff)
    await login_as(u)
    iid = await _sent_incident(db)
    await _answer(db, iid, U1, "danger", '=HYPERLINK("http://x")', name="+evil", email="@a.eu")
    r = await api.get(f"/incidents/{iid}/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("statut,")
    assert "'+evil" in lines[1] and "'@a.eu" in lines[1] and "'=HYPERLINK" in lines[1]
    assert any(ln.startswith("no_response,Bob,'=cmd@epitech.eu") for ln in lines)
    assert "incident_export" in await audit_actions(db, u.email)
    assert responses.neutralize("normal") == "normal" and responses.neutralize(None) == ""
    assert responses.neutralize(" =1") == "' =1" and responses.neutralize("\t1") == "'\t1"


async def test_remind(staff_api, make_user, login_as, db, redis) -> None:
    api, teams, _ = staff_api
    u = await make_user(Role.staff)
    await login_as(u)
    iid = await _sent_incident(db)
    await redis.delete(f"remind:cooldown:{iid}")
    await _answer(db, iid, U1)
    r = await api.post(f"/incidents/{iid}/remind")
    assert r.status_code == 200 and r.json()["reminded"] == 2
    assert {m.aad_id for m in teams.reminded[0]} == {U2, U3}
    assert (await api.post(f"/incidents/{iid}/remind")).status_code == 429
    assert "incident_remind" in await audit_actions(db, u.email)
    await redis.delete(f"remind:cooldown:{iid}")


async def test_remind_guards(staff_api, make_user, login_as, db, redis) -> None:
    api, teams, _ = staff_api
    await login_as(await make_user(Role.admin))
    closed = await _sent_incident(db, "closed")
    assert (await api.post(f"/incidents/{closed}/remind")).status_code == 409
    iid = await _sent_incident(db)
    await redis.delete(f"remind:cooldown:{iid}")
    app.dependency_overrides[alerts_router.get_roster] = lambda: LogRoster()
    assert (await api.post(f"/incidents/{iid}/remind")).status_code == 409
    app.dependency_overrides[alerts_router.get_roster] = lambda: FakeRoster(fail=True)
    assert (await api.post(f"/incidents/{iid}/remind")).status_code == 502
    app.dependency_overrides[alerts_router.get_roster] = lambda: FakeRoster([])
    assert (await api.post(f"/incidents/{iid}/remind")).json() == {"reminded": 0}
    app.dependency_overrides[alerts_router.get_roster] = lambda: FakeRoster()
    teams.fail = True
    assert (await api.post(f"/incidents/{iid}/remind")).status_code == 502
    teams.fail = False  # cooldown libéré après l'échec
    assert (await api.post(f"/incidents/{iid}/remind")).status_code == 200
    await redis.delete(f"remind:cooldown:{iid}")


async def test_remind_rate_limited(staff_api, make_user, login_as, db, redis) -> None:
    api, _, _ = staff_api
    u = await make_user(Role.admin)
    await login_as(u)
    await redis.delete(f"rl:incidents:remind:{u.id}")
    app.dependency_overrides[get_settings] = lambda: _ha_settings().model_copy(
        update={"alert_rate_limit": 1}
    )
    iid = await _sent_incident(db, "closed")
    assert (await api.post(f"/incidents/{iid}/remind")).status_code == 409
    assert (await api.post(f"/incidents/{iid}/remind")).status_code == 429
    await redis.delete(f"rl:incidents:remind:{u.id}")


# --- SSE ---------------------------------------------------------------------


async def test_event_stream_yields_updates_and_closes(redis) -> None:
    iid = uuid.uuid4()
    gen = responses.event_stream(redis, iid, lambda: _false(), max_seconds=5, keepalive=0.2)
    assert await anext(gen) == "retry: 3000\n\n"
    assert await anext(gen) == ": keepalive\n\n"
    await redis.publish(responses.channel(iid), json.dumps({"status": "danger"}))
    chunk = await anext(gen)
    while chunk.startswith(":"):
        chunk = await anext(gen)
    assert chunk == 'event: update\ndata: {"status": "danger"}\n\n'
    await gen.aclose()


async def _false() -> bool:
    return False


async def test_event_stream_stops_on_disconnect(redis) -> None:
    gen = responses.event_stream(redis, uuid.uuid4(), lambda: _true(), keepalive=0.1)
    assert [c async for c in gen] == ["retry: 3000\n\n"]


async def _true() -> bool:
    return True


async def test_stream_endpoint_auth_and_404(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    iid = await _sent_incident(db)
    assert (await api.get(f"/incidents/{iid}/stream")).status_code == 401
    await login_as(await make_user(Role.staff))
    assert (await api.get(f"/incidents/{uuid.uuid4()}/stream")).status_code == 404


# --- purge / modèle ----------------------------------------------------------


async def test_purge_retention(db) -> None:
    from datetime import UTC, datetime, timedelta

    iid = await _sent_incident(db)
    await _answer(db, iid, U1)
    await _answer(db, iid, U2)
    async with db() as s:
        row = await s.scalar(
            select(Response).where(Response.incident_id == iid, Response.user_aad_id == U1)
        )
        row.received_at = datetime.now(UTC) - timedelta(days=100)
        await s.commit()
    async with db() as s:
        assert await responses.purge(s, 0) == 0
        assert await responses.purge(s, 90) >= 1
        await s.commit()
    async with db() as s:
        left = (
            await s.scalars(select(Response.user_aad_id).where(Response.incident_id == iid))
        ).all()
    assert list(left) == [U2]


# --- roster Graph / Teams notifier -----------------------------------------


def _graph_transport(calls: list[str], fail: bool = False) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "g"})
        if fail:
            return httpx.Response(500)
        assert req.headers["authorization"] == "Bearer g"
        if "page2" in str(req.url):
            return httpx.Response(
                200,
                json={"value": [{"@odata.type": "#microsoft.graph.user", "id": U2.upper(),
                                 "displayName": "Bob", "userPrincipalName": "b@x.eu"}]},
            )  # fmt: skip
        return httpx.Response(
            200,
            json={
                "value": [
                    {"@odata.type": "#microsoft.graph.user", "id": U1, "displayName": "Alice",
                     "mail": "a@x.eu"},
                    {"@odata.type": "#microsoft.graph.group", "id": U3},
                ],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/page2",
            },
        )  # fmt: skip

    return httpx.MockTransport(handler)


async def test_graph_roster_paging_filter_cache() -> None:
    _cache.clear()
    s = _settings().model_copy(update={"roster_group_id": "grp"})
    calls: list[str] = []
    g = GraphRoster(s, _graph_transport(calls))
    members = await g.members()
    assert [(m.aad_id, m.email) for m in members] == [(U1, "a@x.eu"), (U2, "b@x.eu")]
    n = len(calls)
    await g.members()
    assert len(calls) == n  # cache
    _cache.clear()


async def test_graph_roster_error() -> None:
    _cache.clear()
    s = _settings().model_copy(update={"roster_group_id": "grp"})
    with pytest.raises(RosterError):
        await GraphRoster(s, _graph_transport([], fail=True)).members()


async def test_graph_notifier_remind_mentions() -> None:
    from pydantic import SecretStr

    s = _settings().model_copy(
        update={
            "teams_app_id": APP_ID,
            "teams_app_secret": SecretStr("s"),
            "teams_service_url": "https://smba.test",
            "teams_conversation_id": "conv",
        }
    )
    sent: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in req.url.path:
            return httpx.Response(200, json={"access_token": "t"})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "act"})

    n = GraphTeamsNotifier(s, httpx.MockTransport(handler))
    out = await n.remind(
        IncidentNotice("i", "Titre", "Msg", False), [Member(U1, None, "A<b>"), Member(U2, None, "")]
    )
    assert out["targets"] == 2 and out["mentioned"] == 2
    act = sent[0]
    assert act["attachments"][0]["content"]["body"][0]["text"].startswith("RAPPEL")
    assert "<at>A&lt;b&gt;</at>" in act["text"] and act["entities"][0]["mentioned"]["id"] == U1


# --- dashboard UI ------------------------------------------------------------


async def test_dashboard_pages(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    iid = await _sent_incident(db)
    r = await api.get(f"/alerts/{iid}")
    assert r.status_code == 302 and r.headers["location"] == "/login"
    await login_as(await make_user(Role.staff))
    await _answer(db, iid, U1, "danger", "<script>x</script>", name="Alice")
    r = await api.get(f"/alerts/{iid}")
    assert r.status_code == 200
    assert "T &lt;b&gt;" in r.text and f"/incidents/{iid}/stream" in r.text
    assert f"/incidents/{iid}/export.csv" in r.text and f"/incidents/{iid}/remind" in r.text
    f = await api.get(f"/ui/alerts/{iid}/responses")
    assert f.status_code == 200 and "&lt;script&gt;" in f.text and "<script>x" not in f.text
    assert "Bob" in f.text and "Alice" in f.text
    assert (await api.get(f"/alerts/{uuid.uuid4()}")).status_code == 404


async def test_dashboard_fragment_requires_login(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    iid = await _sent_incident(db)
    r = await api.get(f"/ui/alerts/{iid}/responses")
    assert r.status_code == 302


# --- durcissement (review phase 6) -------------------------------------------


async def test_jwks_refresh_cooldown_on_unknown_kid(bot, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    orig = botauth.fetch_jwks

    async def counting() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return await orig()

    monkeypatch.setattr(botauth, "fetch_jwks", counting)
    bad = jwt.encode(
        {"iss": botauth.ISSUER, "aud": APP_ID, "exp": int(time.time()) + 300},
        _KEY,
        algorithm="RS256",
        headers={"kid": "unknown"},
    )
    for _ in range(3):
        r = await bot.post("/teams/messages", json=activity(uuid.uuid4()), headers=auth(bad))
        assert r.status_code == 401
    assert calls == 1
    ok = auth(bot_token(serviceurl="https://smba.test/"))
    body = {"type": "message", "serviceUrl": "https://smba.test/"}
    r = await bot.post("/teams/messages", json=body, headers=ok)
    assert r.status_code == 200 and calls == 1


async def test_webhook_requires_both_service_urls(bot) -> None:
    ok = bot_token(serviceurl="https://smba.test/")
    body = activity(uuid.uuid4())
    del body["serviceUrl"]
    assert (await bot.post("/teams/messages", json=body, headers=auth(ok))).status_code == 401
    r = await bot.post(
        "/teams/messages", json=activity(uuid.uuid4()), headers=auth(bot_token(serviceurl=None))
    )
    assert r.status_code == 401  # claim absent


async def test_webhook_body_limit_before_auth(bot) -> None:
    big = b"{" + b" " * (70 * 1024) + b"}"
    r = await bot.post("/teams/messages", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


async def test_webhook_tenant_check(bot, db) -> None:
    tenant = "44444444-4444-4444-4444-444444444444"
    app.dependency_overrides[get_settings] = lambda: _bot_settings().model_copy(
        update={"teams_tenant_id": tenant}
    )
    iid = await _sent_incident(db)
    tok = auth(bot_token(serviceurl="https://smba.test/"))
    for cd in (None, {"tenant": {"id": "other"}}, {"tenant": "x"}, "x"):
        a = activity(iid)
        if cd is not None:
            a["channelData"] = cd
        assert (await bot.post("/teams/messages", json=a, headers=tok)).status_code == 403
    a = activity(iid)
    a["channelData"] = {"tenant": {"id": tenant}}
    r = await bot.post("/teams/messages", json=a, headers=tok)
    assert r.status_code == 200 and r.json()["statusCode"] == 200


@pytest.mark.parametrize(
    "mut",
    [
        {"value": "x"},
        {"value": {"action": "x"}},
        {"from": "x"},
        {"from": {"aadObjectId": {"a": 1}}},
    ],
)
async def test_webhook_malformed_types_are_400_not_500(bot, db, mut) -> None:
    a = {**activity(await _sent_incident(db)), **mut}
    tok = auth(bot_token(serviceurl="https://smba.test/"))
    r = await bot.post("/teams/messages", json=a, headers=tok)
    assert r.status_code == 200 and r.json()["statusCode"] == 400
    a = activity(await _sent_incident(db))
    a["value"]["action"]["data"] = ["x"]
    r = await bot.post("/teams/messages", json=a, headers=tok)
    assert r.json()["statusCode"] == 400
    a["value"]["action"]["data"] = {"incident_id": ["x"], "status": {"a": 1}}
    assert (await bot.post("/teams/messages", json=a, headers=tok)).json()["statusCode"] == 400


async def test_export_csv_roster_failure_is_502(staff_api, make_user, login_as, db) -> None:
    api, _, _ = staff_api
    await login_as(await make_user(Role.staff))
    iid = await _sent_incident(db)
    app.dependency_overrides[alerts_router.get_roster] = lambda: FakeRoster(fail=True)
    assert (await api.get(f"/incidents/{iid}/export.csv")).status_code == 502


async def test_remind_reports_all_mentioned(staff_api, make_user, login_as, db, redis) -> None:
    api, teams, _ = staff_api
    await login_as(await make_user(Role.staff))
    iid = await _sent_incident(db)
    await redis.delete(f"remind:cooldown:{iid}")
    members = [Member(f"bbbbbbbb-0000-0000-0000-{i:012d}", None, f"M{i}") for i in range(45)]
    app.dependency_overrides[alerts_router.get_roster] = lambda: FakeRoster(members)
    r = await api.post(f"/incidents/{iid}/remind")
    assert r.status_code == 200 and r.json()["reminded"] == 45
    await redis.delete(f"remind:cooldown:{iid}")


async def test_graph_notifier_remind_batches() -> None:
    from pydantic import SecretStr

    s = _settings().model_copy(
        update={
            "teams_app_id": APP_ID,
            "teams_app_secret": SecretStr("s"),
            "teams_service_url": "https://smba.test",
            "teams_conversation_id": "conv",
        }
    )
    sent: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if "oauth2" in req.url.path:
            return httpx.Response(200, json={"access_token": "t"})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "act"})

    n = GraphTeamsNotifier(s, httpx.MockTransport(handler))
    targets = [Member(f"bbbbbbbb-0000-0000-0000-{i:012d}", None, f"M{i}") for i in range(45)]
    out = await n.remind(IncidentNotice("i", "T", "M", False), targets)
    assert out["mentioned"] == 45 and [len(a["entities"]) for a in sent] == [20, 20, 5]


async def test_stream_slots_cap_per_user(redis) -> None:
    uid = uuid.uuid4()
    await redis.delete(f"sse:streams:{uid}")
    for _ in range(responses.MAX_STREAMS_PER_USER):
        assert await responses.acquire_stream_slot(redis, uid)
    assert not await responses.acquire_stream_slot(redis, uid)
    await responses.release_stream_slot(redis, uid)
    assert await responses.acquire_stream_slot(redis, uid)
    for _ in range(responses.MAX_STREAMS_PER_USER + 1):
        await responses.release_stream_slot(redis, uid)
    assert await redis.get(f"sse:streams:{uid}") is None


async def test_event_stream_releases_slot(redis) -> None:
    released = []

    async def on_close() -> None:
        released.append(1)

    gen = responses.event_stream(redis, uuid.uuid4(), lambda: _true(), on_close=on_close)
    assert [c async for c in gen] == ["retry: 3000\n\n"]
    assert released == [1]
