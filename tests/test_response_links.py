# ruff: noqa: F811
import hashlib
import inspect
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select, text

from app.alerts import autoremind, links, responses, service, webhook
from app.alerts import router as alerts_router
from app.alerts.roster import LogRoster, Member, StudentRoster, get_roster_provider
from app.alerts.webhook import HttpWebhook, WebhookError
from app.config import Settings, get_settings
from app.ha.router import get_redis
from app.logging import RedactLinkToken
from app.main import app
from app.models import (
    AuditLog,
    Incident,
    IncidentNotification,
    Response,
    ResponseLink,
    Role,
    Student,
)
from tests.helpers import _settings, audit_actions
from tests.test_alerts import FakeTeams
from tests.test_ha import redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_db")

HOOK = "https://prod.flow.test/workflows/abc/triggers/manual/paths/invoke?sig=SECRETSIG"


def cfg(**over: Any) -> Settings:
    base = {
        "alert_webhook_url": HOOK,
        "alert_webhook_batch": 3,
        "alert_webhook_retries": 3,
        "remind_cooldown": 0,
        "auto_remind_interval": 600,
        "auto_remind_max": 3,
        "rl_auth_limit": 0,
        **over,
    }
    return Settings(**{**dict(_settings()), **base})


@pytest.fixture(autouse=True)
async def _clean(db):
    async with db() as s:
        await s.execute(delete(Incident))
        await s.execute(delete(Student))
        await s.commit()


@pytest.fixture
def app_redis(redis):
    """Expose Redis à `app.state` (comme le lifespan) le temps du test, sans polluer les autres."""
    app.state.redis = redis
    yield redis
    del app.state.redis


@pytest.fixture
def pub(client, redis, app_redis):
    settings = cfg()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_redis] = lambda: redis
    return client


async def mk_students(db, n: int = 3) -> list[Student]:
    async with db() as s:
        rows = [
            Student(
                id=uuid.uuid4(),
                email=f"s{i}-{uuid.uuid4().hex[:6]}@epitech.eu",
                first_name=f"Prenom{i}",
                last_name=f"Nom{i}",
            )
            for i in range(n)
        ]
        s.add_all(rows)
        await s.commit()
        return rows


async def mk_incident(db, status: str = "sent", is_test: bool = False, sent_ago: int = 0):
    async with db() as s:
        i = Incident(
            id=uuid.uuid4(),
            type="incendie",
            title="Alerte incendie",
            message="Evacuez le batiment",
            status=status,
            is_test=is_test,
            sent_at=datetime.now(UTC) - timedelta(seconds=sent_ago) if status != "draft" else None,
        )
        s.add(i)
        await s.commit()
        return i


async def issue(db, incident, student, hours: int = 48) -> str:
    """Crée un lien expirant dans `hours` heures et renvoie le jeton (extrait de l'URL)."""
    async with db() as s:
        (it,) = await links.create_links(s, cfg(), incident.id, [student])
        link = await s.get(ResponseLink, it.link_id)
        link.expires_at = datetime.now(UTC) + timedelta(hours=hours)
        await s.commit()
    return it.url.rsplit("/", 1)[1]


async def count(db, model, *where) -> int:
    async with db() as s:
        return int(await s.scalar(select(func.count()).select_from(model).where(*where)) or 0)


# ---------- page publique ----------


async def test_token_is_hashed_only(db) -> None:
    (st,) = await mk_students(db, 1)
    inc = await mk_incident(db)
    token = await issue(db, inc, st)
    assert len(token) == 43
    async with db() as s:
        link = (await s.scalars(select(ResponseLink))).one()
        row = (await s.execute(text("select row_to_json(r) from response_links r"))).scalar_one()
    assert link.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in json.dumps(row)


async def test_get_page_headers_and_content(pub, db) -> None:
    (st,) = await mk_students(db, 1)
    inc = await mk_incident(db)
    token = await issue(db, inc, st)
    r = await pub.get(f"/r/{token}")
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"]
    assert "noindex" in r.headers["x-robots-tag"]
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "script-src" not in r.headers["content-security-policy"]
    for v in ("safe_campus", "safe_offsite", "danger"):
        assert f'value="{v}"' in r.text
    assert 'name="details"' in r.text and "Alerte incendie" in r.text
    assert st.email not in r.text and st.last_name not in r.text and token not in r.text


async def test_post_creates_then_updates_response(pub, db) -> None:
    (st,) = await mk_students(db, 1)
    inc = await mk_incident(db)
    token = await issue(db, inc, st)
    r = await pub.post(f"/r/{token}", data={"status": "safe_campus"})
    assert r.status_code == 200 and "Réponse enregistrée" in r.text
    r = await pub.post(f"/r/{token}", data={"status": "danger", "details": "Salle B12 =cmd"})
    assert r.status_code == 200 and "Salle B12" in r.text
    async with db() as s:
        row = (await s.scalars(select(Response))).one()  # une seule ligne : upsert
        link = (await s.scalars(select(ResponseLink))).one()
        logs = list(await s.scalars(select(AuditLog).where(AuditLog.actor == str(st.id))))
    assert row.user_aad_id == str(st.id) and row.user_email == st.email
    assert row.user_name == "Prenom0 Nom0"
    assert row.status == "danger" and row.text == "Salle B12 =cmd"
    assert link.used_at is not None
    assert [a.action for a in logs] == ["incident_response", "incident_response"]
    assert all(token not in json.dumps(a.detail) for a in logs)
    assert logs[0].detail["channel"] == "link"


async def test_post_invalid_status_and_oversize(pub, db) -> None:
    (st,) = await mk_students(db, 1)
    inc = await mk_incident(db)
    token = await issue(db, inc, st)
    assert (await pub.post(f"/r/{token}", data={"status": "nope"})).status_code == 422
    assert (await pub.post(f"/r/{token}", data={})).status_code == 422
    big = await pub.post(f"/r/{token}", data={"status": "danger", "details": "x" * 20000})
    assert big.status_code == 413
    assert await count(db, Response) == 0


async def test_text_is_truncated(pub, db) -> None:
    (st,) = await mk_students(db, 1)
    inc = await mk_incident(db)
    token = await issue(db, inc, st)
    r = await pub.post(f"/r/{token}", data={"status": "danger", "details": "é" * 1500})
    assert r.status_code == 200
    async with db() as s:
        assert len((await s.scalars(select(Response))).one().text) == responses.TEXT_MAX


async def test_neutral_pages_do_not_leak(pub, db) -> None:
    (st,) = await mk_students(db, 1)
    sent = await mk_incident(db)
    closed = await mk_incident(db, status="closed")
    draft = await mk_incident(db, status="draft")
    ok = await issue(db, sent, st)
    expired = await issue(db, sent, st, hours=-1)
    t_closed = await issue(db, closed, st)
    t_draft = await issue(db, draft, st)
    cases = {
        links.new_token(): 404,
        "short": 404,
        "x" * 43: 404,
        t_draft: 404,
        expired: 410,
        t_closed: 410,
    }
    bodies = set()
    for tok, code in cases.items():
        r = await pub.get(f"/r/{tok}")
        assert r.status_code == code, tok
        assert "noindex" in r.headers["x-robots-tag"] and "no-store" in r.headers["cache-control"]
        assert "Alerte incendie" not in r.text and st.email not in r.text
        bodies.add(r.text)
        p = await pub.post(f"/r/{tok}", data={"status": "safe_campus"})
        assert p.status_code == code
    assert len(bodies) == 1  # même page quel que soit le motif
    assert await count(db, Response) == 0
    assert (await pub.get(f"/r/{ok}")).status_code == 200


async def test_closing_blocks_further_edits(pub, db) -> None:
    (st,) = await mk_students(db, 1)
    inc = await mk_incident(db)
    token = await issue(db, inc, st)
    assert (await pub.post(f"/r/{token}", data={"status": "safe_offsite"})).status_code == 200
    async with db() as s:
        await s.execute(text("update incidents set status='closed' where id=:i"), {"i": inc.id})
        await s.commit()
    assert (await pub.post(f"/r/{token}", data={"status": "danger"})).status_code == 410
    async with db() as s:
        assert (await s.scalars(select(Response))).one().status == "safe_offsite"


async def test_test_incident_is_answerable_and_marked(pub, db) -> None:
    (st,) = await mk_students(db, 1)
    inc = await mk_incident(db, is_test=True)
    token = await issue(db, inc, st)
    r = await pub.get(f"/r/{token}")
    assert r.status_code == 200 and "EXERCICE" in r.text


async def test_rate_limit(client, redis, app_redis) -> None:
    settings = cfg(rl_response_link_limit=2)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_redis] = lambda: redis
    for k in await redis.keys("rl:rlink:*"):
        await redis.delete(k)
    tok = links.new_token()
    codes = [(await client.get(f"/r/{tok}")).status_code for _ in range(4)]
    assert codes == [404, 404, 429, 429]
    for k in await redis.keys("rl:rlink:*"):
        await redis.delete(k)


def test_access_log_redaction() -> None:
    tok = links.new_token()
    rec = logging.LogRecord(
        "uvicorn.access", 20, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", f"/r/{tok}", "1.1", 200), None,
    )  # fmt: skip
    RedactLinkToken().filter(rec)
    assert tok not in rec.getMessage() and "/r/[redacted]" in rec.getMessage()


# ---------- roster / non-répondants ----------


async def test_student_roster_drives_non_respondents(pub, db) -> None:
    studs = await mk_students(db, 3)
    inc = await mk_incident(db)
    token = await issue(db, inc, studs[0])
    await pub.post(f"/r/{token}", data={"status": "safe_campus"})
    roster = StudentRoster(db)
    async with db() as s:
        data, missing = await responses.report(s, roster, inc.id)
    assert data["counts"]["responded"] == 1 and data["counts"]["not_responded"] == 2
    assert data["counts"]["roster_total"] == 3
    assert {m.aad_id for m in missing} == {str(s.id) for s in studs[1:]}
    assert data["respondents"][0]["aad_id"] == str(studs[0].id)
    assert "no_response" in responses.to_csv(data)
    m = (await roster.members())[0]
    assert isinstance(m, Member) and m.email


def test_roster_provider_switches_on_links_mode() -> None:
    assert isinstance(get_roster_provider(cfg()), StudentRoster)
    assert isinstance(get_roster_provider(cfg(alert_webhook_url="")), LogRoster)
    only_links = cfg(alert_webhook_url="", response_links=True)
    assert isinstance(get_roster_provider(only_links), StudentRoster)
    assert isinstance(get_roster_provider(cfg(alert_webhook_enabled=False)), LogRoster)


# ---------- webhook ----------


class Hook:
    def __init__(self, statuses: list[int] | None = None, fail_batches: set[int] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.statuses = list(statuses or [])
        self.fail_batches = fail_batches or set()

    def transport(self) -> httpx.MockTransport:
        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            self.calls.append(body)
            if self.statuses:
                return httpx.Response(self.statuses.pop(0))
            if body.get("batch") in self.fail_batches:
                return httpx.Response(400)
            return httpx.Response(202)

        return httpx.MockTransport(handler)


async def _nosleep(_: float) -> None:
    return None


def client_for(hook: Hook) -> HttpWebhook:
    return HttpWebhook(cfg(), hook.transport(), sleep=_nosleep)


async def test_dispatch_batches_cards_and_hashes(db) -> None:
    studs = await mk_students(db, 7)
    inc = await mk_incident(db, is_test=True)
    hook = Hook()
    async with db() as s:
        res = await webhook.dispatch_initial(s, cfg(), client_for(hook), inc)
    assert (res["recipients"], res["sent"], res["failed"], res["batches"]) == (7, 7, 0, 3)
    assert [len(c["recipients"]) for c in hook.calls] == [3, 3, 1]
    assert hook.calls[0]["incident"] == {
        "id": str(inc.id),
        "title": inc.title,
        "message": inc.message,
        "type": "incendie",
        "is_test": True,
    }
    urls = set()
    for call in hook.calls:
        for rcp in call["recipients"]:
            card = rcp["card"]
            assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
            assert [a["type"] for a in card["actions"]] == ["Action.OpenUrl"] * 3
            assert {a["url"] for a in card["actions"]} == {rcp["url"]}
            assert len(json.dumps(card).encode()) < 28_000
            assert "TEST" in card["body"][0]["text"]
            urls.add(rcp["url"])
    assert len(urls) == 7 and all(u.startswith("https://sound.test/r/") for u in urls)
    assert {rc["email"] for c in hook.calls for rc in c["recipients"]} == {s.email for s in studs}
    async with db() as s:
        hashes = set(await s.scalars(select(ResponseLink.token_hash)))
    assert hashes == {hashlib.sha256(u.rsplit("/", 1)[1].encode()).hexdigest() for u in urls}


async def test_retries_bounded_and_secret_not_logged(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    hook = Hook(statuses=[500, 429, 200])
    await client_for(hook).post({"x": 1})
    assert len(hook.calls) == 3
    hook = Hook(statuses=[500, 500, 500, 500])
    with pytest.raises(WebhookError) as ei:
        await client_for(hook).post({"x": 1})
    assert len(hook.calls) == 3 and "SECRETSIG" not in str(ei.value)
    hook = Hook(statuses=[404, 200])
    with pytest.raises(WebhookError):
        await client_for(hook).post({"x": 1})
    assert len(hook.calls) == 1  # 4xx définitif : pas de nouvelle tentative

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cannot reach " + str(req.url))

    with pytest.raises(WebhookError) as ei:
        await HttpWebhook(cfg(), httpx.MockTransport(boom), sleep=_nosleep).post({})
    assert "SECRETSIG" not in str(ei.value) and "SECRETSIG" not in caplog.text


def test_card_stays_under_limit_with_huge_message() -> None:
    s = cfg(alert_webhook_card_max_bytes=2_000)
    card = webhook.build_card(s, "T", "é" * 10_000, False, "https://sound.test/r/" + "a" * 43)
    assert len(json.dumps(card, ensure_ascii=False).encode()) <= 2_000 + 400
    rem = webhook.build_card(s, "T", "m", False, "u", reminder=True)
    assert rem["body"][0]["text"].startswith("RAPPEL")


async def test_partial_failure_removes_orphan_links(db) -> None:
    await mk_students(db, 6)
    inc = await mk_incident(db)
    hook = Hook(fail_batches={2})
    async with db() as s:
        res = await webhook.dispatch_initial(s, cfg(), client_for(hook), inc)
    assert (res["sent"], res["failed"]) == (3, 3)
    assert await count(db, ResponseLink) == 3


async def test_tick_sends_once_and_is_idempotent(db) -> None:
    await mk_students(db, 4)
    inc = await mk_incident(db)
    old = await mk_incident(db, sent_ago=7200)  # trop ancien
    draft = await mk_incident(db, status="draft")
    hook = Hook()
    c = client_for(hook)
    assert await webhook.tick(db, cfg(), c) == 1
    assert await webhook.tick(db, cfg(), c) == 0
    assert len(hook.calls) == 2  # 4 destinataires / lots de 3
    assert await count(db, ResponseLink, ResponseLink.incident_id == inc.id) == 4
    assert await count(db, ResponseLink, ResponseLink.incident_id.in_([old.id, draft.id])) == 0
    async with db() as s:
        n = await s.scalar(
            select(IncidentNotification).where(IncidentNotification.incident_id == inc.id)
        )
        assert n.delivered_at is not None and n.kind == "webhook"
        a = await s.scalar(select(AuditLog).where(AuditLog.action == "webhook_dispatch"))
        assert a.detail["sent"] == 4 and "SECRETSIG" not in json.dumps(a.detail)


async def test_tick_retries_when_nothing_sent_and_is_inert_unconfigured(db) -> None:
    await mk_students(db, 2)
    await mk_incident(db)
    assert await webhook.tick(db, cfg(), client_for(Hook(statuses=[400] * 50))) == 0
    assert await count(db, ResponseLink) == 0  # liens orphelins nettoyés
    async with db() as s:  # le retry est espacé : on recule la dernière tentative
        await s.execute(
            text("update incident_notifications set last_attempt_at = now() - interval '5 min'")
        )
        await s.commit()
    ok = Hook()
    assert await webhook.tick(db, cfg(), client_for(ok)) == 1 and ok.calls
    off = cfg(alert_webhook_url="")
    assert await webhook.tick(db, off, webhook.get_webhook(off)) == 0


def test_webhook_is_outside_the_tts_path() -> None:
    """Le canal webhook n'est pas appelé par service.send : TTS et priorités inchangés."""
    assert "webhook" not in inspect.getsource(service)


# ---------- relances ----------


async def test_manual_remind_targets_only_missing(pub, db, make_user, login_as, redis) -> None:
    studs = await mk_students(db, 4)
    inc = await mk_incident(db)
    tok = await issue(db, inc, studs[0])
    await pub.post(f"/r/{tok}", data={"status": "safe_campus"})
    hook = Hook()
    app.dependency_overrides[alerts_router.get_webhook] = lambda: client_for(hook)
    app.dependency_overrides[alerts_router.get_roster] = lambda: StudentRoster(db)
    app.dependency_overrides[get_settings] = lambda: cfg(remind_cooldown=300)
    app.dependency_overrides[alerts_router.get_teams_notifier] = lambda: FakeTeams()
    await login_as(await make_user(Role.staff))
    await redis.delete(f"remind:cooldown:{inc.id}")
    r = await pub.post(f"/incidents/{inc.id}/remind")
    assert r.status_code == 200 and r.json()["reminded"] == 3
    sent = [rc for c in hook.calls for rc in c["recipients"]]
    assert {rc["email"] for rc in sent} == {s.email for s in studs[1:]}
    assert all(c["kind"] == "reminder" for c in hook.calls)
    assert all(rc["card"]["body"][0]["text"].startswith("RAPPEL") for rc in sent)
    assert (await pub.post(f"/incidents/{inc.id}/remind")).status_code == 429  # cooldown
    await redis.delete(f"remind:cooldown:{inc.id}")


async def test_auto_remind_uses_webhook_in_links_mode(db, redis) -> None:
    studs = await mk_students(db, 3)
    inc = await mk_incident(db, sent_ago=700)
    async with db() as s:
        s.add(
            Response(
                id=uuid.uuid4(), incident_id=inc.id, user_aad_id=str(studs[0].id), status="danger"
            )
        )
        await s.commit()
    hook, teams = Hook(), FakeTeams()
    await redis.delete(f"remind:cooldown:{inc.id}", f"autoremind:backoff:{inc.id}")
    n = await autoremind.tick(db, redis, cfg(), StudentRoster(db), teams, wh=client_for(hook))
    assert n == 1
    got = {rc["email"] for c in hook.calls for rc in c["recipients"]}
    assert got == {s.email for s in studs[1:]}
    assert not getattr(teams, "reminded", [])  # pas de mentions Teams en mode liens
    await redis.delete(f"remind:cooldown:{inc.id}")


async def test_remind_without_webhook_is_log_mode(db) -> None:
    studs = await mk_students(db, 2)
    inc = await mk_incident(db)
    notice = webhook.IncidentNotice(str(inc.id), inc.title, inc.message, False)
    members = [Member(str(s.id), s.email, "x") for s in studs]
    off = cfg(alert_webhook_url="")
    async with db() as s:
        out = await webhook.remind(s, off, webhook.LogWebhook(), notice, members)
    assert out["skipped"] and await count(db, ResponseLink) == 0


# ---------- UI admin ----------


async def test_admin_can_generate_student_link(pub, db, make_user, login_as) -> None:
    (st,) = await mk_students(db, 1)
    inc = await mk_incident(db)
    admin = await make_user(Role.admin)
    await login_as(admin)
    r = await pub.post(f"/incidents/{inc.id}/links", json={"email": st.email.upper()})
    assert r.status_code == 200
    token = r.json()["url"].rsplit("/", 1)[1]
    assert (await pub.get(f"/r/{token}")).status_code == 200
    unknown = await pub.post(f"/incidents/{inc.id}/links", json={"email": "no@x.eu"})
    assert unknown.status_code == 404
    assert "response_link_create" in await audit_actions(db, admin.email)
    async with db() as s:
        row = await s.scalar(select(AuditLog).where(AuditLog.action == "response_link_create"))
    assert token not in json.dumps(row.detail)


async def test_staff_cannot_generate_link(pub, db, make_user, login_as) -> None:
    (st,) = await mk_students(db, 1)
    inc = await mk_incident(db)
    await login_as(await make_user(Role.staff))
    r = await pub.post(f"/incidents/{inc.id}/links", json={"email": st.email})
    assert r.status_code == 403


async def test_dashboard_shows_link_channel(pub, db, make_user, login_as) -> None:
    studs = await mk_students(db, 2)
    inc = await mk_incident(db)
    await issue(db, inc, studs[0])
    await login_as(await make_user(Role.admin))
    r = await pub.get(f"/alerts/{inc.id}")
    assert r.status_code == 200 and "Liens de réponse individuels" in r.text
    assert "actif" in r.text and "Générer un lien" in r.text
    r = await pub.post(
        f"/ui/alerts/{inc.id}/links",
        content=f"email={studs[1].email}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200 and "data-copy=" in r.text and "/r/" in r.text


async def test_dashboard_hidden_without_links_mode(client, db, make_user, login_as) -> None:
    app.dependency_overrides[get_settings] = lambda: cfg(alert_webhook_url="")
    inc = await mk_incident(db)
    await login_as(await make_user(Role.admin))
    r = await client.get(f"/alerts/{inc.id}")
    assert r.status_code == 200 and "Liens de réponse individuels" not in r.text
