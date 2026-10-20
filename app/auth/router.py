import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit, oidc, tokens
from app.auth.deps import ACCESS_COOKIE, REFRESH_COOKIE, current_user, get_session
from app.config import Settings, get_settings
from app.models import RefreshToken, Role, User

log = logging.getLogger("sound.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

TX_COOKIE = "oidc_tx"
TX_TTL = 600
LOGIN_ROLES = {Role.admin.value, Role.staff.value}


def _check_config(s: Settings) -> None:
    if not (s.entra_tenant_id and s.entra_client_id and s.entra_client_secret and s.jwt_secret):
        raise HTTPException(503, "auth not configured")


def _set_cookie(resp: Response, name: str, value: str, max_age: int, path: str) -> None:
    resp.set_cookie(
        name, value, max_age=max_age, path=path, httponly=True, secure=True, samesite="lax"
    )


def _set_session_cookies(resp: Response, s: Settings, access: str, refresh: str) -> None:
    _set_cookie(resp, ACCESS_COOKIE, access, s.access_token_ttl, "/")
    _set_cookie(resp, REFRESH_COOKIE, refresh, s.refresh_token_ttl, "/auth")


def _clear_session_cookies(resp: Response) -> None:
    resp.delete_cookie(ACCESS_COOKIE, path="/")
    resp.delete_cookie(REFRESH_COOKIE, path="/auth")


async def _issue(session: AsyncSession, s: Settings, user: User) -> tuple[str, str]:
    jti = secrets.token_urlsafe(32)
    session.add(
        RefreshToken(
            jti=jti,
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(seconds=s.refresh_token_ttl),
        )
    )
    return tokens.access_token(s, user.id, user.role), tokens.refresh_token(s, user.id, jti)


@router.get("/login")
async def login(settings: Settings = Depends(get_settings)) -> RedirectResponse:
    _check_config(settings)
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = oidc.new_pkce()
    tx = tokens.encode(settings, "oidc_tx", TX_TTL, state=state, nonce=nonce, cv=verifier)
    resp = RedirectResponse(oidc.authorize_url(settings, state, nonce, challenge), status_code=302)
    _set_cookie(resp, TX_COOKIE, tx, TX_TTL, "/auth")
    return resp


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    _check_config(settings)
    if error or not code or not state:
        audit.record(
            session, "login_failed", request=request, detail={"reason": error or "missing"}
        )
        await session.commit()
        raise HTTPException(400, "authentication failed")
    try:
        tx = tokens.decode(settings, request.cookies.get(TX_COOKIE, ""), "oidc_tx")
    except jwt.PyJWTError:
        raise HTTPException(400, "login transaction expired or missing") from None
    if not secrets.compare_digest(tx["state"], state):
        audit.record(session, "login_failed", request=request, detail={"reason": "state"})
        await session.commit()
        raise HTTPException(400, "state mismatch")

    try:
        id_token = await oidc.exchange_code(settings, code, tx["cv"])
        claims = await oidc.validate_id_token(settings, id_token, tx["nonce"])
        oid = claims["oid"]
    except Exception as e:  # noqa: BLE001 - tout échec réseau/validation = refus
        log.warning("oidc failure: %s", e)
        audit.record(session, "login_failed", request=request, detail={"reason": "oidc"})
        await session.commit()
        raise HTTPException(401, "authentication failed") from None

    email = (claims.get("preferred_username") or claims.get("email") or "").strip().lower()
    user = (await session.execute(select(User).where(User.entra_oid == oid))).scalars().first()
    if user is None:
        user = (await session.execute(select(User).where(User.email == email))).scalars().first()
    reason = None
    if user is None:
        reason = "unknown_user"
    elif not user.is_active:
        reason = "disabled"
    elif user.role not in LOGIN_ROLES:
        reason = "role_not_allowed"
    elif user.entra_oid not in (None, oid):
        reason = "oid_mismatch"
    if reason is not None or user is None:
        audit.record(
            session,
            "login_denied",
            request=request,
            user_id=user.id if user else None,
            actor=email,
            detail={"reason": reason},
        )
        await session.commit()
        raise HTTPException(403, "account not authorized")
    user.entra_oid = oid
    user.display_name = claims.get("name") or user.display_name
    user.last_login_at = datetime.now(UTC)
    await session.flush()

    access, refresh = await _issue(session, settings, user)
    audit.record(
        session, "login", request=request, user_id=user.id, actor=email, detail={"role": user.role}
    )
    resp = RedirectResponse("/", status_code=302)
    _set_session_cookies(resp, settings, access, refresh)
    resp.delete_cookie(TX_COOKIE, path="/auth")
    return resp


async def _valid_refresh(
    session: AsyncSession, settings: Settings, request: Request
) -> tuple[RefreshToken, User] | None:
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        return None
    try:
        claims = tokens.decode(settings, raw, "refresh")
        rt = await session.get(RefreshToken, claims["jti"])
        user = await session.get(User, uuid.UUID(claims["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        return None
    if (
        rt is None
        or user is None
        or rt.user_id != user.id
        or not user.is_active
        or user.role not in LOGIN_ROLES
    ):
        return None
    if rt.revoked_at is not None or rt.expires_at < datetime.now(UTC):
        return None
    return rt, user


@router.post("/refresh")
async def refresh(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    found = await _valid_refresh(session, settings, request)
    if found is None:
        raise HTTPException(401, "invalid refresh token")
    rt, user = found
    now = datetime.now(UTC)
    consumed = await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.jti == rt.jti,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .values(revoked_at=now)
        .returning(RefreshToken.jti)
    )
    if consumed.first() is None:
        raise HTTPException(401, "invalid refresh token")
    access, new_refresh = await _issue(session, settings, user)
    resp = Response(status_code=204)
    _set_session_cookies(resp, settings, access, new_refresh)
    return resp


@router.post("/logout")
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    found = await _valid_refresh(session, settings, request)
    if found is not None:
        _, user = found
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        audit.record(session, "logout", request=request, user_id=user.id, actor=user.email)
    resp = Response(status_code=204)
    _clear_session_cookies(resp)
    return resp


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.display_name,
        "role": user.role,
    }
