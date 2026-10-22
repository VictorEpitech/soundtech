"""Réponses étudiants : enregistrement, rapport (répondants / non-répondants), CSV, SSE, purge."""

import asyncio
import csv
import io
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.roster import Member, RosterError, RosterProvider
from app.models import Response, ResponseStatus

log = logging.getLogger("sound.alerts.responses")

STATUSES = [s.value for s in ResponseStatus]
TEXT_MAX = 500
STREAM_MAX_SECONDS = 900
KEEPALIVE_SECONDS = 15
MAX_STREAMS_PER_USER = 3
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def channel(incident_id: uuid.UUID | str) -> str:
    return f"incident:{incident_id}:responses"


def clean_text(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = _CTRL.sub("", v).strip()[:TEXT_MAX]
    return v or None


async def record(
    session: AsyncSession,
    incident_id: uuid.UUID,
    *,
    aad_id: str,
    name: str,
    email: str | None,
    status: str,
    text: str | None,
) -> None:
    """Upsert atomique : une seule ligne par (incident, utilisateur), la dernière réponse gagne."""
    stmt = insert(Response).values(
        id=uuid.uuid4(),
        incident_id=incident_id,
        user_aad_id=aad_id,
        user_email=email,
        user_name=name,
        status=status,
        text=text,
        received_at=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_responses_incident_user",
        set_={
            "user_email": stmt.excluded.user_email,
            "user_name": stmt.excluded.user_name,
            "status": stmt.excluded.status,
            "text": stmt.excluded.text,
            "received_at": stmt.excluded.received_at,
        },
    )
    await session.execute(stmt)


async def publish(redis: Redis, incident_id: uuid.UUID, status: str) -> None:
    """Notifie les dashboards. Fail-open : une réponse enregistrée ne doit pas échouer ici."""
    try:
        await redis.publish(channel(incident_id), json.dumps({"status": status}))
    except Exception:
        log.warning("response publish failed", exc_info=True)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if isinstance(d, datetime) else None


async def report(
    session: AsyncSession, roster: RosterProvider, incident_id: uuid.UUID
) -> tuple[dict[str, Any], list[Member]]:
    rows = (
        await session.scalars(
            select(Response)
            .where(Response.incident_id == incident_id)
            .order_by(Response.received_at.desc())
        )
    ).all()
    members: list[Member] = []
    roster_error: str | None = None
    if roster.configured:
        try:
            members = await roster.members()
        except RosterError as e:
            roster_error = str(e)
            log.warning("roster unavailable: %s", e)
    by_id = {m.aad_id.lower(): m for m in members}
    respondents = []
    for r in rows:
        m = by_id.get(r.user_aad_id.lower())
        respondents.append(
            {
                "aad_id": r.user_aad_id,
                "name": r.user_name or (m.name if m else ""),
                "email": r.user_email or (m.email if m else None),
                "status": r.status,
                "text": r.text,
                "received_at": _iso(r.received_at),
            }
        )
    seen = {r.user_aad_id.lower() for r in rows}
    missing = [m for m in members if m.aad_id.lower() not in seen]
    counts: dict[str, int] = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    data = {
        "incident_id": str(incident_id),
        "roster": {"configured": roster.configured, "error": roster_error},
        "counts": {
            **counts,
            "responded": len(rows),
            "not_responded": len(missing),
            "roster_total": len(members),
        },
        "respondents": respondents,
        "non_respondents": [
            {"aad_id": m.aad_id, "name": m.name, "email": m.email} for m in missing
        ],
    }
    return data, missing


def neutralize(v: Any) -> str:
    """Neutralise l'injection de formules (CSV/Excel) : préfixe d'une apostrophe."""
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r", "\n") or s.lstrip()[:1] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def to_csv(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["statut", "nom", "email", "identifiant_entra", "texte", "recu_le"])
    for r in data["respondents"]:
        w.writerow(
            [
                neutralize(x)
                for x in (
                    r["status"],
                    r["name"],
                    r["email"],
                    r["aad_id"],
                    r["text"],
                    r["received_at"],
                )
            ]
        )
    for m in data["non_respondents"]:
        w.writerow(
            [neutralize(x) for x in ("no_response", m["name"], m["email"], m["aad_id"], "", "")]
        )
    return "﻿" + buf.getvalue()


async def purge(session: AsyncSession, retention_days: int) -> int:
    """Supprime les réponses plus anciennes que la rétention (<= 0 : conservation illimitée)."""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    res = await session.execute(delete(Response).where(Response.received_at < cutoff))
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


def _slot_key(user_id: Any) -> str:
    return f"sse:streams:{user_id}"


async def acquire_stream_slot(
    redis: Redis, user_id: Any, limit: int = MAX_STREAMS_PER_USER
) -> bool:
    """Réserve un flux SSE pour l'utilisateur (compteur Redis, TTL de secours). Fail-open."""
    key = _slot_key(user_id)
    try:
        n = await redis.incr(key)
        await redis.expire(key, STREAM_MAX_SECONDS + 60)
        if n > limit:
            await redis.decr(key)
            return False
    except Exception:
        log.warning("stream slot unavailable", exc_info=True)
    return True


async def release_stream_slot(redis: Redis, user_id: Any) -> None:
    key = _slot_key(user_id)
    try:
        if await redis.decr(key) < 0:
            await redis.delete(key)
    except Exception:
        log.debug("stream slot release failed", exc_info=True)


async def event_stream(
    redis: Redis,
    incident_id: uuid.UUID,
    is_disconnected: Any,
    max_seconds: float = STREAM_MAX_SECONDS,
    keepalive: float = KEEPALIVE_SECONDS,
    on_close: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[str]:
    """Flux SSE : un évènement `update` par réponse publiée, commentaire keepalive sinon."""
    pubsub = redis.pubsub()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    try:
        await pubsub.subscribe(channel(incident_id))
        yield "retry: 3000\n\n"
        while loop.time() < deadline and not await is_disconnected():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=keepalive)
            if msg is None:
                yield ": keepalive\n\n"
                continue
            data = msg["data"]
            data = data.decode() if isinstance(data, bytes) else str(data)
            yield f"event: update\ndata: {data}\n\n"
    finally:
        if on_close is not None:
            await on_close()
        try:
            await pubsub.unsubscribe(channel(incident_id))
            await pubsub.aclose()
        except Exception:
            log.debug("pubsub close failed", exc_info=True)
