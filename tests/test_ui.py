import uuid
from typing import Any

import pytest

from app.models import Preset, Role, Speaker
from app.ui import router as ui_router

pytestmark = pytest.mark.usefixtures("migrated_db")

PAGES = ["/", "/music", "/announce", "/admin"]
FRAGMENTS = [
    "/ui/music/players",
    "/ui/music/search?q=x",
    "/ui/announce/history",
    "/ui/admin/users",
    "/ui/admin/speakers",
    "/ui/admin/presets",
]


class FakeMA:
    async def players(self) -> list[dict[str, Any]]:
        return [
            {"player_id": "p1", "display_name": "Salon", "playback_state": "playing",
             "volume_level": 30, "current_media": {"title": "Morceau <b>"}},
            {"player_id": "p2", "display_name": "Cuisine", "playback_state": "idle"},
        ]  # fmt: skip

    async def search(self, q: str, types: Any, limit: int) -> dict[str, Any]:
        return {"tracks": [{"uri": "lib://t/1", "name": f"Titre {q}", "artists": [{"name": "A"}]}]}


@pytest.fixture
def fake_ma(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_router.ma_api, "get_ma", lambda settings: FakeMA())


async def test_login_page_public(client) -> None:
    r = await client.get("/login")
    assert r.status_code == 200 and "/auth/login" in r.text
    assert (await client.get("/static/app.css")).status_code == 200
    assert (await client.get("/static/htmx.min.js")).status_code == 200


@pytest.mark.parametrize("url", PAGES + FRAGMENTS)
async def test_anonymous_redirected(client, url: str) -> None:
    r = await client.get(url)
    assert r.status_code == 302 and r.headers["location"] == "/login"


async def test_staff_access(client, make_user, login_as, fake_ma) -> None:
    await login_as(await make_user(Role.staff))
    for url in ["/", "/music", "/announce"]:
        assert (await client.get(url)).status_code == 200, url
    home = await client.get("/")
    assert 'href="/admin"' not in home.text
    for url in ["/admin", "/ui/admin/users", "/ui/admin/speakers", "/ui/admin/presets"]:
        assert (await client.get(url)).status_code == 403, url
    assert (await client.get("/login")).status_code == 302


async def test_admin_access(client, make_user, login_as, fake_ma) -> None:
    await login_as(await make_user(Role.admin))
    for url in PAGES + FRAGMENTS:
        assert (await client.get(url)).status_code == 200, url
    assert 'href="/admin"' in (await client.get("/")).text


async def _declare(db, **mapping: str | None) -> None:
    from sqlalchemy import update

    async with db() as s:
        await s.execute(update(Speaker).values(ma_player_id=None))
        for pid, name in mapping.items():
            s.add(
                Speaker(
                    entity_id=f"media_player.uim_{uuid.uuid4().hex[:8]}",
                    name=name,
                    ma_player_id=pid,
                )
            )
        await s.commit()


async def test_music_fragments(client, make_user, login_as, fake_ma, db) -> None:
    await _declare(db, p1="Grande salle", p2="Réfectoire")
    await login_as(await make_user(Role.staff))
    page = (await client.get("/music")).text
    assert "Grande salle" in page and "Réfectoire" in page and "/music/players/p1/pause" in page
    assert "Salon" not in page and "Cuisine" not in page
    assert "/music/players/p2/resume" in page and 'data-sel="p1"' in page
    assert "Morceau &lt;b&gt;" in page
    res = await client.get("/ui/music/search", params={"q": "abc"})
    assert "Titre abc" in res.text and 'data-uri="lib://t/1"' in res.text


async def test_music_only_declared_players(client, make_user, login_as, fake_ma, db) -> None:
    await _declare(db, p2="Réfectoire")
    await login_as(await make_user(Role.staff))
    page = (await client.get("/ui/music/players")).text
    assert "Réfectoire" in page and "/music/players/p1/" not in page


async def test_music_no_declared_points_to_admin(client, make_user, login_as, fake_ma, db) -> None:
    await _declare(db)
    await login_as(await make_user(Role.staff))
    page = (await client.get("/music")).text
    assert "Admin" in page and "ma_player_id" in page and "Salon" not in page


async def test_music_unconfigured_is_graceful(client, make_user, login_as, db) -> None:
    await _declare(db, p1="X")
    await login_as(await make_user(Role.staff))
    r = await client.get("/music")
    assert r.status_code == 200 and "indisponible" in r.text
    r = await client.get("/ui/music/search", params={"q": "abc"})
    assert r.status_code == 200 and "not configured" in r.text


async def test_announce_lists_active_only(client, make_user, login_as, db) -> None:
    async with db() as s:
        s.add_all(
            [
                Speaker(entity_id="media_player.ui_on", name="UI-Active", is_active=True),
                Speaker(entity_id="media_player.ui_off", name="UI-Inactive", is_active=False),
                Preset(title="UI-Preset", text="Bonjour", is_active=True),
                Preset(title="UI-PresetOff", text="x", is_active=False),
            ]
        )
        await s.commit()
    await login_as(await make_user(Role.staff))
    page = (await client.get("/announce")).text
    assert "UI-Active" in page and "UI-Preset" in page
    assert (
        "<span>UI-Inactive</span>" not in page and "UI-PresetOff" not in page
    )  # filtre historique : toutes
    assert 'name="is_test"' in page


async def test_admin_fragments_render_and_escape(client, make_user, login_as) -> None:
    admin = await make_user(Role.admin)
    await login_as(admin)
    r = await client.post("/presets", json={"title": "<script>alert(1)</script>", "text": "t"})
    assert r.status_code == 201
    r = await client.post(
        "/speakers",
        json={"entity_id": "media_player.ui_adm", "name": "Adm", "notify_service": "notify.x_y",
              "ma_player_id": "ma-1"},
    )  # fmt: skip
    assert r.status_code == 201
    presets = (await client.get("/ui/admin/presets")).text
    assert "<script>alert(1)</script>" not in presets and "&lt;script&gt;" in presets
    speakers = (await client.get("/ui/admin/speakers")).text
    assert "notify.x_y" in speakers and "ma-1" in speakers
    assert admin.email in (await client.get("/ui/admin/users")).text


async def test_hx_unauthenticated_gets_401_not_redirect(client) -> None:
    r = await client.get("/ui/alerts/list", headers={"HX-Request": "true"})
    assert r.status_code == 401
    r = await client.get("/ui/alerts/list")
    assert r.status_code == 302 and r.headers["location"] == "/login"


def test_incident_polling_does_not_replace_controls() -> None:
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "app/templates/alerts.html").read_text()
    assert "every 5s" not in html and 'id="alert-dialog"' in html
