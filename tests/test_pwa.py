import json
import re
from pathlib import Path

import pytest

from app.models import Role
from app.ui import pwa

ROOT = Path(__file__).resolve().parent.parent / "app"


async def test_manifest(client) -> None:
    r = await client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(r.text)
    assert m["name"] == "Sound" and m["short_name"] and m["start_url"] == "/"
    assert m["display"] == "standalone" and m["theme_color"] and m["background_color"]
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert {
        ("192x192", "any"),
        ("512x512", "any"),
        ("192x192", "maskable"),
        ("512x512", "maskable"),
    } <= sizes
    for icon in m["icons"]:
        assert (ROOT / icon["src"].lstrip("/")).is_file()
        assert (await client.get(icon["src"])).status_code == 200
    assert any(s["url"] == "/alerts" for s in m["shortcuts"])


async def test_service_worker(client) -> None:
    r = await client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "no-cache" in r.headers["cache-control"]
    assert r.headers["service-worker-allowed"] == "/"
    assert "__VERSION__" not in r.text and "__ASSETS__" not in r.text
    m = re.search(r"const ASSETS = (\[.*?\]);", r.text)
    assert m
    assets = json.loads(m.group(1))
    assert "/static/app.css" in assets
    assert all(a.startswith("/static/") and ".." not in a for a in assets)
    for forbidden in ("/incidents", "/alerts", "/announcements", "/auth", "/ui/", "/music"):
        assert not any(a.startswith(forbidden) for a in assets)
    assert 'const OFFLINE_URL = "/offline"' in r.text
    assert "sound-shell-" in r.text


def test_sw_version_format() -> None:
    assert re.fullmatch(r"[0-9a-f]{12}", pwa.shell_version())


async def test_offline_public(client) -> None:
    r = await client.get("/offline")
    assert r.status_code == 200
    assert "Hors ligne : les alertes et annonces ne peuvent pas être envoyées" in r.text
    assert "sirène" in r.text and "téléphone" in r.text
    assert "fab-alert" not in r.text


@pytest.mark.usefixtures("migrated_db")
async def test_head_tags_and_fab(client, make_user, login_as) -> None:
    login = (await client.get("/login")).text
    assert 'rel="manifest"' in login and "apple-touch-icon" in login
    assert 'name="theme-color"' in login and "serviceWorker" in login
    assert "fab-alert" not in login and "data-alert-open" not in login
    for role in (Role.staff, Role.admin):
        await login_as(await make_user(role))
        home = (await client.get("/")).text
        assert "fab-alert" not in home
        assert 'class="nav-alert" href="/alerts" data-alert-open' in home
        assert 'id="alert-dialog"' in home and 'id="incident-banner"' in home
        assert "/static/alerts.js" in home
        assert "data-alert-open" in (await client.get("/alerts")).text
        client.cookies.clear()


async def test_no_client_side_alert_blocking(client) -> None:
    css = (await client.get("/static/app.css")).text
    js = (await client.get("/static/pwa.js")).text
    assert "is-offline #ad-send" not in css and "is-offline .nav-alert" not in css
    assert "disabled" not in js and "pointer-events" not in js
    assert "no-store" in js


def test_fonts_self_hosted_and_precached() -> None:
    css = (ROOT / "static/fonts.css").read_text()
    urls = re.findall(r"url\((/static/fonts/[^)]+)\)", css)
    assert urls and all((ROOT / u.lstrip("/")).is_file() for u in urls)
    assert set(urls) <= set(pwa.shell_assets())
    assert "/static/fonts.css" in pwa.shell_assets()
    assert (ROOT / "static/fonts/LICENSES.md").is_file()


async def test_no_external_font_hosts(client) -> None:
    html = (await client.get("/offline")).text
    assert "googleapis" not in html and "gstatic" not in html
    assert "/static/fonts.css" in html


async def test_icons_hidden_and_status_labels(client, make_user, login_as) -> None:
    await login_as(await make_user(Role.staff))
    html = (await client.get("/alerts")).text
    assert not re.search(r'<span class="icon[^"]*">', html)
    assert 'role="switch"' in html and 'aria-describedby="mode-state"' in html


@pytest.mark.usefixtures("migrated_db")
async def test_alert_sheet_and_banner_fragments(client, make_user, login_as, db) -> None:
    from app.models import Incident

    anon = await client.get("/ui/alerts/sheet", headers={"HX-Request": "true"})
    assert anon.status_code == 401
    await login_as(await make_user(Role.staff))
    sheet = (await client.get("/ui/alerts/sheet")).text
    assert 'id="alert-sheet"' in sheet and 'role="switch"' in sheet
    assert not re.search(r'<span class="icon[^"]*">', sheet)
    assert (await client.get("/ui/alerts/active")).text.strip() == ""
    async with db() as s:
        s.add(Incident(type="incendie", title="Feu <b>", message="m", status="sent", is_test=False))
        await s.commit()
    banner = (await client.get("/ui/alerts/active")).text
    assert "Incident en cours" in banner and 'href="/alerts/' in banner and "<b>" not in banner


@pytest.mark.usefixtures("migrated_db")
async def test_confirm_word_still_required_server_side(client, make_user, login_as) -> None:
    await login_as(await make_user(Role.staff))
    r = await client.post("/incidents/from-template", json={"type": "incendie", "confirm": "x"})
    assert r.status_code == 422


def test_alert_js_hold_contract() -> None:
    js = (ROOT / "static/alerts.js").read_text()
    assert "HOLD_MS = 1200" in js and "if (sending || done" in js
    assert "confirm: word()" in js


def test_alert_js_409_is_not_reported_as_failure() -> None:
    js = (ROOT / "static/alerts.js").read_text()
    assert "r.status === 409" in js and "vient peut-être de partir" in js
    block = js[js.index("r.status === 409") : js.index("} else {", js.index("r.status === 409"))]
    assert "NON envoyée" not in block and "recentIncident" in block


def test_design_system_contract() -> None:
    js = (ROOT / "static/app.js").read_text()
    css = (ROOT / "static/app.css").read_text()
    assert "htmx:confirm" in js and "confirmDialog" in js and "MAX_TOASTS = 3" in js
    assert "aria-busy" in js and "#alert-dialog" in js and "fieldErrors" in js
    assert css.count(".btn-danger {") == 1 and ".btn-filled.btn-danger" not in css
    assert ".skeleton" in css and ".toast-stack" in css
    for name in ("schedule_runs", "history", "incidents"):
        html = (ROOT / f"templates/partials/{name}.html").read_text()
        assert html.count("<th>") <= html.count("data-label") + 1, name
    macros = (ROOT / "templates/_macros.html").read_text()
    assert "macro empty(" in macros
    assert "btn-danger" not in (ROOT / "templates/base.html").read_text()
