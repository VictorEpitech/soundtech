# ruff: noqa: F811
import httpx
import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.ma import images
from app.ma.router import image_transport
from app.main import app
from app.models import Role
from tests.helpers import _settings
from tests.test_ma import (  # noqa: F401
    TOKEN,
    _ma_settings,
    declared,
    fake_ma,
    music_api,
)

pytestmark = pytest.mark.usefixtures("migrated_db")

SECRET = _settings().jwt_secret
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _signed(provider: str = "plex", path: str = "/p/1") -> dict[str, str]:
    return {"provider": provider, "path": path, "sig": images.sign(SECRET, provider, path)}


def _transport(handler):
    app.dependency_overrides[image_transport] = lambda: httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.pop(image_transport, None)


@pytest.fixture
async def staff(music_api: AsyncClient, make_user, login_as) -> AsyncClient:
    await login_as(await make_user(Role.admin))
    return music_api


# --- proxy image ---------------------------------------------------------------


async def test_image_proxy_ok_hides_token(staff) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    _transport(handler)
    r = await staff.get("/music/image", params=_signed())
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert "max-age" in r.headers["cache-control"]
    assert TOKEN not in r.text and TOKEN not in str(r.headers)
    req = seen[0]
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.url.path == "/imageproxy" and req.url.params["path"] == "/p/1"


@pytest.mark.parametrize(
    "params",
    [
        {"provider": "plex", "path": "http://evil.test/x.png", "sig": "0" * 32},
        {"provider": "plex", "path": "/p/1", "sig": "bad"},
        {"provider": "a b", "path": "/p/1", "sig": "0" * 32},
    ],
)
async def test_image_proxy_rejects_unsigned(staff, params) -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    _transport(handler)
    assert (await staff.get("/music/image", params=params)).status_code == 403
    assert not calls


async def test_image_proxy_external_url_cannot_be_forged(staff) -> None:
    """URL externe : servie seulement si signée par le serveur, et jamais côté client."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    _transport(handler)
    r = await staff.get("/music/image", params=_signed("http", "https://cdn.test/a.png"))
    assert r.status_code == 200
    assert seen[0].url.host == "127.0.0.1"  # toujours l'hôte MA


async def test_image_proxy_placeholder_cases(staff) -> None:
    big = b"x" * 2_100_000
    cases = [
        httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"}),
        httpx.Response(200, content=big, headers={"content-type": "image/jpeg"}),
        httpx.Response(404),
    ]
    for resp in cases:
        _transport(lambda req, resp=resp: resp)
        r = await staff.get("/music/image", params=_signed())
        assert r.status_code == 200 and r.content == images.PLACEHOLDER, resp
        assert r.headers["content-type"] == "image/svg+xml"


async def test_image_proxy_auth(
    client: AsyncClient, make_user, login_as, fake_ma, declared
) -> None:
    app.dependency_overrides[get_settings] = lambda: _ma_settings(fake_ma)
    assert (await client.get("/music/image", params=_signed())).status_code == 401


# --- file d'attente -----------------------------------------------------------


async def test_queue_listing(staff) -> None:
    r = await staff.get("/music/players/p1/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["current_index"] == 1
    (b,) = body["items"]  # l'élément passé (qa) n'est plus listé
    assert (
        b["id"] == "qb"
        and b["current"] is True
        and b["image"].startswith("/music/image?provider=plex")
    )
    assert body["elapsed"] == 42 and body["duration"] is not None
    assert "state" in body
    assert TOKEN not in r.text


async def test_queue_hides_finished(staff, fake_ma) -> None:
    fake_ma.qstate = "idle"
    fake_ma.elapsed = 120
    body = (await staff.get("/music/players/p1/queue")).json()
    assert body["items"] == []
    r = await staff.get("/ui/music/queue", params={"player_id": "p1"})
    assert "file d'attente est vide" in r.text.replace("&#39;", "'")
    fake_ma.elapsed = 10  # idle mais pas terminé : reste affiché
    assert len((await staff.get("/music/players/p1/queue")).json()["items"]) == 1


async def test_queue_fragment_now_playing(staff) -> None:
    r = await staff.get("/ui/music/queue", params={"player_id": "p1"})
    assert 'role="progressbar"' in r.text and 'data-elapsed="42"' in r.text
    assert 'id="now-playing"' in r.text


async def test_queue_filtered_on_declared_players(staff, fake_ma) -> None:
    n = len(fake_ma.cmds)
    for pid in ("p2", "p3"):
        base = f"/music/players/{pid}/queue"
        assert (await staff.get(base)).status_code == 403
        assert (await staff.post(f"{base}/clear")).status_code == 403
        assert (await staff.delete(f"{base}/items/qa")).status_code == 403
        assert (await staff.post(f"{base}/items/qa/move", json={"shift": 1})).status_code == 403
        assert (await staff.post(f"{base}/items/qa/play")).status_code == 403
        assert (await staff.get("/ui/music/queue", params={"player_id": pid})).status_code == 200
    assert len(fake_ma.cmds) == n  # rien n'a atteint MA


async def test_queue_ui_fragment(staff) -> None:
    r = await staff.get("/ui/music/queue", params={"player_id": "p1"})
    assert "Deux" in r.text and "is-current" in r.text and "2:00" in r.text
    r = await staff.get("/ui/music/queue", params={"player_id": "p3"})
    assert "Deux" not in r.text and "player not declared" in r.text


async def test_queue_actions(staff, fake_ma) -> None:
    base = "/music/players/p1/queue"
    assert (await staff.post(f"{base}/clear")).status_code == 204
    assert (await staff.delete(f"{base}/items/qa")).status_code == 204
    assert (await staff.post(f"{base}/items/qa/move", json={"shift": -1})).status_code == 204
    assert (await staff.post(f"{base}/items/qb/play")).status_code == 204
    assert (await staff.post(f"{base}/items/qa/move", json={"shift": 999})).status_code == 422
    assert (await staff.delete(f"{base}/items/a b")).status_code in (404, 422)
    assert ("player_queues/clear", {"queue_id": "p1"}) in fake_ma.cmds
    assert (
        "player_queues/delete_item",
        {"queue_id": "p1", "item_id_or_index": "qa"},
    ) in fake_ma.cmds
    assert (
        "player_queues/move_item",
        {"queue_id": "p1", "queue_item_id": "qa", "pos_shift": -1},
    ) in fake_ma.cmds
    assert (
        "player_queues/play_index",
        {"queue_id": "p1", "index": "qb"},
    ) in fake_ma.cmds


# --- enqueue -------------------------------------------------------------------


@pytest.mark.parametrize("opt", ["play", "next", "add", "replace"])
async def test_enqueue_modes(staff, fake_ma, opt) -> None:
    uri = "library://album/7"
    r = await staff.post("/music/players/p1/play", json={"uri": uri, "option": opt})
    assert r.status_code == 204
    assert (
        "player_queues/play_media",
        {"queue_id": "p1", "media": uri, "option": opt},
    ) in fake_ma.cmds


async def test_enqueue_invalid_and_undeclared(staff, fake_ma) -> None:
    assert (
        await staff.post("/music/players/p1/play", json={"uri": "lib://x", "option": "nuke"})
    ).status_code == 422
    n = len(fake_ma.cmds)
    r = await staff.post("/music/players/p2/play", json={"uri": "lib://x", "option": "add"})
    assert r.status_code == 403 and len(fake_ma.cmds) == n


async def test_search_fragment_grouped(staff) -> None:
    r = await staff.get("/ui/music/search", params={"q": "so"})
    assert "Titres" in r.text and "Song" in r.text and 'data-uri="lib://t/1"' in r.text
    assert (await staff.get("/ui/music/search", params={"q": "s"})).text.strip() == ""
