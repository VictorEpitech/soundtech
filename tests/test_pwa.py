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
    assert "fab-alert" not in login
    for role in (Role.staff, Role.admin):
        await login_as(await make_user(role))
        home = (await client.get("/")).text
        assert 'class="fab-alert" href="/alerts#alert-grid"' in home and "aria-label" in home
        assert "fab-alert" not in (await client.get("/alerts")).text
        client.cookies.clear()


async def test_no_client_side_alert_blocking(client) -> None:
    css = (await client.get("/static/app.css")).text
    js = (await client.get("/static/pwa.js")).text
    assert "is-offline #ad-send" not in css and "is-offline .fab-alert" not in css
    assert "disabled" not in js and "pointer-events" not in js
    assert "no-store" in js
