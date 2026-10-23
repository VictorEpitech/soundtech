import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import websockets
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete

from app.config import Settings, get_settings
from app.ha import service
from app.ha.router import get_redis
from app.ma import service as ma_service
from app.ma.client import MAAuthError, MAClient, MACommandError, MAConnectionError, ws_url
from app.main import app
from app.models import Announcement, Priority, Role, Speaker
from tests.helpers import _settings
from tests.test_ha import TEST_REDIS, _eid, mock_ha, redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_db")

TOKEN = "ma-secret"
PLAYERS = [
    {"player_id": "p1", "display_name": "Salon", "playback_state": "playing", "volume_level": 30},
    {"player_id": "p2", "display_name": "Cuisine", "playback_state": "idle", "volume_level": 10},
]


QUEUE_ITEMS = [
    {
        "queue_item_id": "qa",
        "name": "Un",
        "duration": 61,
        "media_item": {"artists": [{"name": "A"}]},
    },
    {
        "queue_item_id": "qb",
        "name": "Deux",
        "duration": 120,
        "image": {"provider": "plex", "path": "/p/1"},
    },
]


class FakeMA:
    def __init__(self, token: str | None = TOKEN) -> None:
        self.token = token
        self.cmds: list[tuple[str, dict[str, Any]]] = []
        self.fail: set[str] = set()
        self.url = ""

    async def handler(self, ws: Any) -> None:
        await ws.send(json.dumps({"server_version": "2.10.7", "schema_version": 65}))
        authed = self.token is None
        async for raw in ws:
            m = json.loads(raw)
            mid, cmd, args = m["message_id"], m["command"], m.get("args", {})
            if cmd == "auth":
                if args.get("token") == self.token:
                    authed = True
                    await ws.send(json.dumps({"message_id": mid, "result": {"user": "x"}}))
                else:
                    await ws.send(
                        json.dumps({"message_id": mid, "error_code": 21, "details": "bad"})
                    )
                continue
            if not authed:
                await ws.send(
                    json.dumps(
                        {
                            "message_id": mid,
                            "error_code": 20,
                            "details": "Authentication is required.",
                        }
                    )
                )
                continue
            await ws.send(json.dumps({"event": "noise", "data": {}}))
            self.cmds.append((cmd, args))
            if cmd in self.fail:
                await ws.send(json.dumps({"message_id": mid, "error_code": 999, "details": "no"}))
            elif cmd == "players/all":
                await ws.send(json.dumps({"message_id": mid, "result": PLAYERS}))
            elif cmd == "player_queues/get":
                await ws.send(json.dumps({"message_id": mid, "result": {"current_index": 1}}))
            elif cmd == "player_queues/items":
                await ws.send(json.dumps({"message_id": mid, "result": QUEUE_ITEMS}))
            elif cmd == "music/search":
                res = {
                    "tracks": [
                        {
                            "uri": "lib://t/1",
                            "name": "Song",
                            "media_type": "track",
                            "artists": [{"name": "A"}],
                        }
                    ],
                    "artists": [],
                }
                await ws.send(json.dumps({"message_id": mid, "result": res}))
            else:
                await ws.send(json.dumps({"message_id": mid, "result": None}))


@pytest_asyncio.fixture
async def fake_ma() -> AsyncIterator[FakeMA]:
    fake = FakeMA()
    async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        fake.url = f"http://127.0.0.1:{port}"
        yield fake


def _client(fake: FakeMA, token: str = TOKEN) -> MAClient:
    return MAClient(fake.url, token, timeout=2)


def _ma_settings(fake: FakeMA) -> Settings:
    return _settings().model_copy(update={"ma_url": fake.url, "ma_token": SecretStr(TOKEN)})


@pytest_asyncio.fixture
async def declared(db) -> None:
    """p1 active, p2 inactive ; p3 inconnue."""
    from sqlalchemy import update

    async with db() as s:
        await s.execute(update(Speaker).values(ma_player_id=None))
        s.add_all(
            [
                Speaker(entity_id=_eid(), name="Grande salle", ma_player_id="p1"),
                Speaker(entity_id=_eid(), name="Off", ma_player_id="p2", is_active=False),
                Speaker(entity_id=_eid(), name="Sans MA"),
            ]
        )
        await s.commit()


@pytest.fixture
def music_api(client: AsyncClient, fake_ma: FakeMA, declared: None) -> AsyncClient:
    app.dependency_overrides[get_settings] = lambda: _ma_settings(fake_ma)
    return client


# --- client -----------------------------------------------------------------


def test_ws_url_and_config() -> None:
    assert ws_url("http://h:8095/") == "ws://h:8095/ws"
    assert ws_url("https://h") == "wss://h/ws"
    s = Settings()
    assert not s.ma_configured and s.ma_token.get_secret_value() == ""
    assert TOKEN not in repr(Settings(ma_token=SecretStr(TOKEN)))


async def test_client_commands(fake_ma: FakeMA) -> None:
    ma = _client(fake_ma)
    assert [p["player_id"] for p in await ma.players()] == ["p1", "p2"]
    res = await ma.search("x", ["track"], 5)
    assert res["tracks"][0]["uri"] == "lib://t/1"
    await ma.play_media("p1", "lib://t/1", "replace")
    await ma.queue_cmd("p1", "next")
    await ma.volume_set("p1", 40)
    assert (
        "music/search",
        {"search_query": "x", "limit": 5, "media_types": ["track"]},
    ) in fake_ma.cmds
    assert (
        "player_queues/play_media",
        {"queue_id": "p1", "media": "lib://t/1", "option": "replace"},
    ) in fake_ma.cmds
    assert ("player_queues/next", {"queue_id": "p1"}) in fake_ma.cmds
    assert ("players/cmd/volume_set", {"player_id": "p1", "volume_level": 40}) in fake_ma.cmds
    with pytest.raises(ValueError):
        await ma.queue_cmd("p1", "rm -rf")


async def test_client_errors(fake_ma: FakeMA) -> None:
    with pytest.raises(MAAuthError):
        await _client(fake_ma, "wrong").players()
    with pytest.raises(MAAuthError):
        await _client(fake_ma, "").players()
    fake_ma.fail.add("players/all")
    with pytest.raises(MACommandError):
        await _client(fake_ma).players()
    with pytest.raises(MAConnectionError):
        await MAClient("http://127.0.0.1:1", "t", timeout=1).players()


# --- API --------------------------------------------------------------------


async def test_music_roles_and_503(
    client: AsyncClient, make_user, login_as, fake_ma, declared
) -> None:
    assert (await client.get("/music/players")).status_code == 401
    await login_as(await make_user(Role.staff))
    r = await client.get("/music/players")  # MA non configuré
    assert r.status_code == 503
    app.dependency_overrides[get_settings] = lambda: _ma_settings(fake_ma)
    assert (await client.get("/music/players")).status_code == 200


async def test_music_student_refused(music_api, make_user) -> None:
    from app.auth import deps

    student = await make_user(Role.student)

    async def _cu() -> Any:
        return student

    app.dependency_overrides[deps.current_user] = _cu
    try:
        assert (await music_api.get("/music/players")).status_code == 403
    finally:
        app.dependency_overrides.pop(deps.current_user)


async def test_music_routes(music_api, make_user, login_as, fake_ma) -> None:
    await login_as(await make_user(Role.staff))
    r = await music_api.get("/music/players")
    assert [p["id"] for p in r.json()] == ["p1"]  # p2 inactive, non déclarés ignorés
    assert r.json()[0] == {
        "id": "p1",
        "name": "Grande salle",
        "available": True,
        "state": "playing",
        "volume": 30,
        "current": None,
    }
    r = await music_api.get("/music/search", params={"q": "abc", "media_types": ["track"]})
    assert r.json()["tracks"] == [
        {
            "uri": "lib://t/1",
            "name": "Song",
            "media_type": "track",
            "artists": ["A"],
            "image": None,
        }
    ]
    assert (await music_api.get("/music/search")).status_code == 422
    assert (
        await music_api.get("/music/search", params={"q": "a", "media_types": ["x"]})
    ).status_code == 422
    p = "/music/players/p1"
    assert (await music_api.post(f"{p}/play", json={"uri": "lib://t/1"})).status_code == 204
    assert (await music_api.post(f"{p}/play", json={"uri": "a b"})).status_code == 422
    for name in ("pause", "next", "previous", "resume"):
        assert (await music_api.post(f"{p}/{name}")).status_code == 204
    assert (await music_api.post(f"{p}/volume", json={"level": 55})).status_code == 204
    assert (await music_api.post(f"{p}/volume", json={"level": 101})).status_code == 422
    names = [c[0] for c in fake_ma.cmds]
    for c in (
        "player_queues/play_media",
        "player_queues/pause",
        "player_queues/next",
        "player_queues/previous",
        "player_queues/play",
        "players/cmd/volume_set",
    ):
        assert c in names
    fake_ma.fail.add("player_queues/next")
    assert (await music_api.post(f"{p}/next")).status_code == 502


async def test_music_undeclared_player_refused(music_api, make_user, login_as, fake_ma) -> None:
    await login_as(await make_user(Role.staff))
    n = len(fake_ma.cmds)
    for pid in ("p2", "p3"):  # inactive / inconnu
        p = f"/music/players/{pid}"
        assert (await music_api.post(f"{p}/play", json={"uri": "lib://t/1"})).status_code == 403
        assert (await music_api.post(f"{p}/volume", json={"level": 5})).status_code == 403
        for name in ("pause", "next", "previous", "resume"):
            assert (await music_api.post(f"{p}/{name}")).status_code == 403
    assert len(fake_ma.cmds) == n  # rien n'a atteint MA


async def test_music_no_declared_player(client, make_user, login_as, fake_ma, db) -> None:
    from sqlalchemy import update

    async with db() as s:
        await s.execute(update(Speaker).values(ma_player_id=None))
        await s.commit()
    app.dependency_overrides[get_settings] = lambda: _ma_settings(fake_ma)
    await login_as(await make_user(Role.staff))
    assert (await client.get("/music/players")).json() == []
    assert (await client.post("/music/players/p1/pause")).status_code == 403


async def test_music_unreachable_503(client, make_user, login_as, declared) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings().model_copy(
        update={"ma_url": "http://127.0.0.1:1", "ma_timeout": 1.0}
    )
    await login_as(await make_user(Role.staff))
    assert (await client.get("/music/players")).status_code == 503


# --- ducking ----------------------------------------------------------------


async def test_duck_and_resume(fake_ma: FakeMA) -> None:
    ma = _client(fake_ma)
    paused = await ma_service.duck(ma, ["p1", "p2", "", "unknown"])
    assert paused == ["p1"]  # p2 est idle : on ne touche pas
    await ma_service.resume(ma, paused)
    names = [(c, a.get("queue_id")) for c, a in fake_ma.cmds]
    assert ("player_queues/pause", "p1") in names and ("player_queues/play", "p1") in names
    assert ("player_queues/pause", "p2") not in names
    assert await ma_service.duck(None, ["p1"]) == []
    assert await ma_service.duck(ma, []) == []


async def test_duck_never_raises() -> None:
    ma = MAClient("http://127.0.0.1:1", "t", timeout=1)
    assert await ma_service.duck(ma, ["p1"]) == []
    await ma_service.resume(ma, ["p1"])


async def test_announcement_ducks_and_survives_ma_failure(db, redis, fake_ma) -> None:  # noqa: F811
    ann = Announcement(
        id=uuid.uuid4(),
        text="t",
        priority=int(Priority.staff),
        status="queued",
        is_test=False,
        targets=[{"id": "x", "entity_id": _eid(), "name": "n", "ma_player_id": "p1"}],
    )
    async with db() as s:
        s.add(ann)
        await s.commit()
    from app.ha import queue

    await queue.push(redis, str(ann.id), Priority.staff)
    ha, calls = mock_ha()
    assert await service.drain(db, redis, ha, _client(fake_ma)) == 1
    cmds = [c for c, _ in fake_ma.cmds]
    assert cmds.index("player_queues/pause") < cmds.index("player_queues/play")
    assert len(calls) == 1

    # MA hors service : l'annonce part quand même
    ann2 = Announcement(
        id=uuid.uuid4(),
        text="t2",
        priority=int(Priority.staff),
        status="queued",
        is_test=False,
        targets=[{"id": "x", "entity_id": _eid(), "name": "n", "ma_player_id": "p1"}],
    )
    async with db() as s:
        s.add(ann2)
        await s.commit()
    await queue.push(redis, str(ann2.id), Priority.staff)
    ha2, calls2 = mock_ha()
    dead = MAClient("http://127.0.0.1:1", "t", timeout=1)
    assert await service.drain(db, redis, ha2, dead) == 1
    assert len(calls2) == 1
    async with db() as s:
        assert (await s.get(Announcement, ann2.id)).status == "done"


async def test_speaker_ma_player_id(client, redis, make_user, login_as) -> None:  # noqa: F811
    app.dependency_overrides[get_redis] = lambda: redis
    await login_as(await make_user(Role.admin))
    e = _eid()
    r = await client.post("/speakers", json={"entity_id": e, "name": "n", "ma_player_id": "up1234"})
    assert r.status_code == 201 and r.json()["ma_player_id"] == "up1234"
    sid = r.json()["id"]
    r = await client.patch(f"/speakers/{sid}", json={"ma_player_id": "other"})
    assert r.json()["ma_player_id"] == "other"
    assert (await client.patch(f"/speakers/{sid}", json={"ma_player_id": "a b"})).status_code == 422
    assert (await client.patch(f"/speakers/{sid}", json={"ma_player_id": None})).json()[
        "ma_player_id"
    ] is None
    # un speaker sans mapping reste valide
    r = await client.post("/speakers", json={"entity_id": _eid(), "name": "m"})
    assert r.json()["ma_player_id"] is None


# --- fin de lecture / stop si repeat off --------------------------------------


def _qev(qid: str, state: str, **kw: Any) -> dict[str, Any]:
    return {"event": "queue_updated", "data": {"queue_id": qid, "state": state, **kw}}


async def test_end_of_play_stops_when_repeat_off(fake_ma: FakeMA) -> None:
    from app.ma.monitor import EndOfPlayDetector

    det = EndOfPlayDetector(_client(fake_ma))
    assert not await det.handle(_qev("p1", "playing", repeat_mode="off"))
    assert not await det.handle(_qev("p1", "paused", repeat_mode="off"))  # pause d'annonce
    assert not await det.handle(_qev("p1", "playing", repeat_mode="off"))
    assert await det.handle(_qev("p1", "idle", repeat_mode="off"))
    assert ("player_queues/stop", {"queue_id": "p1"}) in fake_ma.cmds
    n = len(fake_ma.cmds)
    assert not await det.handle(_qev("p1", "idle", repeat_mode="off"))  # pas de re-stop
    assert len(fake_ma.cmds) == n


async def test_end_of_play_respects_repeat(fake_ma: FakeMA) -> None:
    from app.ma.monitor import EndOfPlayDetector

    det = EndOfPlayDetector(_client(fake_ma))
    for mode in ("one", "all"):
        await det.handle(_qev("q", "playing", repeat_mode=mode))
        assert not await det.handle(_qev("q", "idle", repeat_mode=mode))
    assert all(c != "player_queues/stop" for c, _ in fake_ma.cmds)
    # repeat inconnu et illisible : on ne coupe pas
    det2 = EndOfPlayDetector(MAClient("http://127.0.0.1:1", "t", timeout=1))
    await det2.handle(_qev("z", "playing"))
    assert not await det2.handle(_qev("z", "idle"))
    # sans repeat dans l'événement : lu via player_queues/get
    await det.handle(
        {"event": "player_updated", "data": {"player_id": "r", "playback_state": "playing"}}
    )
    assert not await det.handle(
        {"event": "player_updated", "data": {"player_id": "r", "playback_state": "idle"}}
    )  # fake renvoie None pour get -> inconnu -> pas de stop
    assert not await det.handle({"event": "other", "data": {}})


async def test_monitor_run_end_to_end(fake_ma: FakeMA) -> None:
    import asyncio

    from app.ma import monitor

    fake_ma.push = [  # type: ignore[attr-defined]
        _qev("p1", "playing", repeat_mode="off"),
        _qev("p1", "idle", repeat_mode="off"),
    ]
    orig = fake_ma.handler

    async def handler(ws: Any) -> None:
        task = asyncio.create_task(orig(ws))
        await asyncio.sleep(0.2)
        for m in fake_ma.push:  # type: ignore[attr-defined]
            await ws.send(json.dumps(m))
        await task

    fake_ma.handler = handler  # le serveur déjà lancé garde l'ancien : relancer
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        t = asyncio.create_task(monitor.run(MAClient(f"http://127.0.0.1:{port}", TOKEN, timeout=2)))
        for _ in range(50):
            if any(c == "player_queues/stop" for c, _ in fake_ma.cmds):
                break
            await asyncio.sleep(0.1)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
    assert ("player_queues/stop", {"queue_id": "p1"}) in fake_ma.cmds


async def test_resume_only_if_was_playing(fake_ma: FakeMA) -> None:
    paused = await ma_service.duck(_client(fake_ma), ["p2"])  # p2 idle
    await ma_service.resume(_client(fake_ma), paused)
    assert not any(c.startswith("player_queues/p") for c, _ in fake_ma.cmds if c.endswith("play"))
    assert "player_queues/play" not in [c for c, _ in fake_ma.cmds]


# --- lecteurs disponibles (admin) --------------------------------------------


async def test_available_players_admin(music_api, make_user, login_as, fake_ma) -> None:
    await login_as(await make_user(Role.staff))
    assert (await music_api.get("/music/players/available")).status_code == 403
    await login_as(await make_user(Role.admin))
    r = await music_api.get("/music/players/available")
    assert r.status_code == 200
    by_id = {p["player_id"]: p for p in r.json()}
    assert set(by_id) == {"p1", "p2"}
    assert by_id["p1"]["declared_as"] == "Grande salle"
    assert by_id["p2"]["declared_as"] == "Off"
    assert by_id["p1"]["name"] == "Salon" and by_id["p1"]["available"] is True
    assert TOKEN not in r.text


async def test_available_players_errors(client, make_user, login_as, declared) -> None:
    await login_as(await make_user(Role.admin))
    app.dependency_overrides[get_settings] = lambda: _settings().model_copy(
        update={"ma_url": "http://127.0.0.1:1", "ma_timeout": 1.0}
    )
    assert (await client.get("/music/players/available")).status_code == 503


async def test_admin_ma_players_fragment(music_api, make_user, login_as, db) -> None:
    async with db() as s:
        s.add(Speaker(entity_id=_eid(), name="Orpheline", ma_player_id="zzz"))
        await s.commit()
    await login_as(await make_user(Role.admin))
    r = await music_api.get("/ui/admin/ma-players")
    assert r.status_code == 200
    assert "Salon" in r.text and "p1" in r.text and "Utiliser" in r.text
    assert "Orpheline" in r.text and "aucun lecteur" in r.text
    assert TOKEN not in r.text
    async with db() as s:
        await s.execute(delete(Speaker).where(Speaker.ma_player_id == "zzz"))
        await s.commit()
