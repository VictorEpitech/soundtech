import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.teams import IncidentNotice, TeamsNotifier
from app.auth import audit
from app.config import Settings
from app.ha import service as ha_service
from app.models import Incident, IncidentStatus, Priority, Speaker, User

log = logging.getLogger("sound.alerts")


class AlertError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def expected_confirm(settings: Settings, incident: Incident) -> str:
    return settings.alert_test_confirm_text if incident.is_test else settings.alert_confirm_text


async def _transition(
    session: AsyncSession, incident_id: uuid.UUID, src: IncidentStatus, **values: Any
) -> bool:
    """Transition atomique : un seul appelant concurrent peut passer `src` -> nouvel état."""
    res = await session.execute(
        update(Incident)
        .where(Incident.id == incident_id, Incident.status == src.value)
        .values(**values)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _claim(session: AsyncSession, settings: Settings, incident_id: uuid.UUID) -> bool:
    """Prend la main sur l'envoi, de façon atomique : un seul appelant concurrent gagne."""
    now = datetime.now(UTC)
    for src in (IncidentStatus.draft, IncidentStatus.failed):
        if await _transition(
            session, incident_id, src, status=IncidentStatus.sent.value, sent_at=now, delivery=None
        ):
            return True
    cur = await session.get(Incident, incident_id, populate_existing=True)
    if cur is None or cur.status != IncidentStatus.sent.value or cur.sent_at is None:
        return False
    stale = cur.sent_at < now - timedelta(seconds=settings.alert_send_lease)
    d = cur.delivery
    if d is None:
        if not stale:  # diffusion peut-être encore en cours
            return False
    elif any((d.get(k) or {}).get("ok") for k in ("tts", "teams")):
        return False
    # compare-and-swap sur sent_at : deux renvois concurrents ne peuvent pas gagner tous les deux
    res = await session.execute(
        update(Incident)
        .where(
            Incident.id == incident_id,
            Incident.status == IncidentStatus.sent.value,
            Incident.sent_at == cur.sent_at,
        )
        .values(sent_at=now, delivery=None)
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        return False
    return True


async def _deliver_tts(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    incident: Incident,
    user: User,
    request: Request | None,
) -> dict[str, Any]:
    if not settings.ha_configured:
        raise AlertError(503, "home assistant not configured")
    speakers = list(
        (await session.scalars(select(Speaker).where(Speaker.is_active.is_(True)))).all()
    )
    if not speakers:
        raise AlertError(409, "no active speaker")
    ann = ha_service.build_announcement(
        settings,
        text=incident.message,
        speakers=speakers,
        is_test=incident.is_test,
        priority=Priority.alert,
        author=user,
    )
    await ha_service.submit(session, redis, ann, request=request)
    incident.announcement_id = ann.id
    return {"ok": True, "announcement_id": str(ann.id), "speakers": len(speakers)}


async def _deliver_teams(
    settings: Settings, notifier: TeamsNotifier, incident: Incident
) -> dict[str, Any]:
    notice = IncidentNotice(str(incident.id), incident.title, incident.message, incident.is_test)
    detail = await asyncio.wait_for(notifier.notify(notice), timeout=settings.teams_timeout + 5)
    return {"ok": True, **detail}


async def send(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    notifier: TeamsNotifier,
    incident_id: uuid.UUID,
    user: User,
    request: Request | None = None,
) -> Incident:
    """draft|failed -> sent (atomique) puis TTS et Teams indépendants ; chaque échec est consigné.

    Si aucun canal n'aboutit, l'incident passe en `failed` et peut être renvoyé. Un incident
    `sent` sans `delivery` (crash en cours de diffusion) est repris après `alert_send_lease`.
    """
    if not await _claim(session, settings, incident_id):
        await session.rollback()
        raise AlertError(409, "incident is not sendable")
    audit.record(
        session,
        "incident_send",
        request=request,
        user_id=user.id,
        actor=user.email,
        detail={"incident_id": str(incident_id)},
    )
    await session.commit()
    incident = await session.get(Incident, incident_id, populate_existing=True)
    assert incident is not None

    async def tts() -> dict[str, Any]:
        try:
            return await _deliver_tts(session, redis, settings, incident, user, request)
        except AlertError as exc:
            return {"ok": False, "error": exc.detail}
        except Exception as exc:
            log.exception("incident %s: tts failed", incident_id)
            return {"ok": False, "error": f"{type(exc).__name__}"}

    async def teams() -> dict[str, Any]:
        try:
            return await _deliver_teams(settings, notifier, incident)
        except Exception as exc:
            log.exception("incident %s: teams failed", incident_id)
            return {"ok": False, "error": str(exc) or type(exc).__name__}

    tts_res, teams_res = await asyncio.gather(tts(), teams())
    delivery = {"tts": tts_res, "teams": teams_res}
    any_ok = bool(tts_res.get("ok") or teams_res.get("ok"))
    values = {
        "delivery": delivery,
        "announcement_id": incident.announcement_id,
        "status": (IncidentStatus.sent if any_ok else IncidentStatus.failed).value,
    }
    for attempt in (1, 2):  # la session peut être en échec (rollback requis) : on réessaie une fois
        try:
            await session.execute(
                update(Incident).where(Incident.id == incident_id).values(**values)
            )
            audit.record(
                session,
                "incident_delivery",
                request=request,
                user_id=user.id,
                actor=user.email,
                detail={
                    "incident_id": str(incident_id),
                    "tts_ok": bool(tts_res.get("ok")),
                    "teams_ok": bool(teams_res.get("ok")),
                },
            )
            await session.commit()
            break
        except SQLAlchemyError:
            await session.rollback()
            if attempt == 2:
                log.exception("incident %s: delivery result not persisted", incident_id)
                raise
    refreshed = await session.get(Incident, incident_id, populate_existing=True)
    assert refreshed is not None
    return refreshed


async def close(
    session: AsyncSession, incident_id: uuid.UUID, user: User, request: Request | None = None
) -> Incident:
    closing = {
        "status": IncidentStatus.closed.value,
        "closed_at": datetime.now(UTC),
        "closed_by_email": user.email,
    }
    if not (
        await _transition(session, incident_id, IncidentStatus.sent, **closing)
        or await _transition(session, incident_id, IncidentStatus.failed, **closing)
    ):
        raise AlertError(409, "incident is not sent")
    audit.record(
        session,
        "incident_close",
        request=request,
        user_id=user.id,
        actor=user.email,
        detail={"incident_id": str(incident_id)},
    )
    await session.commit()
    incident = await session.get(Incident, incident_id, populate_existing=True)
    assert incident is not None
    return incident
