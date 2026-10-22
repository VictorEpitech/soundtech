"""Web Push (VAPID) pour les admins/staff : abonnements, endpoints, envoi.

Jamais bloquant ni fatal : sans clés VAPID, le push est simplement désactivé ; toute erreur
d'envoi est journalisée et comptée, jamais propagée à l'appelant.
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from pywebpush import WebPushException, webpush
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.deps import get_session, require_role
from app.config import Settings, get_settings
from app.models import PushSubscription, Role, User
from app.ratelimit import per_user

log = logging.getLogger("sound.push")

staff_up = require_role(Role.admin, Role.staff)
push_user = per_user("push", "rl_push_limit", staff_up)

router = APIRouter(prefix="/push", tags=["push"])

ENDPOINT_MAX = 2048
_B64URL = re.compile(r"^[A-Za-z0-9_-]{16,255}={0,2}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
MAX_PARALLEL = 5
GONE = (404, 410)


DEFAULT_ALLOWED_HOSTS = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
    "push.apple.com",
)


def allowed_hosts(settings: Settings) -> tuple[str, ...]:
    extra = tuple(
        h.strip().lower().lstrip(".") for h in settings.push_allowed_hosts.split(",") if h.strip()
    )
    return DEFAULT_ALLOWED_HOSTS + extra


def valid_endpoint(url: str, allowed: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS) -> bool:
    """https, port 443, hôte public d'un service push connu (liste blanche de suffixes)."""
    if len(url) > ENDPOINT_MAX or _CTRL.search(url) or " " in url:
        return False
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return False
    if u.scheme != "https" or not host or u.username or u.password or port not in (None, 443):
        return False
    if host.endswith(".") or host == "localhost" or host.endswith(".localhost") or "." not in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False  # IP littérale
    except ValueError:
        pass
    return any(host == a or host.endswith("." + a) for a in allowed)


def _resolves_public(host: str) -> bool:
    """Toutes les adresses résolues doivent être globales (ni privée, loopback, link-local...)."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addrs = {i[4][0] for i in infos}
    try:
        return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)
    except ValueError:
        return False


class _NoRedirectSession(requests.Session):
    def request(self, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        kwargs["allow_redirects"] = False
        return super().request(*args, **kwargs)


def _webpush_call(settings: Settings, info: dict[str, Any], data: str) -> Any:
    host = urlsplit(info["endpoint"]).hostname or ""
    if not _resolves_public(host):
        raise PermissionError("endpoint resolves to a non-public address")
    with _NoRedirectSession() as sess:
        return webpush(
            subscription_info=info,
            data=data,
            vapid_private_key=settings.vapid_private_key.get_secret_value(),
            vapid_claims={"sub": settings.vapid_subject},
            ttl=3600,
            headers={"Urgency": "high"},
            timeout=settings.push_timeout,
            requests_session=sess,
        )


class Keys(BaseModel):
    p256dh: str = Field(pattern=_B64URL.pattern)
    auth: str = Field(pattern=_B64URL.pattern)


class SubscribeIn(BaseModel):
    endpoint: str = Field(min_length=12, max_length=ENDPOINT_MAX)
    keys: Keys


class UnsubscribeIn(BaseModel):
    endpoint: str = Field(min_length=12, max_length=ENDPOINT_MAX)


@router.get("/vapid-public-key")
async def vapid_public_key(
    settings: Settings = Depends(get_settings), _: User = Depends(push_user)
) -> dict[str, Any]:
    if not settings.push_configured:
        return {"enabled": False, "public_key": None}
    return {"enabled": True, "public_key": settings.vapid_public_key}


@router.post("/subscribe", status_code=201)
async def subscribe(
    body: SubscribeIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    user: User = Depends(push_user),
) -> dict[str, bool]:
    if not settings.push_configured:
        raise HTTPException(503, "push notifications are not configured")
    if not valid_endpoint(body.endpoint, allowed_hosts(settings)):
        raise HTTPException(422, "invalid push endpoint")
    ua = _CTRL.sub("", request.headers.get("user-agent", ""))[:255] or None
    stmt = insert(PushSubscription).values(
        user_id=user.id,
        endpoint=body.endpoint,
        p256dh=body.keys.p256dh,
        auth=body.keys.auth,
        user_agent=ua,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PushSubscription.endpoint],
        set_={
            "p256dh": stmt.excluded.p256dh,
            "auth": stmt.excluded.auth,
            "user_agent": stmt.excluded.user_agent,
        },
        where=PushSubscription.user_id == user.id,  # jamais de réattribution à un autre user
    ).returning(PushSubscription.id)
    if (await session.execute(stmt)).scalar_one_or_none() is None:
        raise HTTPException(409, "endpoint already registered by another user")
    audit.record(session, "push_subscribe", request=request, user_id=user.id, actor=user.email)
    return {"subscribed": True}


@router.delete("/subscribe")
async def unsubscribe(
    body: UnsubscribeIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(push_user),
) -> dict[str, int]:
    res = await session.execute(
        delete(PushSubscription).where(
            PushSubscription.endpoint == body.endpoint, PushSubscription.user_id == user.id
        )
    )
    n = int(res.rowcount or 0)  # type: ignore[attr-defined]
    if n:
        audit.record(
            session, "push_unsubscribe", request=request, user_id=user.id, actor=user.email
        )
    return {"deleted": n}


@dataclass
class PushResult:
    attempted: int = 0
    delivered: int = 0
    removed: int = 0
    failed: int = 0


async def _send_one(settings: Settings, sub: PushSubscription, data: str) -> str:
    """Renvoie "ok" | "gone" | "failed"."""
    info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_webpush_call, settings, info, data),
            timeout=settings.push_timeout + 3,
        )
        return "ok"
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in GONE:
            return "gone"
        log.warning("push failed (http %s)", status)
    except Exception as exc:
        log.warning("push failed: %s", type(exc).__name__)
    return "failed"


async def send_to_staff(
    session: AsyncSession, settings: Settings, payload: dict[str, Any]
) -> PushResult:
    """Envoie `payload` à tous les abonnements admin/staff actifs ; purge les 404/410.

    Ne lève jamais (hors annulation) : le push est un canal best-effort.
    """
    res = PushResult()
    if not settings.push_configured:
        return res
    try:
        subs = list(
            (
                await session.scalars(
                    select(PushSubscription)
                    .join(User, User.id == PushSubscription.user_id)
                    .where(User.is_active.is_(True), User.role.in_(("admin", "staff")))
                )
            ).all()
        )
        res.attempted = len(subs)
        data = json.dumps(payload, separators=(",", ":"))
        sem = asyncio.Semaphore(MAX_PARALLEL)

        async def one(sub: PushSubscription) -> str:
            async with sem:
                return await _send_one(settings, sub, data)

        outcomes = await asyncio.gather(*(one(s) for s in subs))
        ok_ids = [s.id for s, o in zip(subs, outcomes, strict=True) if o == "ok"]
        gone_ids = [s.id for s, o in zip(subs, outcomes, strict=True) if o == "gone"]
        res.delivered, res.removed = len(ok_ids), len(gone_ids)
        res.failed = len(subs) - res.delivered - res.removed
        if gone_ids:
            await session.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone_ids)))
        if ok_ids:
            await session.execute(
                update(PushSubscription)
                .where(PushSubscription.id.in_(ok_ids))
                .values(last_ok_at=datetime.now(UTC))
            )
        await session.commit()
    except Exception:
        log.exception("push dispatch failed")
        await session.rollback()
    return res
