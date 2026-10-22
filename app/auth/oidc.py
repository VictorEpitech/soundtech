import asyncio
import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.config import Settings

LOGIN_BASE = "https://login.microsoftonline.com"
JWKS_TTL = 3600


class OIDCError(Exception):
    pass


def issuer(s: Settings) -> str:
    return f"{LOGIN_BASE}/{s.entra_tenant_id}/v2.0"


def redirect_uri(s: Settings) -> str:
    return f"{s.public_base_url.rstrip('/')}/auth/callback"


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(s: Settings, state: str, nonce: str, challenge: str) -> str:
    q = urlencode(
        {
            "client_id": s.entra_client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri(s),
            "response_mode": "query",
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{LOGIN_BASE}/{s.entra_tenant_id}/oauth2/v2.0/authorize?{q}"


async def exchange_code(s: Settings, code: str, verifier: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{LOGIN_BASE}/{s.entra_tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": s.entra_client_id,
                "client_secret": s.entra_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(s),
                "code_verifier": verifier,
            },
        )
    if r.status_code != 200:
        try:
            body = r.json()
            err = f"{body.get('error')}: {str(body.get('error_description', ''))[:160]}"
        except ValueError:
            err = "corps non JSON"
        raise OIDCError(f"token endpoint returned {r.status_code} ({err})")
    id_token = r.json().get("id_token")
    if not id_token:
        raise OIDCError("no id_token in token response")
    return id_token


_jwks_cache: dict[str, Any] = {"keys": None, "at": 0.0}
_jwks_lock = asyncio.Lock()


async def fetch_jwks(s: Settings) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{LOGIN_BASE}/{s.entra_tenant_id}/discovery/v2.0/keys")
    r.raise_for_status()
    return r.json()["keys"]


async def _signing_key(s: Settings, kid: str) -> Any:
    async with _jwks_lock:
        fresh = _jwks_cache["keys"] is not None and time.time() - _jwks_cache["at"] < JWKS_TTL
        for attempt in (0, 1):
            if not fresh or attempt:
                _jwks_cache["keys"] = await fetch_jwks(s)
                _jwks_cache["at"] = time.time()
            for k in _jwks_cache["keys"]:
                if k.get("kid") == kid:
                    return jwt.PyJWK(k).key
            fresh = False
    raise OIDCError("unknown signing key")


async def validate_id_token(s: Settings, id_token: str, nonce: str) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise OIDCError("unexpected id_token header")
        key = await _signing_key(s, header["kid"])
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=s.entra_client_id,
            issuer=issuer(s),
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=30,
        )
    except jwt.PyJWTError as e:
        raise OIDCError(f"invalid id_token: {e}") from e
    if claims.get("tid") != s.entra_tenant_id:
        raise OIDCError("tenant mismatch")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OIDCError("nonce mismatch")
    if not claims.get("oid"):
        raise OIDCError("missing oid claim")
    return claims
