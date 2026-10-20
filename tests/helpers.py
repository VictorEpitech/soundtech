import os
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings

TEST_DB = os.environ.get("TEST_DATABASE_URL")
TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
KID = "test-kid"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _settings() -> Settings:
    return Settings(
        database_url=TEST_DB or "postgresql+asyncpg://x:x@localhost/x",
        entra_tenant_id=TENANT,
        entra_client_id=CLIENT,
        entra_client_secret="secret",
        public_base_url="https://sound.test",
        jwt_secret="x" * 40,
    )


def make_id_token(nonce: str, *, key: Any = _KEY, **over: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": CLIENT,
        "tid": TENANT,
        "sub": "sub-" + uuid.uuid4().hex,
        "oid": str(uuid.uuid4()),
        "preferred_username": "someone@epitech.eu",
        "name": "Some One",
        "nonce": nonce,
        "iat": now,
        "exp": now + 300,
        **over,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


async def audit_actions(db: async_sessionmaker[AsyncSession], actor: str) -> list[str]:
    from app.models import AuditLog

    async with db() as s:
        rows = await s.scalars(
            select(AuditLog.action).where(AuditLog.actor == actor).order_by(AuditLog.id)
        )
        return list(rows)
