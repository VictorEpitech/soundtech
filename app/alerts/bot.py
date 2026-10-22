"""Webhook Bot Framework : réception des Action.Execute `incident_response` depuis Teams."""

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts import botauth, responses
from app.alerts.teams import RESPONSE_VERB
from app.auth import audit
from app.auth.deps import get_session
from app.config import Settings, get_settings
from app.ha.router import get_redis
from app.models import Incident, IncidentStatus
from app.ratelimit import client_ip, hit

log = logging.getLogger("sound.alerts.bot")

router = APIRouter(prefix="/teams", tags=["teams"])

MAX_BODY = 64 * 1024
MSG = "application/vnd.microsoft.activity.message"
ERR = "application/vnd.microsoft.error"


def _ok(text: str) -> JSONResponse:
    return JSONResponse({"statusCode": 200, "type": MSG, "value": text})


def _err(code: int, message: str) -> JSONResponse:
    """Erreur d'invoke : HTTP 200, le statut est porté par le corps (contrat Action.Execute)."""
    return JSONResponse(
        {"statusCode": code, "type": ERR, "value": {"code": "BadRequest", "message": message}}
    )


def _aad_id(v: Any) -> str | None:
    try:
        return str(uuid.UUID(str(v))).lower()
    except (ValueError, AttributeError, TypeError):
        return None


@router.post("/messages")
async def messages(
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    if not settings.teams_app_id:
        raise HTTPException(503, "teams bot not configured")
    await hit(
        request, f"rl:teams:ip:{client_ip(request)}", settings.rl_webhook_limit, settings.rl_window
    )
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(400, "invalid content-length") from None
    if declared > MAX_BODY:
        raise HTTPException(413, "payload too large")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY:
            raise HTTPException(413, "payload too large")
    raw = bytes(buf)
    try:
        activity = json.loads(raw)
        if not isinstance(activity, dict):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "invalid activity") from None
    try:
        await botauth.verify(
            settings, request.headers.get("authorization"), activity.get("serviceUrl")
        )
    except botauth.BotAuthError as e:
        log.warning("bot auth refused: %s", e)
        raise HTTPException(401, "invalid bot token") from None

    tenant = settings.teams_tenant_id
    if tenant:
        channel_data = activity.get("channelData")
        tenant_obj = channel_data.get("tenant") if isinstance(channel_data, dict) else None
        tid = tenant_obj.get("id") if isinstance(tenant_obj, dict) else None
        if not isinstance(tid, str) or tid.lower() != tenant.lower():
            log.warning("bot tenant mismatch")
            raise HTTPException(403, "tenant not allowed")

    if activity.get("type") != "invoke":
        return JSONResponse({})
    if activity.get("name") != "adaptiveCard/action":
        return _err(400, "unsupported invoke")

    value = activity.get("value") or {}
    action = (value.get("action") or {}) if isinstance(value, dict) else None
    if not isinstance(action, dict) or action.get("verb") != RESPONSE_VERB:
        return _err(400, "unsupported verb")
    data = action.get("data") or {}
    sender = activity.get("from") or {}
    if not isinstance(data, dict) or not isinstance(sender, dict):
        return _err(400, "invalid payload")
    aad = _aad_id(sender.get("aadObjectId"))
    if aad is None:
        return _err(400, "unidentified user")
    name = str(sender.get("name") or "")[:255]
    status = data.get("status") or data.get("statut")
    text = responses.clean_text(data.get("details") or data.get("texte") or data.get("text"))
    try:
        incident_id = uuid.UUID(str(data.get("incident_id")))
    except (ValueError, AttributeError, TypeError):
        return _err(400, "invalid incident")

    audit_detail = {"incident_id": str(incident_id), "aad_id": aad}
    incident = await session.get(Incident, incident_id)
    if incident is None or incident.status != IncidentStatus.sent.value:
        audit.record(
            session,
            "incident_response_rejected",
            request=request,
            actor=aad,
            detail={**audit_detail, "reason": "unknown" if incident is None else incident.status},
        )
        return _err(404 if incident is None else 409, "incident unknown or closed")
    if not isinstance(status, str) or status not in responses.STATUSES:
        return _err(400, "invalid status")

    await responses.record(
        session, incident_id, aad_id=aad, name=name, email=None, status=status, text=text
    )
    audit.record(
        session,
        "incident_response",
        request=request,
        actor=aad,
        detail={**audit_detail, "status": status, "has_text": text is not None},
    )
    await session.commit()
    await responses.publish(redis, incident_id, status)
    return _ok("Réponse enregistrée, merci.")
