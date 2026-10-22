"""Relances automatiques des non-répondants (cron du worker, une fois par minute).

Idempotence : verrou Redis par incident + compare-and-swap du compteur en base, pris AVANT l'envoi
(au plus une relance par échéance, même avec plusieurs workers ou après un redémarrage). En cas
d'échec, le compteur est restitué et l'envoi est retenté au tick suivant ; l'échec n'est journalisé
(audit) qu'une fois par série.
"""

import asyncio
import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.alerts import responses
from app.alerts.roster import RosterProvider
from app.alerts.teams import IncidentNotice, TeamsNotifier
from app.auth import audit
from app.config import Settings
from app.models import Incident

log = logging.getLogger("sound.alerts.autoremind")

ACTOR = "worker"
MAX_FAILURES = 3


def effective(incident: Incident, settings: Settings) -> tuple[int, int]:
    """(intervalle en s, plafond) applicables ; 0 = désactivé."""
    interval = (
        incident.auto_remind_interval
        if incident.auto_remind_interval is not None
        else settings.auto_remind_interval
    )
    mx = (
        incident.auto_remind_max
        if incident.auto_remind_max is not None
        else settings.auto_remind_max
    )
    return interval, mx


def is_due(incident: Incident, settings: Settings, now: datetime) -> bool:
    if incident.status != "sent" or incident.sent_at is None:
        return False
    if incident.auto_remind_failures >= MAX_FAILURES:
        return False  # trop d'échecs consécutifs : arrêt des relances auto (reprise : PATCH)
    interval, mx = effective(incident, settings)
    if interval <= 0 or mx <= 0 or incident.auto_remind_count >= mx:
        return False
    ref = incident.auto_remind_last_at or incident.sent_at
    return now >= ref + timedelta(seconds=interval)


async def _record_failure(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    incident: Incident,
    reason: str,
) -> None:
    """Compte l'échec, pose un backoff (x2 par échec) ; audit à la 1re erreur et à l'arrêt."""
    failures = incident.auto_remind_failures + 1
    await session.execute(
        update(Incident).where(Incident.id == incident.id).values(auto_remind_failures=failures)
    )
    detail = {"incident_id": str(incident.id), "reason": reason, "failures": failures}
    if failures == 1:
        audit.record(session, "auto_remind_failed", actor=ACTOR, detail=detail)
    if failures >= MAX_FAILURES:
        audit.record(session, "auto_remind_stopped", actor=ACTOR, detail=detail)
        log.error("auto remind %s stopped after %d consecutive failures", incident.id, failures)
    await session.commit()
    interval, _ = effective(incident, settings)
    ttl = max(60, min(interval or 60, 60 * 2**failures))
    try:
        await redis.set(f"autoremind:backoff:{incident.id}", "1", ex=ttl)
    except Exception:
        log.warning("auto remind backoff unavailable", exc_info=True)


async def _process(
    sm: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    roster: RosterProvider,
    notifier: TeamsNotifier,
    incident_id: uuid.UUID,
    now: datetime,
) -> bool:
    lock = f"autoremind:lock:{incident_id}"
    try:
        if await redis.exists(f"autoremind:backoff:{incident_id}"):
            return False
        if not await redis.set(lock, "1", nx=True, ex=settings.auto_remind_lock_ttl):
            return False
    except Exception:
        log.warning("auto remind lock unavailable, skipped", exc_info=True)
        return False
    cooldown = f"remind:cooldown:{incident_id}"
    try:
        async with sm() as s:
            inc = await s.get(Incident, incident_id, populate_existing=True)
            if inc is None or not is_due(inc, settings, now):
                return False
            data, missing = await responses.report(s, roster, incident_id)
            if data["roster"]["error"]:
                await _record_failure(s, redis, settings, inc, "roster")
                return False
            if not missing:
                return False
            try:
                fresh = settings.remind_cooldown <= 0 or await redis.set(
                    cooldown, "1", nx=True, ex=settings.remind_cooldown
                )
            except Exception:
                log.warning("remind cooldown unavailable", exc_info=True)
                fresh = True
            if not fresh:
                return False  # relance manuelle récente : on réessaiera au tick suivant
            seen, prev_last = inc.auto_remind_count, inc.auto_remind_last_at
            res = await s.execute(
                update(Incident)
                .where(
                    Incident.id == incident_id,
                    Incident.status == "sent",
                    Incident.auto_remind_count == seen,
                )
                .values(auto_remind_count=seen + 1, auto_remind_last_at=now)
            )
            await s.commit()
            if res.rowcount != 1:  # type: ignore[attr-defined]
                return False
            notice = IncidentNotice(str(inc.id), inc.title, inc.message, inc.is_test)
            batches = max(1, math.ceil(len(missing) / 20))
            try:
                await asyncio.wait_for(
                    notifier.remind(notice, missing),
                    timeout=settings.teams_timeout * batches + 10,
                )
            except Exception as exc:
                posted = list(getattr(exc, "mentioned_ids", []) or [])
                log.warning(
                    "auto remind %s failed: %s (%d already mentioned)",
                    incident_id,
                    type(exc).__name__,
                    len(posted),
                )
                if not posted:  # rien posté : on restitue l'échéance
                    await s.execute(
                        update(Incident)
                        .where(Incident.id == incident_id, Incident.auto_remind_count == seen + 1)
                        .values(auto_remind_count=seen, auto_remind_last_at=prev_last)
                    )
                    try:
                        await redis.delete(cooldown)
                    except Exception:
                        log.warning("remind cooldown reset failed", exc_info=True)
                else:  # échec partiel : échéance consommée, on ne reposte pas les lots déjà partis
                    audit.record(
                        s,
                        "auto_remind",
                        actor=ACTOR,
                        detail={
                            "incident_id": str(incident_id),
                            "n": seen + 1,
                            "targets": len(missing),
                            "mentioned": len(posted),
                            "partial": True,
                            "is_test": inc.is_test,
                        },
                    )
                await _record_failure(s, redis, settings, inc, "teams")
                return False
            await s.execute(
                update(Incident).where(Incident.id == incident_id).values(auto_remind_failures=0)
            )
            audit.record(
                s,
                "auto_remind",
                actor=ACTOR,
                detail={
                    "incident_id": str(incident_id),
                    "n": seen + 1,
                    "targets": len(missing),
                    "is_test": inc.is_test,
                },
            )
            await s.commit()
            return True
    finally:
        try:
            await redis.delete(lock)
        except Exception:
            log.debug("auto remind lock release failed", exc_info=True)


async def tick(
    sm: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    roster: RosterProvider,
    notifier: TeamsNotifier,
    now: datetime | None = None,
) -> int:
    """Relance les incidents échus ; renvoie le nombre de relances envoyées."""
    now = now or datetime.now(UTC)
    async with sm() as s:
        incidents = (
            await s.scalars(
                select(Incident).where(Incident.status == "sent", Incident.sent_at.is_not(None))
            )
        ).all()
    due = [i.id for i in incidents if is_due(i, settings, now)]
    sent = 0
    for iid in due:
        try:
            if await _process(sm, redis, settings, roster, notifier, iid, now):
                sent += 1
        except Exception:
            log.exception("auto remind %s crashed", iid)
    return sent
