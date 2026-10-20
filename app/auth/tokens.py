import time
import uuid
from typing import Any

import jwt

from app.config import Settings

ALG = "HS256"


def _secret(s: Settings) -> str:
    if len(s.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be set (32+ chars)")
    return s.jwt_secret


def encode(s: Settings, typ: str, ttl: int, **claims: Any) -> str:
    now = int(time.time())
    payload = {"typ": typ, "iat": now, "exp": now + ttl, **claims}
    return jwt.encode(payload, _secret(s), algorithm=ALG)


def decode(s: Settings, token: str, typ: str) -> dict[str, Any]:
    claims = jwt.decode(
        token, _secret(s), algorithms=[ALG], options={"require": ["exp", "iat", "typ"]}
    )
    if claims["typ"] != typ:
        raise jwt.InvalidTokenError("wrong token type")
    return claims


def access_token(s: Settings, user_id: uuid.UUID, role: str) -> str:
    return encode(s, "access", s.access_token_ttl, sub=str(user_id), role=role)


def refresh_token(s: Settings, user_id: uuid.UUID, jti: str) -> str:
    return encode(s, "refresh", s.refresh_token_ttl, sub=str(user_id), jti=jti)
