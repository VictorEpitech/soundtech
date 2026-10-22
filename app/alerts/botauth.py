"""Vérification du JWT entrant du Bot Framework (signature JWKS, audience, issuer)."""

import asyncio
import time
from typing import Any

import httpx
import jwt

from app.config import Settings

OPENID_CONFIG = "https://login.botframework.com/v1/.well-known/openidconfiguration"
ISSUER = "https://api.botframework.com"
JWKS_TTL = 3600
JWKS_REFRESH_COOLDOWN = 60


class BotAuthError(Exception):
    pass


_cache: dict[str, Any] = {"keys": None, "at": 0.0}
_lock = asyncio.Lock()


async def fetch_jwks() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10) as client:
        meta = await client.get(OPENID_CONFIG)
        meta.raise_for_status()
        r = await client.get(meta.json()["jwks_uri"])
        r.raise_for_status()
    return r.json()["keys"]


async def _signing_key(kid: str) -> Any:
    async with _lock:
        now = time.time()
        fresh = _cache["keys"] is not None and now - _cache["at"] < JWKS_TTL
        for attempt in (0, 1):
            if attempt and now - _cache["at"] < JWKS_REFRESH_COOLDOWN:
                break  # kid inconnu : pas de refetch avant le cooldown
            if not fresh or attempt:
                try:
                    _cache["keys"] = await fetch_jwks()
                except (httpx.HTTPError, KeyError, ValueError):
                    raise BotAuthError("cannot fetch signing keys") from None
                _cache["at"] = now = time.time()
            for k in _cache["keys"]:
                if k.get("kid") == kid:
                    return jwt.PyJWK(k).key
            fresh = False
    raise BotAuthError("unknown signing key")


async def verify(settings: Settings, authorization: str | None, service_url: str | None) -> None:
    """Lève `BotAuthError` si le jeton n'est pas un jeton Bot Framework valide pour notre app."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise BotAuthError("missing bearer token")
    token = authorization[7:].strip()
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise BotAuthError("unexpected token header")
        key = await _signing_key(header["kid"])
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.teams_app_id,
            issuer=ISSUER,
            options={"require": ["exp", "iss", "aud"]},
            leeway=300,
        )
    except jwt.PyJWTError as e:
        raise BotAuthError(f"invalid token: {e}") from e
    claim_url = claims.get("serviceurl")
    if (
        not isinstance(claim_url, str)
        or not isinstance(service_url, str)
        or not claim_url
        or claim_url.rstrip("/") != service_url.rstrip("/")
    ):
        raise BotAuthError("serviceUrl missing or mismatch")
