"""Page publique de réponse par lien individuel : `GET|POST /r/{token}`, sans authentification.

Le jeton fait office de capacité : 256 bits aléatoires, comparé par hash (jamais stocké ni
journalisé en clair). Tout refus renvoie la même page neutre (404 inconnu/non diffusé, 410
expiré/clos) : aucune information sur l'incident ni sur l'étudiant. Pas de cookie, donc pas
de CSRF ; pas de script ; jamais de cache ni d'indexation.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts import links, responses
from app.alerts.teams import RESPONSE_CHOICES
from app.auth import audit
from app.auth.deps import get_session
from app.ha.router import get_redis
from app.models import Response as ResponseRow
from app.models import ResponseLink
from app.ratelimit import per_ip

log = logging.getLogger("sound.alerts.public")

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

router = APIRouter(
    prefix="/r",
    tags=["public"],
    include_in_schema=False,
    dependencies=[Depends(per_ip("rlink", "rl_response_link_limit"))],
)

MAX_BODY = 16384  # 500 caractères accentués encodés en formulaire ~ 4,5 Ko
HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}
LABELS = dict(RESPONSE_CHOICES)


def _page(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
    resp = templates.TemplateResponse(request, name, ctx, status_code=status, headers=HEADERS)
    return resp


def _neutral(request: Request, state: links.LinkState) -> HTMLResponse:
    status = 410 if state in (links.LinkState.expired, links.LinkState.closed) else 404
    return _page(request, "reply_gone.html", status)


async def _current(session: AsyncSession, res: links.Resolved) -> tuple[str, str] | None:
    """(statut, précision) déjà enregistrés pour cet étudiant, lus en base (pas d'objet caché)."""
    assert res.incident is not None and res.student is not None
    row = (
        await session.execute(
            select(ResponseRow.status, ResponseRow.text).where(
                ResponseRow.incident_id == res.incident.id,
                ResponseRow.user_aad_id == links.student_aad_id(res.student),
            )
        )
    ).first()
    return (row[0], row[1] or "") if row else None


def _form(
    request: Request,
    res: links.Resolved,
    current: tuple[str, str] | None,
    *,
    saved: bool = False,
    error: str = "",
    status: int = 200,
) -> HTMLResponse:
    assert res.incident is not None
    return _page(
        request,
        "reply.html",
        status,
        incident=res.incident,
        choices=RESPONSE_CHOICES,
        current_label=LABELS.get(current[0]) if current else None,
        current_text=(current[1] if current else "") or "",
        saved=saved,
        error=error,
    )


@router.get("/{token}")
async def reply_page(
    token: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    res = await links.resolve(session, token)
    if res.state is not links.LinkState.ok:
        return _neutral(request, res.state)
    return _form(request, res, await _current(session, res))


@router.post("/{token}")
async def reply_submit(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> Response:
    res = await links.resolve(session, token)
    if res.state is not links.LinkState.ok:
        return _neutral(request, res.state)
    assert res.incident and res.student and res.link
    raw = await request.body()
    current = await _current(session, res)
    if len(raw) > MAX_BODY:
        return _form(request, res, current, error="Message trop long.", status=413)
    form = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    status = (form.get("status") or [""])[0]
    if status not in responses.STATUSES:
        return _form(request, res, current, error="Choisissez une réponse.", status=422)
    text = responses.clean_text((form.get("details") or [""])[0])
    aad = links.student_aad_id(res.student)
    await responses.record(
        session,
        res.incident.id,
        aad_id=aad,
        name=links.student_name(res.student),
        email=res.student.email,
        status=status,
        text=text,
    )
    await session.execute(
        update(ResponseLink).where(ResponseLink.id == res.link.id).values(used_at=datetime.now(UTC))
    )
    audit.record(
        session,
        "incident_response",
        request=request,
        actor=aad,
        detail={
            "incident_id": str(res.incident.id),
            "aad_id": aad,
            "status": status,
            "has_text": text is not None,
            "channel": "link",
        },
    )
    await session.commit()
    await responses.publish(redis, res.incident.id, status)
    return _form(request, res, (status, text or ""), saved=True)
