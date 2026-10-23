import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts import autoremind, links, report_pdf, responses, service, webhook
from app.alerts.roster import RosterProvider, get_roster_provider
from app.alerts.teams import IncidentNotice, TeamsError, TeamsNotifier, get_notifier
from app.auth import audit
from app.auth.deps import get_session, require_role
from app.config import Settings, get_settings
from app.ha.router import get_redis
from app.models import (
    AlertTemplate,
    Announcement,
    AuditLog,
    Incident,
    IncidentType,
    Role,
    Student,
    User,
)

staff_up = require_role(Role.admin, Role.staff)
admin_only = require_role(Role.admin)

log = logging.getLogger("sound.alerts")

router = APIRouter(prefix="/incidents", tags=["incidents"])


def get_teams_notifier(settings: Settings = Depends(get_settings)) -> TeamsNotifier:
    return get_notifier(settings)


def get_webhook(settings: Settings = Depends(get_settings)) -> webhook.WebhookClient:
    return webhook.get_webhook(settings)


def get_roster(settings: Settings = Depends(get_settings)) -> RosterProvider:
    return get_roster_provider(settings)


class IncidentIn(BaseModel):
    type: IncidentType
    title: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1)
    is_test: bool = False
    auto_remind_interval: int | None = Field(default=None, ge=0, le=86400)
    auto_remind_max: int | None = Field(default=None, ge=0, le=50)

    @field_validator("title", "message")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("ne doit pas être vide")
        return v


class AutoRemindIn(BaseModel):
    """Surcharge par incident ; `null` = valeur globale, 0 = relances désactivées."""

    interval: int | None = Field(default=None, ge=0, le=86400)
    max: int | None = Field(default=None, ge=0, le=50)


class SendIn(BaseModel):
    confirm: str = Field(max_length=64)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if isinstance(d, datetime) else None


def _out(i: Incident, settings: Settings | None = None) -> dict[str, Any]:
    interval, mx = autoremind.effective(i, settings or get_settings())
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
        "auto_remind": {
            "interval": interval,
            "max": mx,
            "count": i.auto_remind_count,
            "last_at": _iso(i.auto_remind_last_at),
            "overridden": i.auto_remind_interval is not None or i.auto_remind_max is not None,
        },
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


def _dedup_key(user: User, type_: str, is_test: bool) -> str:
    return f"dedup:incidents:from-template:{user.id}:{type_}:{int(is_test)}"


async def _dedup_acquire(redis: Redis, user: User, type_: str, is_test: bool) -> str | None:
    """Verrou 15 s anti-doublon. Fail-open si Redis est indisponible. Renvoie la clé posée."""
    key = _dedup_key(user, type_, is_test)
    try:
        ok = await redis.set(key, "1", nx=True, ex=15)
    except Exception:
        log.warning("dedup lock unavailable", exc_info=True)
        return None
    if not ok:
        raise HTTPException(409, "envoi identique déjà en cours/récent")
    return key


async def _dedup_release(redis: Redis, key: str | None) -> None:
    if key is None:
        return
    try:
        await redis.delete(key)
    except Exception:
        log.warning("dedup lock release failed", exc_info=True)


async def _get(session: AsyncSession, incident_id: uuid.UUID) -> Incident:
    i = await session.get(Incident, incident_id)
    if i is None:
        raise HTTPException(404, "incident not found")
    return i


async def _create_draft(
    session: AsyncSession,
    settings: Settings,
    request: Request,
    user: User,
    *,
    type_: str,
    title: str,
    message: str,
    is_test: bool,
    auto_remind_interval: int | None = None,
    auto_remind_max: int | None = None,
) -> Incident:
    if len(message) > settings.announcement_max_length:
        raise HTTPException(422, f"message too long (max {settings.announcement_max_length})")
    i = Incident(
        id=uuid.uuid4(),
        type=type_,
        title=title,
        message=message,
        is_test=is_test,
        auto_remind_interval=auto_remind_interval,
        auto_remind_max=auto_remind_max,
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
    return i


async def _confirmed_send(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    notifier: TeamsNotifier,
    request: Request,
    user: User,
    incident: Incident,
    confirm: str,
) -> dict[str, Any]:
    if confirm != service.expected_confirm(settings, incident):
        audit.record(
            session,
            "incident_send_refused",
            request=request,
            user_id=user.id,
            actor=user.email,
            detail={"incident_id": str(incident.id)},
        )
        await session.commit()
        raise HTTPException(422, "confirmation text mismatch")
    try:
        sent = await service.send(session, redis, settings, notifier, incident.id, user, request)
    except service.AlertError as e:
        raise HTTPException(e.status, e.detail) from None
    out = _out(sent)
    if sent.status == "failed":
        raise HTTPException(
            502, "alert delivery failed on all channels (tts, teams): retry possible"
        )
    return out


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
    i = await _create_draft(
        session,
        settings,
        request,
        user,
        type_=body.type.value,
        title=body.title,
        message=body.message,
        is_test=body.is_test,
        auto_remind_interval=body.auto_remind_interval,
        auto_remind_max=body.auto_remind_max,
    )
    return _out(i)


class FromTemplateIn(BaseModel):
    type: IncidentType
    is_test: bool = False
    confirm: str = Field(max_length=64)


@router.post("/from-template")
async def send_from_template(
    body: FromTemplateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    notifier: TeamsNotifier = Depends(get_teams_notifier),
    user: User = Depends(staff_up),
) -> dict[str, Any]:
    """Brouillon depuis le modèle actif du type, puis envoi par le flux confirmé habituel.

    Le texte vient exclusivement du modèle (pas de texte libre) ; la confirmation est vérifiée
    avant toute création, avec les mêmes mots que l'envoi classique (ALERTE / TEST).
    """
    await _rate_limit(redis, settings, user, "create")
    await _rate_limit(redis, settings, user, "send")
    tpl = await session.scalar(
        select(AlertTemplate).where(
            AlertTemplate.type == body.type.value, AlertTemplate.is_active.is_(True)
        )
    )
    if tpl is None:
        raise HTTPException(404, "alert template not found or inactive")
    expected = settings.alert_test_confirm_text if body.is_test else settings.alert_confirm_text
    if body.confirm != expected:
        audit.record(
            session,
            "incident_send_refused",
            request=request,
            user_id=user.id,
            actor=user.email,
            detail={"type": tpl.type, "is_test": body.is_test},
        )
        await session.commit()
        raise HTTPException(422, "confirmation text mismatch")
    lock = await _dedup_acquire(redis, user, tpl.type, body.is_test)
    try:
        incident = await _create_draft(
            session,
            settings,
            request,
            user,
            type_=tpl.type,
            title=tpl.title,
            message=tpl.message,
            is_test=body.is_test,
        )
        await session.commit()
        return await _confirmed_send(
            session, redis, settings, notifier, request, user, incident, body.confirm
        )
    except BaseException:
        await _dedup_release(redis, lock)
        raise


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
    return await _confirmed_send(
        session, redis, settings, notifier, request, user, incident, body.confirm
    )


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


@router.delete("/{incident_id}", status_code=204)
async def delete_incident(
    incident_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> Response:
    i = await _get(session, incident_id)
    if i.status != "closed":
        raise HTTPException(409, "only closed incidents can be deleted")
    audit.record(
        session,
        "incident_delete",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={
            "incident_id": str(i.id),
            "title": i.title,
            "type": i.type,
            "is_test": i.is_test,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        },
    )
    await session.delete(i)
    return Response(status_code=204)


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


@router.get("/{incident_id}/report.pdf")
async def report_pdf_export(
    incident_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    roster: RosterProvider = Depends(get_roster),
    user: User = Depends(staff_up),
) -> Response:
    await _rate_limit(redis, settings, user, "report")
    incident = await _get(session, incident_id)
    data, _missing = await responses.report(session, roster, incident_id)
    ann = (
        await session.get(Announcement, incident.announcement_id)
        if incident.announcement_id
        else None
    )
    speakers = [
        str(t.get("name") or t.get("entity_id") or "") for t in (ann.targets if ann else [])
    ]
    reminders = list(
        (
            await session.scalars(
                select(AuditLog.ts)
                .where(
                    AuditLog.action.in_(("incident_remind", "auto_remind")),
                    AuditLog.detail["incident_id"].as_string() == str(incident_id),
                )
                .order_by(AuditLog.ts)
            )
        ).all()
    )
    now = datetime.now(UTC)
    pdf = report_pdf.build_pdf(
        report_pdf.ReportInput(
            incident=_out(incident),
            data=data,
            speakers=speakers,
            reminders=reminders,
            actor=user.email or "",
            retention_days=settings.response_retention_days,
            now=now,
        )
    )
    audit.record(
        session,
        "incident_report_export",
        request=request,
        user_id=user.id,
        actor=user.email,
        detail={"incident_id": str(incident_id)},
    )
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{report_pdf.safe_filename(incident_id, now)}"'
            ),
            "Cache-Control": "no-store",
        },
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
    wh: webhook.WebhookClient = Depends(get_webhook),
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
        if settings.links_active:  # nouveaux liens individuels aux seuls non-répondants
            detail = await webhook.remind(session, settings, wh, notice, missing)
        else:
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


@router.patch("/{incident_id}/auto-remind")
async def set_auto_remind(
    incident_id: uuid.UUID,
    body: AutoRemindIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    user: User = Depends(staff_up),
) -> dict[str, Any]:
    await _rate_limit(redis, settings, user, "auto-remind")
    incident = await _get(session, incident_id)
    if incident.status == "closed":
        raise HTTPException(409, "incident is closed")
    incident.auto_remind_interval = body.interval
    incident.auto_remind_max = body.max
    incident.auto_remind_failures = 0  # reprise explicite après un arrêt sur échecs
    audit.record(
        session,
        "incident_auto_remind_config",
        request=request,
        user_id=user.id,
        actor=user.email,
        detail={"incident_id": str(incident_id), "interval": body.interval, "max": body.max},
    )
    await session.flush()
    return _out(incident, settings)


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


class LinkIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)


@router.post("/{incident_id}/links")
async def create_student_link(
    incident_id: uuid.UUID,
    body: LinkIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    """Génère un nouveau lien pour un étudiant (test / dépannage). Le jeton n'est montré qu'ici."""
    await _rate_limit(redis, settings, admin, "link")
    incident = await _get(session, incident_id)
    if incident.status != "sent":
        raise HTTPException(409, "incident is not sent")
    student = await session.scalar(
        select(Student).where(Student.email == body.email.strip().lower())
    )
    if student is None:
        raise HTTPException(404, "student not found")
    issued = (await links.create_links(session, settings, incident_id, [student]))[0]
    audit.record(
        session,
        "response_link_create",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"incident_id": str(incident_id), "student_id": str(student.id)},
    )
    return {"url": issued.url, "expires_in_hours": settings.response_link_ttl_hours}
