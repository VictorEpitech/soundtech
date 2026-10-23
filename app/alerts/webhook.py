"""Diffusion par webhook (Power Automate) : un lien de réponse individuel par destinataire.

Charge utile (JSON, par lot de `ALERT_WEBHOOK_BATCH` destinataires) :
  {"kind": "alert"|"reminder", "batch": i, "batches": n,
   "incident": {id, title, message, type, is_test},
   "recipients": [{email, first_name, last_name, url, card}]}

Choix : UNE carte Adaptive Card 1.4 par destinataire (`recipients[].card`), car les 3 boutons
`Action.OpenUrl` portent le lien personnel ; le flow n'a qu'à poster `string(item().card)`.
Chaque carte reste sous `ALERT_WEBHOOK_CARD_MAX_BYTES` (limite Teams : 28 KB).

L'URL du webhook est un secret (elle contient la signature du flow) : jamais journalisée.
Un échec ne lève jamais vers l'appelant du TTS : ce module est appelé par le worker uniquement.
"""

import asyncio
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.alerts import links, watch
from app.alerts.roster import Member
from app.alerts.teams import RESPONSE_CHOICES, IncidentNotice, TeamsError
from app.auth import audit
from app.config import Settings
from app.models import Incident, IncidentNotification, ResponseLink, Student

log = logging.getLogger("sound.alerts.webhook")

BACKOFF = (0.5, 2.0, 5.0)
MESSAGE_MAX = 4000

# action Adaptive Card -> style (positive = vert, destructive = rouge)
_STYLES = {"safe_campus": "positive", "safe_offsite": "positive", "danger": "destructive"}


_SECRET_URLS: set[str] = set()


class _HideSecretUrl(logging.Filter):
    """httpx journalise « POST <url> » en INFO : l'URL du webhook (signature) ne doit pas sortir."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(u in msg for u in _SECRET_URLS)


logging.getLogger("httpx").addFilter(_HideSecretUrl())


class WebhookError(TeamsError):
    """Hérite de TeamsError pour réutiliser la gestion d'erreur des relances existantes."""


def build_card(
    settings: Settings, title: str, message: str, is_test: bool, url: str, reminder: bool = False
) -> dict[str, Any]:
    """Adaptive Card 1.4 : texte + 3 `Action.OpenUrl` vers la page de réponse personnelle."""
    tag = settings.test_prefix.rstrip(". ").upper() or "TEST"
    head = f"[{tag}] {title}" if is_test else title
    if reminder:
        head = f"RAPPEL : {head}"
    limit = settings.alert_webhook_card_max_bytes
    msg = message[:MESSAGE_MAX]
    while True:
        card: dict[str, Any] = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": head,
                    "weight": "Bolder",
                    "size": "Large",
                    "color": "Attention",
                    "wrap": True,
                },
                {"type": "TextBlock", "text": msg, "wrap": True},
                {
                    "type": "TextBlock",
                    "text": "Indiquez votre situation (un clic) :",
                    "isSubtle": True,
                    "wrap": True,
                },
            ],
            "actions": [
                {"type": "Action.OpenUrl", "title": t, "url": url, "style": _STYLES[v]}
                for v, t in RESPONSE_CHOICES
            ],
        }
        if len(json.dumps(card, ensure_ascii=False).encode()) <= limit or len(msg) <= 200:
            return card
        msg = msg[: len(msg) // 2]


@dataclass(frozen=True)
class Recipient:
    student_id: uuid.UUID
    email: str
    first_name: str
    last_name: str
    url: str


class WebhookClient(Protocol):
    @property
    def configured(self) -> bool: ...

    async def post(self, payload: dict[str, Any]) -> None:
        """Lève `WebhookError` après épuisement des tentatives."""
        ...


class LogWebhook:
    """Défaut sans configuration : rien n'est envoyé (mode journal)."""

    configured = False

    async def post(self, payload: dict[str, Any]) -> None:
        log.warning("webhook not configured: batch not sent")


class HttpWebhook:
    configured = True

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._s = settings
        self._transport = transport
        self._sleep = sleep
        if url := settings.alert_webhook_url.get_secret_value():
            _SECRET_URLS.add(url)

    async def post(self, payload: dict[str, Any]) -> None:
        s = self._s
        url = s.alert_webhook_url.get_secret_value()
        reason = "unknown"
        for attempt in range(s.alert_webhook_retries):
            if attempt:
                await self._sleep(BACKOFF[min(attempt - 1, len(BACKOFF) - 1)])
            try:
                async with httpx.AsyncClient(
                    timeout=s.alert_webhook_timeout, transport=self._transport
                ) as h:
                    r = await h.post(url, json=payload)
                if r.status_code < 300:
                    return
                reason = f"http {r.status_code}"
                if r.status_code < 500 and r.status_code not in (408, 429):
                    break  # erreur définitive (URL révoquée, corps refusé) : inutile de réessayer
            except httpx.HTTPError as exc:  # le message httpx peut contenir l'URL : type seul
                reason = type(exc).__name__
        raise WebhookError(f"webhook {reason}")


def get_webhook(settings: Settings) -> WebhookClient:
    return HttpWebhook(settings) if settings.webhook_configured else LogWebhook()


def _payload(
    incident: Incident, kind: str, batch: int, batches: int, recipients: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "kind": kind,
        "batch": batch,
        "batches": batches,
        "incident": {
            "id": str(incident.id),
            "title": incident.title,
            "message": incident.message,
            "type": incident.type,
            "is_test": incident.is_test,
        },
        "recipients": recipients,
    }


async def send_links(
    session: AsyncSession,
    settings: Settings,
    client: WebhookClient,
    incident: Incident,
    students: list[Student],
    kind: str = "alert",
) -> dict[str, Any]:
    """Crée les liens et poste par lots. Ne lève pas : renvoie les compteurs et les ids envoyés.

    Les liens d'un lot dont l'envoi échoue sont supprimés (personne ne les connaît).
    """
    n = len(students)
    size = settings.alert_webhook_batch
    batches = math.ceil(n / size) if n else 0
    sent: list[str] = []
    failed = 0
    for i in range(batches):
        chunk = students[i * size : (i + 1) * size]
        made = await links.create_links(session, settings, incident.id, chunk)
        await session.commit()
        recipients = [
            {
                "email": it.student.email,
                "first_name": it.student.first_name,
                "last_name": it.student.last_name,
                "url": it.url,
                "card": build_card(
                    settings, incident.title, incident.message, incident.is_test, it.url,
                    reminder=kind == "reminder",
                ),
            }
            for it in made
        ]  # fmt: skip
        try:
            await client.post(_payload(incident, kind, i + 1, batches, recipients))
        except WebhookError as exc:
            log.warning("webhook batch %d/%d failed: %s", i + 1, batches, exc)
            failed += len(chunk)
            await session.execute(
                delete(ResponseLink).where(ResponseLink.id.in_([it.link_id for it in made]))
            )
            await session.commit()
            continue
        sent.extend(links.student_aad_id(it.student) for it in made)
    return {
        "recipients": n,
        "sent": len(sent),
        "failed": failed,
        "batches": batches,
        "sent_ids": sent,
    }


async def _students(session: AsyncSession, ids: list[str]) -> list[Student]:
    wanted: list[uuid.UUID] = []
    for i in ids:
        try:
            wanted.append(uuid.UUID(i))
        except ValueError:
            continue
    if not wanted:
        return []
    rows = (await session.scalars(select(Student).where(Student.id.in_(wanted)))).all()
    return sorted(rows, key=lambda s: s.email)


async def dispatch_initial(
    session: AsyncSession, settings: Settings, client: WebhookClient, incident: Incident
) -> dict[str, Any]:
    """Envoi initial à tout l'annuaire. Renvoie les compteurs (sans lever)."""
    students = list((await session.scalars(select(Student).order_by(Student.email))).all())
    return await send_links(session, settings, client, incident, students, "alert")


def remind_timeout(settings: Settings, targets: int) -> float:
    batches = max(1, math.ceil(targets / settings.alert_webhook_batch))
    return batches * (settings.alert_webhook_timeout * settings.alert_webhook_retries + 10) + 10


async def remind(
    session: AsyncSession,
    settings: Settings,
    client: WebhookClient,
    notice: IncidentNotice,
    missing: list[Member],
) -> dict[str, Any]:
    """Relance les non-répondants avec de nouveaux liens (équivalent de `TeamsNotifier.remind`).

    Lève `WebhookError` seulement si rien n'est parti ; un envoi partiel est renvoyé en détail.
    """
    if not client.configured:
        log.warning("webhook not configured: reminder for incident %s not sent", notice.incident_id)
        return {"skipped": "webhook not configured", "targets": len(missing)}
    incident = await session.get(Incident, uuid.UUID(notice.incident_id))
    if incident is None:
        raise WebhookError("incident not found")
    students = await _students(session, [m.aad_id for m in missing])
    res = await send_links(session, settings, client, incident, students, "reminder")
    if students and res["sent"] == 0:
        raise WebhookError("webhook reminder failed")
    return {
        "targets": len(missing),
        "mentioned": res["sent"],
        "failed": res["failed"],
        "partial": res["failed"] > 0,
    }


NOTIF_KIND = "webhook"
NOTIF_KEY = "webhook_initial"
PICKUP_WINDOW = timedelta(hours=1)  # pas de rejeu des vieux incidents


async def tick(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: WebhookClient,
    now: datetime | None = None,
) -> int:
    """Envoi initial des liens pour les incidents `sent` récents ; idempotent (claim en base).

    Un envoi partiel est considéré comme traité (pas de doublons) : la relance cible ensuite
    les non-répondants. Si aucun lot ne part, l'envoi est retenté (`watch.MAX_ATTEMPTS`).
    """
    if not client.configured:
        return 0
    now = now or datetime.now(UTC)
    async with sm() as s:
        incidents = list(
            (
                await s.scalars(
                    select(Incident).where(
                        Incident.status == "sent",
                        Incident.sent_at.is_not(None),
                        Incident.sent_at >= now - PICKUP_WINDOW,
                    )
                )
            ).all()
        )
        done = set()
        if incidents:
            done = set(
                (
                    await s.scalars(
                        select(IncidentNotification.incident_id).where(
                            IncidentNotification.key == NOTIF_KEY,
                            IncidentNotification.incident_id.in_([i.id for i in incidents]),
                            (IncidentNotification.delivered_at.is_not(None))
                            | (IncidentNotification.attempts >= watch.MAX_ATTEMPTS),
                        )
                    )
                ).all()
            )
    handled = 0
    for inc in incidents:
        if inc.id in done:
            continue
        try:
            async with sm() as s:
                nid = await watch._claim(s, inc.id, NOTIF_KIND, NOTIF_KEY, now)
            if nid is None:
                continue
            async with sm() as s:
                cur = await s.get(Incident, inc.id, populate_existing=True)
                if cur is None or cur.status != "sent":
                    continue
                res = await dispatch_initial(s, settings, client, cur)
                ok = res["recipients"] == 0 or res["sent"] > 0
                audit.record(
                    s,
                    "webhook_dispatch",
                    actor="worker",
                    detail={
                        "incident_id": str(inc.id),
                        "ok": ok,
                        **{k: res[k] for k in ("recipients", "sent", "failed", "batches")},
                    },
                )
                if ok:
                    await s.execute(
                        update(IncidentNotification)
                        .where(IncidentNotification.id == nid)
                        .values(delivered_at=datetime.now(UTC))
                    )
                await s.commit()
                handled += int(ok)
        except Exception:
            log.exception("webhook dispatch %s crashed", inc.id)
    return handled
