# ruff: noqa: F811
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import Settings
from app.ha import queue, service
from app.ha.client import HAClient, HARequestError, HAServerError
from app.ha.ssml import build_ssml, validate_voice_lang
from app.models import Announcement, AuditLog, Priority, Role
from tests.helpers import audit_actions
from tests.test_ha import _eid, _speaker, ha_api, mock_ha, redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_db")


# --- SSML / liste blanche ---------------------------------------------------


def test_ssml_escapes_user_text() -> None:
    evil = 'a < b & c </speak><voice name="X">pwn'
    out = build_ssml(evil, "Celine", "fr-FR")
    assert out == (
        '<speak><voice name="Celine"><lang xml:lang="fr-FR">'
        'a &lt; b &amp; c &lt;/speak&gt;&lt;voice name="X"&gt;pwn'
        "</lang></voice></speak>"
    )
    assert out.count("<speak>") == 1 and out.count("</speak>") == 1
    ctl = build_ssml("a\x00b\x0bc", "Lea", None)
    assert ctl is not None and "\x00" not in ctl and "\x0b" not in ctl


def test_ssml_prefix_outside_voice_and_optional() -> None:
    out = build_ssml("salut", "Hans", "de-DE", prefix="Ceci est un test.")
    assert out == (
        '<speak>Ceci est un test. <voice name="Hans"><lang xml:lang="de-DE">salut</lang>'
        "</voice></speak>"
    )
    assert build_ssml("x", None, None, prefix="T") is None
    assert build_ssml("x", None, "en-GB") == '<speak><lang xml:lang="en-GB">x</lang></speak>'


def test_whitelist_validation() -> None:
    s = Settings()
    assert validate_voice_lang(s, "Celine", "fr-FR") == ("Celine", "fr-FR")
    assert validate_voice_lang(s, None, None) == (None, None)
    for v, lang in (("celine", None), ('Evil"/>', None), (None, "xx-XX"), (None, "fr-fr")):
        with pytest.raises(HTTPException) as ei:
            validate_voice_lang(s, v, lang)
        assert ei.value.status_code == 422


def test_alert_ignores_voice() -> None:
    a = service.build_announcement(
        Settings(),
        text="Feu",
        speakers=[],
        is_test=False,
        priority=Priority.alert,
        author=None,
        voice="Celine",
        lang="fr-FR",
    )
    assert a.voice is None and a.lang is None and a.ssml is None


# --- repli texte brut -------------------------------------------------------


def _client(handler: Any) -> HAClient:
    return HAClient(
        "http://h",
        "t",
        "notify.alexa_media",
        retries=1,
        backoff=0,
        transport=httpx.MockTransport(handler),
    )


async def test_ssml_fallback_single_attempt(caplog: pytest.LogCaptureFixture) -> None:
    msgs: list[str] = []

    def refuse_ssml(req: httpx.Request) -> httpx.Response:
        m = json.loads(req.content)["message"]
        msgs.append(m)
        return httpx.Response(400 if m.startswith("<speak>") else 200)

    with caplog.at_level("WARNING", logger="sound.ha"):
        await _client(refuse_ssml).speak("media_player.a", "plain", None, "<speak>plain</speak>")
    assert msgs == ["<speak>plain</speak>", "plain"]
    assert any("plain text fallback" in r.message for r in caplog.records)

    msgs.clear()

    def refuse_all(_: httpx.Request) -> httpx.Response:
        msgs.append("x")
        return httpx.Response(400)

    with pytest.raises(HARequestError):  # le repli échoue aussi : pas de 3e tentative
        await _client(refuse_all).speak("media_player.a", "plain", None, "<speak>plain</speak>")
    assert len(msgs) == 2


async def test_server_error_no_fallback() -> None:
    seen: list[str] = []

    def boom(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content)["message"])
        return httpx.Response(500)

    with pytest.raises(HAServerError):
        await _client(boom).speak("media_player.a", "p", None, "<speak>p</speak>")
    assert set(seen) == {"<speak>p</speak>"}  # retries sur SSML, jamais de repli sur 5xx


# --- API : envoi, défauts, prévisualisation ---------------------------------


async def test_post_voice_validation_and_send(ha_api, make_user, login_as, db, redis) -> None:
    await login_as(await make_user(Role.admin))
    sid = await _speaker(ha_api, _eid())
    base = {"text": "Bonjour <tout> & le monde", "speaker_ids": [sid]}
    for bad in ({"voice": "Nope"}, {"lang": "zz-ZZ"}):
        assert (await ha_api.post("/announcements", json={**base, **bad})).status_code == 422
    r = await ha_api.post("/announcements", json={**base, "voice": "Celine", "lang": "fr-FR"})
    assert r.status_code == 202 and r.json()["voice"] == "Celine"
    ha, calls = mock_ha()
    assert await service.process_one(db, redis, ha) is True
    msg = calls[0][2]["message"]
    assert msg.startswith('<speak><voice name="Celine"><lang xml:lang="fr-FR">')
    assert "Bonjour &lt;tout&gt; &amp; le monde" in msg


async def test_admin_defaults_applied(ha_api, make_user, login_as, db) -> None:
    await login_as(await make_user(Role.staff))
    assert (await ha_api.put("/announcements/defaults", json={"voice": "Hans"})).status_code == 403
    assert (await ha_api.get("/announcements/defaults")).status_code == 200
    admin = await make_user(Role.admin)
    await login_as(admin)
    assert (await ha_api.put("/announcements/defaults", json={"voice": "x"})).status_code == 422
    r = await ha_api.put("/announcements/defaults", json={"voice": "Hans", "lang": "de-DE"})
    assert r.status_code == 200
    assert "announce_defaults_update" in await audit_actions(db, admin.email)
    sid = await _speaker(ha_api, _eid())
    body = {"text": "hallo", "speaker_ids": [sid]}
    a = (await ha_api.post("/announcements", json=body)).json()
    assert (a["voice"], a["lang"]) == ("Hans", "de-DE")
    b = (await ha_api.post("/announcements", json={**body, "voice": "Lea"})).json()
    assert (b["voice"], b["lang"]) == ("Lea", "de-DE")
    await ha_api.put("/announcements/defaults", json={"voice": None, "lang": None})
    c = (await ha_api.post("/announcements", json=body)).json()
    assert c["voice"] is None and c["lang"] is None


async def _counts(db: Any, redis: Any) -> tuple[int, int, int]:
    async with db() as s:
        n = await s.scalar(select(func.count(Announcement.id)))
        a = await s.scalar(select(func.count(AuditLog.id)))
    return n, a, await redis.zcard(queue.QUEUE_KEY)


async def test_preview_no_side_effects(ha_api, make_user, login_as, db, redis) -> None:
    await login_as(await make_user(Role.admin))
    ok = await _speaker(ha_api, _eid(), notify_service="notify.alexa_media_a")
    no_notify = await _speaker(ha_api, _eid())
    off = await _speaker(ha_api, _eid(), is_active=False)
    before = await _counts(db, redis)
    body = {
        "text": "Bonjour <b>",
        "speaker_ids": [ok, no_notify, off],
        "is_test": True,
        "voice": "Celine",
        "lang": "fr-FR",
    }
    r = await ha_api.post("/announcements/preview", json=body)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["text"] == "Ceci est un test. Bonjour <b>"
    assert (p["voice"], p["lang"]) == ("Celine", "fr-FR")
    assert {s["id"] for s in p["speakers"]} == {ok, no_notify}
    assert [i["id"] for i in p["ignored"]] == [off]
    assert p["estimated_seconds"] >= 1
    assert any("notify_service" in w for w in p["warnings"])
    assert p["ssml"].startswith("<speak>Ceci est un test. <voice")
    assert "&lt;b&gt;" in p["ssml"]
    assert await _counts(db, redis) == before


async def test_preview_warnings_and_errors(ha_api, make_user, login_as) -> None:
    await login_as(await make_user(Role.admin))
    sid = await _speaker(ha_api, _eid())
    await login_as(await make_user(Role.staff))
    long = {"text": "x" * 501, "speaker_ids": [sid]}
    r = await ha_api.post("/announcements/preview", json=long)
    assert r.status_code == 200 and any("trop long" in w for w in r.json()["warnings"])
    assert (await ha_api.post("/announcements", json=long)).status_code == 422
    bad = {"text": "x", "speaker_ids": [sid], "voice": "Nope"}
    assert (await ha_api.post("/announcements/preview", json=bad)).status_code == 422
    unk = {"text": "x", "speaker_ids": [str(uuid.uuid4())]}
    assert (await ha_api.post("/announcements/preview", json=unk)).status_code == 404


async def test_preview_requires_login(ha_api) -> None:
    body = {"text": "x", "speaker_ids": [str(uuid.uuid4())]}
    assert (await ha_api.post("/announcements/preview", json=body)).status_code == 401


# --- historique -------------------------------------------------------------


async def _seed(db: Any, **kw: Any) -> Announcement:
    a = Announcement(
        id=uuid.uuid4(),
        author_email=kw.pop("author", "a@epitech.eu"),
        text=kw.pop("text", "t"),
        priority=2,
        is_test=kw.pop("is_test", False),
        status=kw.pop("status", "done"),
        targets=kw.pop("targets", []),
        results=kw.pop("results", None),
        created_at=kw.pop("created_at", datetime.now(UTC)),
    )
    async with db() as s:
        s.add(a)
        await s.commit()
    return a


async def test_history_filters_and_rights(ha_api, make_user, login_as, db) -> None:
    tag = uuid.uuid4().hex[:8]
    await login_as(await make_user(Role.admin))
    sid = await _speaker(ha_api, _eid())
    g = await ha_api.post("/speaker-groups", json={"name": f"g{tag}", "member_ids": [sid]})
    gid = g.json()["id"]
    other = str(uuid.uuid4())
    old = datetime.now(UTC) - timedelta(days=30)
    t = {"id": sid, "entity_id": "media_player.x", "name": "X"}
    o = {"id": other, "entity_id": "media_player.o", "name": "O"}
    bob, al = f"{tag}-bob@epitech.eu", f"{tag}-al@epitech.eu"
    await _seed(db, text=f"{tag} alpha", author=bob, targets=[t])
    await _seed(db, text=f"{tag} 100% sûr_", is_test=True, status="failed", author=al, targets=[o])
    await _seed(db, text=f"{tag} vieux", created_at=old, author=bob)
    await login_as(await make_user(Role.admin))

    async def texts(**p: Any) -> list[str]:
        r = await ha_api.get("/announcements", params={"q": tag, **p})
        assert r.status_code == 200, r.text
        return [a["text"] for a in r.json()]

    assert len(await texts()) == 3
    assert len(await texts(is_test="true")) == 1 and len(await texts(is_test="false")) == 2
    assert len(await texts(status="failed")) == 1
    assert len(await texts(author="bob")) == 2
    assert len(await texts(speaker_id=sid)) == 1
    assert len(await texts(group_id=gid)) == 1
    assert len(await texts(speaker_id=other)) == 1
    day = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    assert len(await texts(date_from=day)) == 2
    assert len(await texts(date_to=day)) == 1
    assert len(await texts(date_from="", status="", is_test="")) == 3  # vides = pas de filtre
    r = await ha_api.get("/announcements", params={"q": f"{tag} 100%"})
    assert [a["text"] for a in r.json()] == [f"{tag} 100% sûr_"]  # % littéral, pas joker
    assert (await ha_api.get("/announcements", params={"q": "%"})).status_code == 200
    r = await ha_api.get("/announcements", params={"q": tag, "limit": 1, "offset": 1})
    assert len(r.json()) == 1 and r.headers["x-total-count"] == "3"
    inj = await ha_api.get(
        "/announcements", params={"q": "'; DROP TABLE announcements;--", "author": "' OR 1=1--"}
    )
    assert inj.status_code == 200 and inj.json() == []
    bads = (
        {"status": "nope"},
        {"date_from": "2026-13-01"},
        {"is_test": "peut-etre"},
        {"speaker_id": "abc"},
        {"date_from": "2026-02-02", "date_to": "2026-01-01"},
        {"limit": 500},
    )
    for bad in bads:
        assert (await ha_api.get("/announcements", params=bad)).status_code == 422
    assert (await ha_api.get("/announcements", params={"group_id": other})).status_code == 404


async def test_history_requires_login(ha_api) -> None:
    assert (await ha_api.get("/announcements")).status_code == 401


# --- UI ---------------------------------------------------------------------


async def test_ui_pages(ha_api, make_user, login_as, db) -> None:
    await login_as(await make_user(Role.admin))
    sid = await _speaker(ha_api, _eid())
    t = {"id": sid, "entity_id": "media_player.z", "name": "Zed"}
    a = await _seed(db, text="ui <i>x</i>", targets=[t])
    a2 = await _seed(db, status="failed", results={"_error": "interrompue"})
    page = await ha_api.get("/announce")
    assert page.status_code == 200
    assert 'id="preview-btn"' in page.text and 'name="voice"' in page.text
    h = await ha_api.get("/ui/announce/history", params={"q": "ui <i>", "page": 1, "status": ""})
    assert h.status_code == 200
    assert "ui &lt;i&gt;x&lt;/i&gt;" in h.text and "<i>x" not in h.text
    d = await ha_api.get(f"/ui/announce/{a.id}")
    assert d.status_code == 200 and "Zed" in d.text and "en attente" in d.text.lower()
    assert "interrompue" in (await ha_api.get(f"/ui/announce/{a2.id}")).text
    assert (await ha_api.get("/ui/announce/history", params={"status": "zz"})).status_code == 422
    assert (await ha_api.get("/admin/annonces")).text.count("Voix des annonces") == 1


# --- style de diction -------------------------------------------------------

_STYLE_SSML = {
    "whisper": ('<amazon:effect name="whispered">', "</amazon:effect>"),
    "slow": ('<prosody rate="slow">', "</prosody>"),
    "fast": ('<prosody rate="fast">', "</prosody>"),
    "soft": ('<prosody volume="soft">', "</prosody>"),
    "loud": ('<prosody volume="loud">', "</prosody>"),
    "low": ('<prosody pitch="low">', "</prosody>"),
    "high": ('<prosody pitch="high">', "</prosody>"),
    "emphasis": ('<emphasis level="strong">', "</emphasis>"),
}


def test_style_ssml_nesting_and_escape() -> None:
    from app.ha.ssml import STYLES

    assert set(STYLES) == set(_STYLE_SSML)
    for key, (o, c) in _STYLE_SSML.items():
        assert build_ssml("a<b", None, None, style=key) == f"<speak>{o}a&lt;b{c}</speak>"
    out = build_ssml("x", "Celine", "fr-FR", prefix="TEST", style="slow")
    assert out == (
        '<speak>TEST <voice name="Celine"><lang xml:lang="fr-FR">'
        '<prosody rate="slow">x</prosody></lang></voice></speak>'
    )
    assert build_ssml("x", None, None) is None


def test_style_validation_and_alert() -> None:
    from app.ha.ssml import validate_style

    assert validate_style(None) is None and validate_style("fast") == "fast"
    with pytest.raises(HTTPException) as ei:
        validate_style("shout")
    assert ei.value.status_code == 422
    a = service.build_announcement(
        Settings(),
        text="Feu",
        speakers=[],
        is_test=False,
        priority=Priority.alert,
        author=None,
        style="loud",
    )
    assert a.style is None and a.ssml is None


async def test_style_api_preview_send_history(ha_api, make_user, login_as, db, redis) -> None:
    await login_as(await make_user(Role.admin))
    sid = await _speaker(ha_api, _eid(), notify_service="notify.alexa_media_a")
    base = {"text": "Bonjour", "speaker_ids": [sid]}
    for url in ("/announcements", "/announcements/preview"):
        r = await ha_api.post(url, json={**base, "style": "shout"})
        assert r.status_code == 422
    p = (await ha_api.post("/announcements/preview", json={**base, "style": "whisper"})).json()
    assert p["style"] == "whisper" and p["style_label"] == "Chuchoté"
    assert p["ssml"] == '<speak><amazon:effect name="whispered">Bonjour</amazon:effect></speak>'
    r = await ha_api.post("/announcements", json={**base, "style": "whisper"})
    assert r.status_code == 202 and r.json()["style"] == "whisper"
    ha, calls = mock_ha()
    assert await service.process_one(db, redis, ha) is True
    assert "whispered" in calls[0][2]["message"]
    async with db() as s:
        row = (await s.scalars(select(Announcement))).all()[-1]
    assert row.style == "whisper"
