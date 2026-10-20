import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth import oidc
from app.auth.deps import get_session
from app.config import Settings, get_settings
from app.main import app
from app.models import Role, User
from tests.helpers import _KEY, _OTHER_KEY, KID, TEST_DB, _settings, make_id_token


@pytest.fixture(scope="session")
def migrated_db() -> str:
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    env = {**os.environ, "DATABASE_URL": TEST_DB}
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "alembic", *a], env=env, check=True, capture_output=True
    )
    run("downgrade", "base")
    run("upgrade", "head")
    return TEST_DB


@pytest_asyncio.fixture
async def db(migrated_db: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch):
    async def _session() -> AsyncIterator[AsyncSession]:
        async with db() as s:
            yield s
            await s.commit()

    async def _jwks(_: Settings) -> list[dict[str, Any]]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
        return [{**jwk, "kid": KID, "alg": "RS256", "use": "sig"}]

    monkeypatch.setattr(oidc, "fetch_jwks", _jwks)
    oidc._jwks_cache.update(keys=None, at=0.0)
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_session] = _session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://sound.test", follow_redirects=False
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def other_key() -> Any:
    return _OTHER_KEY


@pytest.fixture
def do_login(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """Joue login -> callback avec un id_token forgé ; renvoie la réponse du callback."""

    async def run(**claims: Any) -> Any:
        key = claims.pop("_key", _KEY)
        r = await client.get("/auth/login")
        assert r.status_code == 302
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["code_challenge_method"] == ["S256"]
        token = make_id_token(**{"nonce": q["nonce"][0], "key": key, **claims})

        async def _exchange(*_: Any) -> str:
            return token

        monkeypatch.setattr(oidc, "exchange_code", _exchange)
        return await client.get("/auth/callback", params={"code": "c", "state": q["state"][0]})

    return run


@pytest_asyncio.fixture
async def make_user(db: async_sessionmaker[AsyncSession]) -> Callable[..., Any]:
    async def run(role: Role = Role.admin, active: bool = True, email: str | None = None) -> User:
        async with db() as s:
            u = User(
                email=email or f"{uuid.uuid4().hex[:10]}@epitech.eu",
                role=role.value,
                is_active=active,
            )
            s.add(u)
            await s.commit()
            return u

    return run


@pytest_asyncio.fixture
async def login_as(do_login: Callable[..., Any], client: AsyncClient) -> Callable[..., Any]:
    async def run(user: User) -> None:
        r = await do_login(preferred_username=user.email)
        assert r.status_code == 302, r.text

    return run
