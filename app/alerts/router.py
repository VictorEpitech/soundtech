import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts import responses, service
from app.alerts.roster import RosterProvider, get_roster_provider
from app.alerts.teams import IncidentNotice, TeamsError, TeamsNotifier, get_notifier
from app.auth import audit
from app.auth.deps import get_session, require_role
from app.config import Settings, get_settings
from app.ha.router import get_redis
from app.models import Incident, IncidentType, Role, User

staff_up = require_role(Role.admin, Role.staff)

log = logging.getLogger("sound.alerts")

router = APIRouter(prefix="/incidents", tags=["incidents"])


def get_teams_notifier(settings: Settings = Depends(get_settings)) -> TeamsNotifier:
    return get_notifier(settings)


def get_roster(settings: Settings = Depends(get_settings)) -> RosterProvider:
    return get_roster_provider(settings)


class IncidentIn(BaseModel):
    type: IncidentType
    title: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1)
    is_test: bool = False

    @field_validator("title", "message")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ne doit pas être vide")
        return v


class SendIn(BaseModel):
    confirm: str = Field(max_length=64)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if isinstance(d, datetime) else None


def _out(i: Incident) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "type": i.type,
        "title": i.title,
        "message": i.message,
        "is_test": i.is_test,
        "status": i.status,
        "created_by_email": i.created_by_email,
        "closed_by_email": i.closed_by_email,
        "announcement_id": str(i.announcement_id) if i.announcement_id else None,
        "delivery": i.delivery,
        "created_at": _iso(i.created_at),
        "sent_at": _iso(i.sent_at),
        "closed_at": _iso(i.closed_at),
    }


async def _rate_limit(redis: Redis, settings: Settings, user: User, action: str) -> None:
    """Fenêtre fixe par utilisateur. Fail-open si Redis est indisponible (une alerte prime)."""
    if settings.alert_rate_limit <= 0:
        return
    key = f"rl:incidents:{action}:{user.id}"
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, settings.alert_rate_window)
    except Exception:
        log.warning("rate limit unavailable", exc_info=True)
        return
    if n > settings.alert_rate_limit:
        raise HTTPException(429, "too many requests")


async def _get(session: AsyncSession, incident_id: uuid.UUID) -> Incident:
    i = await session.get(Incident, incident_id)
    if i is None:
        raise HTTPException(404, "incident not found")
    return i


@router.post("", status_code=201)
async def create_incident(
    body: IncidentIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    user: User = Depends(staff_up),
) -> dict[str, Any]:
    await _rate_limit(redis, settings, user, "create")
    if len(body.message) > settings.announcement_max_length:
        raise HTTPException(422, f"message too long (max {settings.announcement_max_length})")
    i = Incident(
        id=uuid.uuid4(),
        type=body.type.value,
        title=body.title,
        message=body.message,
        is_test=body.is_test,
        status="draft",
        created_by=user.id,
        created_by_email=user.email,
    )
    session.add(i)
    await session.flush()
    audit.record(
        session,
        "incident_create",
        request=request,
        user_id=user.id,
        actor=user.email,
        detail={"incident_id": str(i.id), "type": i.type, "is_test": i.is_test},
    )
    await session.refresh(i)
    return _out(i)


@router.get("")
async def list_incidents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> list[dict[str, Any]]:
    q = select(Incident).order_by(Incident.created_at.desc()).limit(limit).offset(offset)
    return [_out(i) for i in (await session.scalars(q)).all()]


@router.get("/{incident_id}")
async def get_incident(
    incident_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    return _out(await _get(session, incident_id))


@router.post("/{incident_id}/send")
async def send_incident(
    incident_id: uuid.UUID,
    body: SendIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    notifier: TeamsNotifier = Depends(get_teams_notifier),
    user: User = Depends(staff_up),
) -> dict[str, Any]:
    await _rate_limit(redis, settings, user, "send")
    incident = await _get(session, incident_id)
    if body.confirm != service.expected_confirm(settings, incident):
        audit.record(
            session,
            "incident_send_refused",
            request=request,
            user_id=user.id,
            actor=user.email,
            detail={"incident_id": str(incident_id)},
        )
        await session.commit()
        raise HTTPException(422, "confirmation text mismatch")
    try:
        sent = await service.send(session, redis, settings, notifier, incident_id, user, request)
    except service.AlertError as e:
        raise HTTPException(e.status, e.detail) from None
    out = _out(sent)
    if sent.status == "failed":
        raise HTTPException(
            502, "alert delivery failed on all channels (tts, teams): retry possible"
        )
    return out


@router.post("/{incident_id}/close")
async def close_incident(
    incident_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(staff_up),
) -> dict[str, Any]:
    await _get(session, incident_id)
    try:
        return _out(await service.close(session, incident_id, user, request))
    except service.AlertError as e:
        raise HTTPException(e.status, e.detail) from None


@router.get("/{incident_id}/responses")
async def incident_responses(
    incident_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    roster: RosterProvider = Depends(get_roster),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    await _get(session, incident_id)
    data, _missing = await responses.report(session, roster, incident_id)
    return data


@router.get("/{incident_id}/export.csv")
async def export_csv(
    incident_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    roster: RosterProvider = Depends(get_roster),
    user: User = Depends(staff_up),
) -> Response:
    await _get(session, incident_id)
    data, _missing = await responses.report(session, roster, incident_id)
    if data["roster"]["error"]:
        raise HTTPException(502, "roster unavailable")
    audit.record(
        session,
        "incident_export",
        request=request,
        user_id=user.id,
        actor=user.email,
        detail={"incident_id": str(incident_id), "rows": len(data["respondents"])},
    )
    return Response(
        responses.to_csv(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="incident-{incident_id}.csv"'},
    )


@router.post("/{incident_id}/remind")
async def remind(
    incident_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    notifier: TeamsNotifier = Depends(get_teams_notifier),
    roster: RosterProvider = Depends(get_roster),
    user: User = Depends(staff_up),
) -> dict[str, Any]:
    await _rate_limit(redis, settings, user, "remind")
    incident = await _get(session, incident_id)
    if incident.status != "sent":
        raise HTTPException(409, "incident is not sent")
    if not roster.configured:
        raise HTTPException(409, "roster not configured (ROSTER_GROUP_ID)")
    data, missing = await responses.report(session, roster, incident_id)
    if data["roster"]["error"]:
        raise HTTPException(502, "roster unavailable")
    if not missing:
        return {"reminded": 0}
    key = f"remind:cooldown:{incident_id}"
    try:
        fresh = settings.remind_cooldown <= 0 or await redis.set(
            key, "1", nx=True, ex=settings.remind_cooldown
        )
    except Exception:
        log.warning("remind cooldown unavailable", exc_info=True)
        fresh = True
    if not fresh:
        raise HTTPException(429, "reminder already sent recently")
    notice = IncidentNotice(str(incident.id), incident.title, incident.message, incident.is_test)
    try:
        detail = await notifier.remind(notice, missing)
    except TeamsError as e:
        try:
            await redis.delete(key)
        except Exception:
            log.warning("remind cooldown reset failed", exc_info=True)
        audit.record(
            session,
            "incident_remind_failed",
            request=request,
            user_id=user.id,
            actor=user.email,
            detail={"incident_id": str(incident_id), "error": str(e)},
        )
        await session.commit()
        raise HTTPException(502, "reminder delivery failed") from None
    audit.record(
        session,
        "incident_remind",
        request=request,
        user_id=user.id,
        actor=user.email,
        detail={"incident_id": str(incident_id), "targets": len(missing)},
    )
    return {**detail, "reminded": detail.get("mentioned", len(missing))}


@router.get("/{incident_id}/stream")
async def stream(
    incident_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    user: User = Depends(staff_up),
) -> StreamingResponse:
    await _get(session, incident_id)
    await session.close()  # ne pas garder une connexion DB pendant toute la durée du flux
    if not await responses.acquire_stream_slot(redis, user.id):
        raise HTTPException(429, "too many concurrent streams")
    return StreamingResponse(
        responses.event_stream(
            redis,
            incident_id,
            request.is_disconnected,
            on_close=lambda: responses.release_stream_slot(redis, user.id),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
