# ruff: noqa: F811
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app import ratelimit
from app.config import Settings, get_settings
from tests.helpers import _settings
from tests.test_ha import ha_api, redis  # noqa: F401


class FakePipe:
    def __init__(self, store: dict[str, int], fail: bool) -> None:
        self.store, self.fail, self.key = store, fail, ""

    def incr(self, key: str) -> None:
        self.key = key

    def expire(self, key: str, window: int, nx: bool = False) -> None:
        assert nx and window > 0

    async def execute(self) -> list[int]:
        if self.fail:
            raise ConnectionError("redis down")
        self.store[self.key] = self.store.get(self.key, 0) + 1
        return [self.store[self.key], 1]


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.store: dict[str, int] = {}
        self.fail = fail

    def pipeline(self, transaction: bool = True) -> FakePipe:
        return FakePipe(self.store, self.fail)


def _app(redis: Any, **over: Any) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    settings = _settings().model_copy(update=over)
    app.dependency_overrides[get_settings] = lambda: settings
    user = SimpleNamespace(id=uuid.UUID(int=1))
    other = SimpleNamespace(id=uuid.UUID(int=2))
    who = {"u": user}

    async def current() -> Any:
        return who["u"]

    app.state.who, app.state.users = who, (user, other)

    @app.get("/ip", dependencies=[Depends(ratelimit.per_ip("t", "rl_auth_limit"))])
    async def ip() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/user")
    async def usr(u: Any = Depends(ratelimit.per_user("t", "rl_music_limit", current))) -> dict:
        return {"ok": True}

    return app


async def _get(app: FastAPI, path: str, n: int) -> list[int]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return [(await c.get(path)).status_code for _ in range(n)]


async def test_per_ip_blocks_after_limit() -> None:
    app = _app(FakeRedis(), rl_auth_limit=3)
    assert await _get(app, "/ip", 5) == [200, 200, 200, 429, 429]


async def test_429_has_retry_after() -> None:
    app = _app(FakeRedis(), rl_auth_limit=1, rl_window=42)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/ip")
        r = await c.get("/ip")
    assert r.status_code == 429 and r.headers["retry-after"] == "42"


async def test_per_user_isolated() -> None:
    app = _app(FakeRedis(), rl_music_limit=2)
    assert await _get(app, "/user", 3) == [200, 200, 429]
    app.state.who["u"] = app.state.users[1]
    assert await _get(app, "/user", 1) == [200]


async def test_zero_disables() -> None:
    app = _app(FakeRedis(), rl_auth_limit=0)
    assert await _get(app, "/ip", 5) == [200] * 5


async def test_fail_open_when_redis_down() -> None:
    app = _app(FakeRedis(fail=True), rl_auth_limit=1)
    assert await _get(app, "/ip", 3) == [200] * 3


async def test_fail_open_when_redis_missing() -> None:
    app = _app(None, rl_auth_limit=1)
    del app.state.redis
    assert await _get(app, "/ip", 3) == [200] * 3


def test_defaults_generous_for_webhook() -> None:
    s = Settings.model_fields
    assert s["rl_webhook_limit"].default >= 1000
    assert s["rl_auth_limit"].default < s["rl_webhook_limit"].default


@pytest.mark.usefixtures("migrated_db")
async def test_webhook_rate_limited(ha_api, redis, monkeypatch) -> None:  # noqa: F811
    from app.main import app as real
    from tests.test_responses import _bot_settings

    monkeypatch.setattr(real.state, "redis", redis, raising=False)
    await redis.flushdb()
    s = _bot_settings().model_copy(update={"rl_webhook_limit": 2})
    real.dependency_overrides[get_settings] = lambda: s
    codes = [
        (await ha_api.post("/teams/messages", json={"type": "message"})).status_code
        for _ in range(3)
    ]
    assert codes[:2] == [401, 401] and codes[2] == 429
