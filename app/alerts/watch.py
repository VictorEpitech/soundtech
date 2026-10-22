"""Notifications admin/staff pendant un incident (cron du worker).

Déclencheurs : aucune réponse après N min, réponse « en danger » (une notification par réponse),
taux de réponse sous un seuil. Canal : Web Push, avec repli sur un message Teams 1:1 si le push
n'a atteint personne. Le contenu est fixe et générique : ni nom, ni texte libre.

Idempotence : une ligne `IncidentNotification` par (incident, clé) ; la prise en charge est un
UPDATE atomique qui incrémente `attempts` (plusieurs workers : un seul gagne). Une notification
non délivrée est retentée (au plus `MAX_ATTEMPTS` fois, espacées de `RETRY_AFTER`).
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import push
from app.alerts import responses
from app.alerts.roster import RosterProvider
from app.alerts.teams import TeamsNotifier
from app.config import Settings
from app.models import Incident, IncidentNotification, Response

log = logging.getLogger("sound.alerts.watch")

NO_RESPONSE = "no_response"
DANGER = "danger"
LOW_RATE = "low_rate"
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(seconds=30)
MAX_PER_TICK = 50

BODIES = {
    NO_RESPONSE: "Aucune réponse reçue — ouvrir le récap",
    DANGER: 'Réponse "en danger" reçue — ouvrir le récap',
    LOW_RATE: "Taux de réponse faible — ouvrir le récap",
}


def build_payload(
    kind: str, incident_id: uuid.UUID, notif_id: uuid.UUID, is_test: bool
) -> dict[str, str]:
    prefix = "[TEST] " if is_test else ""
    return {
        "title": f"{prefix}Sound — suivi d'incident",
        "body": BODIES[kind],
        "url": f"/alerts/{incident_id}",
        "tag": f"sound-{notif_id}",
    }


async def _candidates(
    s: AsyncSession,
    settings: Settings,
    roster: RosterProvider,
    incidents: list[Incident],
    now: datetime,
    done: set[tuple[uuid.UUID, str]],
) -> list[tuple[Incident, str, str]]:
    """(incident, kind, clé) dont la condition est remplie et pas encore délivrés."""
    out: list[tuple[Incident, str, str]] = []
    ids = [i.id for i in incidents]
    counts = {
        r[0]: r[1]
        for r in (
            await s.execute(
                select(Response.incident_id, func.count())
                .where(Response.incident_id.in_(ids))
                .group_by(Response.incident_id)
            )
        ).all()
    }
    for inc in incidents:
        assert inc.sent_at is not None
        age = now - inc.sent_at
        if (
            settings.notify_no_response_min > 0
            and age >= timedelta(minutes=settings.notify_no_response_min)
            and not counts.get(inc.id)
            and (inc.id, NO_RESPONSE) not in done
        ):
            out.append((inc, NO_RESPONSE, NO_RESPONSE))
        if (
            settings.notify_rate_threshold > 0
            and age >= timedelta(minutes=settings.notify_rate_after_min)
            and roster.configured
            and (inc.id, LOW_RATE) not in done
        ):
            data, _ = await responses.report(s, roster, inc.id)
            c = data["counts"]
            if (
                not data["roster"]["error"]
                and c["roster_total"] > 0
                and c["responded"] * 100 < settings.notify_rate_threshold * c["roster_total"]
            ):
                out.append((inc, LOW_RATE, LOW_RATE))
    by_id = {i.id: i for i in incidents}
    rows = (
        await s.execute(
            select(Response.incident_id, Response.id, Response.received_at)
            .where(Response.incident_id.in_(ids), Response.status == "danger")
            .order_by(Response.received_at)
        )
    ).all()
    for incident_id, rid, received_at in rows:
        key = f"{DANGER}:{rid}:{int(received_at.timestamp() * 1000)}"
        if (incident_id, key) not in done:
            out.append((by_id[incident_id], DANGER, key))
    return out


async def _claim(
    s: AsyncSession, incident_id: uuid.UUID, kind: str, key: str, now: datetime
) -> uuid.UUID | None:
    await s.execute(
        insert(IncidentNotification)
        .values(id=uuid.uuid4(), incident_id=incident_id, kind=kind, key=key)
        .on_conflict_do_nothing(constraint="uq_incident_notifications_key")
    )
    res = await s.execute(
        update(IncidentNotification)
        .where(
            IncidentNotification.incident_id == incident_id,
            IncidentNotification.key == key,
            IncidentNotification.delivered_at.is_(None),
            IncidentNotification.attempts < MAX_ATTEMPTS,
            (IncidentNotification.last_attempt_at.is_(None))
            | (IncidentNotification.last_attempt_at <= now - RETRY_AFTER),
        )
        .values(attempts=IncidentNotification.attempts + 1, last_attempt_at=now)
        .returning(IncidentNotification.id)
    )
    nid = res.scalar_one_or_none()
    await s.commit()
    return nid


async def _dispatch(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifier: TeamsNotifier,
    payload: dict[str, str],
) -> bool:
    """Renvoie True si la notification est à considérer comme traitée."""
    async with sm() as ps:
        res = await push.send_to_staff(ps, settings, dict(payload))
    attempted, delivered = res.attempted, res.delivered
    ids = settings.notify_teams_ids
    if delivered == 0 and ids:
        text = f"{payload['title']} : {payload['body']} {settings.public_base_url}{payload['url']}"
        for aad_id in ids:
            attempted += 1
            try:
                await asyncio.wait_for(
                    notifier.notify_user(aad_id, text), timeout=settings.teams_timeout + 5
                )
                delivered += 1
            except Exception as exc:
                log.warning("teams 1:1 notification failed: %s", type(exc).__name__)
    return delivered > 0 or attempted == 0


async def tick(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    roster: RosterProvider,
    notifier: TeamsNotifier,
    now: datetime | None = None,
) -> int:
    """Évalue les déclencheurs ; renvoie le nombre de notifications traitées."""
    now = now or datetime.now(UTC)
    async with sm() as s:
        incidents = list(
            (
                await s.scalars(
                    select(Incident).where(Incident.status == "sent", Incident.sent_at.is_not(None))
                )
            ).all()
        )
        if not incidents:
            return 0
        rows = (
            await s.execute(
                select(
                    IncidentNotification.incident_id,
                    IncidentNotification.key,
                    IncidentNotification.delivered_at,
                    IncidentNotification.attempts,
                ).where(IncidentNotification.incident_id.in_([i.id for i in incidents]))
            )
        ).all()
        done = {(r[0], r[1]) for r in rows if r[2] is not None or r[3] >= MAX_ATTEMPTS}
        cands = await _candidates(s, settings, roster, incidents, now, done)
    handled = 0
    for inc, kind, key in cands[:MAX_PER_TICK]:
        try:
            async with sm() as s:
                nid = await _claim(s, inc.id, kind, key, now)
            if nid is None:
                continue
            payload = build_payload(kind, inc.id, nid, inc.is_test)
            ok = await _dispatch(sm, settings, notifier, payload)
            if ok:
                async with sm() as s:
                    await s.execute(
                        update(IncidentNotification)
                        .where(IncidentNotification.id == nid)
                        .values(delivered_at=now)
                    )
                    await s.commit()
                handled += 1
        except Exception:
            log.exception("notification %s/%s failed", inc.id, kind)
    return handled
