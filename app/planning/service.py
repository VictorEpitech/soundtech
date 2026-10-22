"""Exécution des planifications : tick minute, idempotence stricte, rattrapage borné.

Garanties :
- verrou Redis sur le tick + `UNIQUE (schedule_id, scheduled_for)` : une échéance n'est jamais
  réclamée deux fois (plusieurs workers, redémarrage) ; la réclamation précède l'exécution, donc
  un crash en cours d'exécution laisse une ligne `running` (puis `failed`), jamais un doublon ;
- échéance en retard de plus de CATCHUP : `missed`, rien n'est exécuté ;
- jamais pendant un incident d'alerte `sent` (`skipped_alert`) ;
- les annonces/scènes planifiées sont en priorité STAFF (jamais devant une alerte) et passent par
  les mêmes services que l'interface.
"""

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from redis.exceptions import LockError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import groups, scenes
from app.auth import audit
from app.config import Settings
from app.ha import router as ha_router
from app.ha import service as ha_service
from app.ma.client import MAClient, MAError
from app.ma.router import declared_players
from app.models import (
    Closure,
    PlanningSettings,
    Preset,
    Priority,
    Scene,
    Schedule,
    ScheduleRun,
    ScheduleRunStatus,
    Speaker,
    User,
)
from app.planning.holidays import fr_holidays
from app.planning.recurrence import ClosedFn, closed_days, load_tz, local_date, occurrences

log = logging.getLogger("sound.planning")

ACTOR = "scheduler"
CATCHUP = timedelta(minutes=5)
LOOKBACK = timedelta(hours=24)
RUNNING_STALE = timedelta(minutes=10)
EXEC_TIMEOUT = 90
TICK_LOCK = "sound:planning:tick-lock"
TICK_LOCK_TTL = 300
TICK_BUDGET = 240
MAX_PARALLEL = 4
Status = ScheduleRunStatus


class ActionError(Exception):
    """Échec d'action : le message est court et sans texte sensible."""


class AlertInProgress(ActionError):
    """Un incident d'alerte `sent` est apparu : rien n'est envoyé (statut `skipped_alert`)."""


async def _guard_alert(session: AsyncSession) -> None:
    if await scenes._incident_in_progress(session):
        raise AlertInProgress("alerte en cours")


async def observe_holidays(session: AsyncSession) -> bool:
    row = await session.get(PlanningSettings, 1)
    return True if row is None else row.observe_holidays


async def closure_checker(session: AsyncSession, start: date, end: date) -> ClosedFn:
    rows = await session.execute(
        select(Closure.start_date, Closure.end_date).where(
            Closure.is_active.is_(True), Closure.end_date >= start, Closure.start_date <= end
        )
    )
    spans = [(a, b) for a, b in rows]
    hol: dict[date, str] = {}
    if await observe_holidays(session):
        for y in range(start.year, end.year + 1):
            hol.update(fr_holidays(y))
    return closed_days(spans, hol)


# --- actions ----------------------------------------------------------------


async def _targets(session: AsyncSession, sc: Schedule) -> list[Speaker]:
    found: list[Speaker] = []
    for gid in sc.group_ids or []:
        try:
            found += await groups.resolve_group(session, uuid.UUID(gid))
        except (HTTPException, ValueError):
            continue
    ids = [uuid.UUID(i) for i in sc.speaker_ids or []]
    if ids:
        rows = await session.scalars(
            select(Speaker).where(Speaker.id.in_(ids), Speaker.is_active.is_(True))
        )
        found += list(rows)
    targets = groups.dedupe(found)
    if not targets:
        raise ActionError("aucune enceinte active ciblée")
    return targets


async def _author(session: AsyncSession, sc: Schedule) -> User | None:
    if sc.created_by is None:
        return None
    user = await session.get(User, sc.created_by)
    return user if user is not None and user.is_active else None


async def _do_announcement(
    session: AsyncSession, redis: Redis, settings: Settings, sc: Schedule
) -> str:
    if not settings.ha_configured:
        raise ActionError("home assistant non configuré")
    if sc.preset_id is not None:
        preset = await session.get(Preset, sc.preset_id)
        if preset is None or not preset.is_active:
            raise ActionError("message pré-enregistré indisponible")
        text = preset.text.strip()
    else:
        text = (sc.text or "").strip()
    if not text:
        raise ActionError("texte vide")
    if len(text) > settings.announcement_max_length:
        raise ActionError("texte trop long")
    try:
        voice, lang = await ha_router._voice_lang(session, settings, sc)  # type: ignore[arg-type]
    except HTTPException:
        raise ActionError("voix/langue invalide") from None
    targets = await _targets(session, sc)
    author = await _author(session, sc)
    ann = ha_service.build_announcement(
        settings,
        text=text,
        speakers=targets,
        is_test=sc.is_test,
        priority=Priority.staff,
        author=author,
        voice=voice,
        lang=lang,
    )
    if author is None:
        ann.author_email = sc.created_by_email
    await _guard_alert(session)
    try:
        await ha_service.submit(session, redis, ann)
    except Exception:
        raise ActionError("file d'annonces indisponible") from None
    return f"annonce {ann.id} sur {len(targets)} enceinte(s)"


async def _do_scene(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    ma: MAClient | None,
    sc: Schedule,
    actor: str,
    user_id: uuid.UUID | None,
) -> str:
    scene = await session.get(Scene, sc.scene_id) if sc.scene_id else None
    if scene is None:
        raise ActionError("scène supprimée")
    if ma is None:
        raise ActionError("music assistant non configuré")
    try:
        res = await scenes.launch_scene(
            session,
            scene,
            redis=redis,
            settings=settings,
            ma=ma,
            declared=await declared_players(session),
            author=await _author(session, sc),
            priority=Priority.staff,
            audit_user_id=user_id,
            audit_actor=actor,
            extra={"schedule_id": str(sc.id)},
        )
    except HTTPException as e:
        if e.detail == scenes.ALERT_IN_PROGRESS:
            raise AlertInProgress("alerte en cours") from None
        raise ActionError(str(e.detail)[:120]) from None
    return f"scène lancée ({len(res['ok'])} ok, {len(res['failed'])} en échec)"


async def _stop_player(ma: MAClient, pid: str, volume: int | None) -> bool:
    try:
        if volume is not None:
            await ma.volume_set(pid, volume)
        await ma.queue_cmd(pid, "pause")
    except MAError:
        return False
    return True


async def _do_stop(session: AsyncSession, ma: MAClient | None, sc: Schedule) -> str:
    if ma is None:
        raise ActionError("music assistant non configuré")
    declared = await declared_players(session)
    pids = list(
        dict.fromkeys(
            t.ma_player_id
            for t in await _targets(session, sc)
            if t.ma_player_id and t.ma_player_id in declared
        )
    )
    if not pids:
        raise ActionError("aucun lecteur musique parmi les cibles")
    await _guard_alert(session)
    res = await asyncio.gather(*(_stop_player(ma, p, sc.stop_volume) for p in pids))
    if not any(res):
        raise ActionError("aucun lecteur n'a répondu")
    return f"pause de {sum(res)}/{len(pids)} lecteur(s)"


async def execute(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    ma: MAClient | None,
    sc: Schedule,
    actor: str = ACTOR,
    user_id: uuid.UUID | None = None,
) -> tuple[str, str]:
    """Exécute l'action ; renvoie (statut, détail). Ne lève jamais."""

    async def reset() -> None:  # un rollback expirerait `sc` : on le détache d'abord
        if sc in session:
            session.expunge(sc)
        await session.rollback()

    try:
        async with asyncio.timeout(EXEC_TIMEOUT):
            if sc.action == "announcement":
                detail = await _do_announcement(session, redis, settings, sc)
            elif sc.action == "scene":
                detail = await _do_scene(session, redis, settings, ma, sc, actor, user_id)
            else:
                detail = await _do_stop(session, ma, sc)
    except AlertInProgress as e:
        await reset()
        return Status.skipped_alert.value, str(e)
    except ActionError as e:
        await reset()
        return Status.failed.value, str(e)
    except TimeoutError:
        await reset()
        return Status.failed.value, "délai dépassé"
    except Exception as e:
        log.exception("schedule %s failed", sc.id)
        await reset()
        return Status.failed.value, f"erreur interne ({type(e).__name__})"
    return Status.ok.value, detail


# --- journal / idempotence ----------------------------------------------------


async def _claim(
    session: AsyncSession, sc: Schedule, when: datetime, trigger: str
) -> uuid.UUID | None:
    stmt = (
        pg_insert(ScheduleRun)
        .values(
            id=uuid.uuid4(),
            schedule_id=sc.id,
            schedule_name=sc.name,
            trigger=trigger,
            scheduled_for=when,
            status=Status.running.value,
        )
        .on_conflict_do_nothing(constraint="uq_schedule_runs_slot")
        .returning(ScheduleRun.id)
    )
    rid = (await session.execute(stmt)).scalar_one_or_none()
    await session.commit()
    return rid


async def _finish(
    session: AsyncSession,
    run_id: uuid.UUID,
    sc: Schedule,
    when: datetime,
    status: str,
    detail: str,
    *,
    actor: str = ACTOR,
    user_id: uuid.UUID | None = None,
    request: Request | None = None,
    trigger: str = "schedule",
) -> ScheduleRun:
    run = await session.get(ScheduleRun, run_id)
    now = datetime.now(UTC)
    if run is None:  # planification supprimée pendant l'exécution (cascade) : l'audit reste
        log.warning("schedule %s : ligne de journal disparue", sc.id)
        run = ScheduleRun(
            id=run_id,
            schedule_id=sc.id,
            schedule_name=sc.name,
            trigger=trigger,
            scheduled_for=when,
            status=status,
            detail=detail[:255],
            executed_at=now,
        )
    else:
        run.status, run.detail, run.executed_at = status, detail[:255], now
    if status in (Status.ok.value, Status.failed.value):
        await session.execute(update(Schedule).where(Schedule.id == sc.id).values(last_run_at=now))
    audit.record(
        session,
        "schedule_run",
        request=request,
        user_id=user_id,
        actor=actor,
        detail={
            "schedule_id": str(sc.id),
            "name": sc.name,
            "scheduled_for": run.scheduled_for.isoformat(),
            "trigger": trigger,
            "status": status,
            "detail": detail[:255],
        },
    )
    await session.commit()
    return run


async def _decide_and_run(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    ma: MAClient | None,
    sc: Schedule,
    when: datetime,
    now: datetime,
    is_closed: ClosedFn,
) -> None:
    fresh = await session.get(Schedule, sc.id)  # état à jour : supprimée/modifiée depuis le scan
    if fresh is None or not fresh.is_active or fresh.armed_at > when:
        return
    try:
        if when not in occurrences(fresh, when - timedelta(seconds=1), when):
            return
    except ValueError:
        return
    sc = fresh
    run_id = await _claim(session, sc, when, "schedule")
    if run_id is None:  # déjà réclamée (autre worker, tick précédent)
        return

    async def finish(status: str, detail: str) -> None:
        await _finish(session, run_id, sc, when, status, detail)

    if sc.respect_closures and is_closed(local_date(when, load_tz(sc.timezone))):
        await finish(Status.skipped_closure.value, "jour fermé")
    elif now - when > CATCHUP:
        await finish(Status.missed.value, "échéance manquée (worker arrêté ?)")
    elif await scenes._incident_in_progress(session):
        await finish(Status.skipped_alert.value, "alerte en cours")
    else:
        status, detail = await execute(session, redis, settings, ma, sc)
        await finish(status, detail)


async def _reap_stale(db: async_sessionmaker[AsyncSession], now: datetime) -> None:
    async with db() as s:
        res = await s.execute(
            update(ScheduleRun)
            .where(
                ScheduleRun.status == Status.running.value,
                ScheduleRun.scheduled_for < now - RUNNING_STALE,
            )
            .values(status=Status.failed.value, detail="interrompue", executed_at=now)
        )
        await s.commit()
        if res.rowcount:
            log.warning("%d exécutions planifiées interrompues", res.rowcount)


async def tick(
    db: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    ma: MAClient | None,
    now: datetime | None = None,
) -> int:
    """Traite les échéances dues. Renvoie le nombre d'échéances réclamées par ce tick."""
    now = now or datetime.now(UTC)
    lock = redis.lock(TICK_LOCK, timeout=TICK_LOCK_TTL, blocking=False)
    if not await lock.acquire():
        return 0
    n = 0
    try:
        await _reap_stale(db, now)
        async with db() as s:
            schedules = list(await s.scalars(select(Schedule).where(Schedule.is_active.is_(True))))
            floor = now - LOOKBACK
            is_closed = await closure_checker(
                s, (floor - timedelta(days=2)).date(), (now + timedelta(days=2)).date()
            )
            known = {
                (r[0], r[1])
                for r in await s.execute(
                    select(ScheduleRun.schedule_id, ScheduleRun.scheduled_for).where(
                        ScheduleRun.scheduled_for > floor
                    )
                )
            }
        work: list[tuple[Schedule, datetime]] = []
        for sc in schedules:  # détachés : chaque échéance a sa propre session
            try:
                dues = occurrences(sc, max(floor, sc.armed_at), now)
            except ValueError:
                log.warning("schedule %s : récurrence invalide", sc.id)
                continue
            work += [(sc, when) for when in dues if (sc.id, when) not in known]
        n = len(work)
        sem = asyncio.Semaphore(MAX_PARALLEL)

        async def one(sc: Schedule, when: datetime) -> None:
            async with sem:
                try:
                    async with db() as rs:
                        await _decide_and_run(rs, redis, settings, ma, sc, when, now, is_closed)
                except Exception:
                    log.exception("schedule %s : échéance %s en erreur", sc.id, when)

        try:
            async with asyncio.timeout(TICK_BUDGET):
                await asyncio.gather(*(one(sc, when) for sc, when in work))
        except TimeoutError:
            log.warning("planification : budget du tick dépassé, reste traité au tick suivant")
    finally:
        try:
            await lock.release()
        except LockError:
            log.warning("verrou du tick expiré avant la fin")
    return n


async def run_now(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    ma: MAClient | None,
    sc: Schedule,
    user: User,
    request: Request | None = None,
) -> ScheduleRun:
    """Exécution manuelle (admin) : ignore les fermetures, jamais pendant une alerte."""
    when = datetime.now(UTC)
    actor, uid = user.email, user.id
    run_id = await _claim(session, sc, when, "manual")
    if run_id is None:  # collision à la microseconde près : traitée comme déjà exécutée
        raise HTTPException(409, "already running")
    if await scenes._incident_in_progress(session):
        status, detail = Status.skipped_alert.value, "alerte en cours"
    else:
        status, detail = await execute(session, redis, settings, ma, sc, actor, uid)
    return await _finish(
        session,
        run_id,
        sc,
        when,
        status,
        detail,
        actor=actor,
        user_id=uid,
        request=request,
        trigger="manual",
    )


def run_out(r: ScheduleRun) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "schedule_id": str(r.schedule_id),
        "schedule_name": r.schedule_name,
        "trigger": r.trigger,
        "scheduled_for": r.scheduled_for.isoformat(),
        "executed_at": r.executed_at.isoformat() if r.executed_at else None,
        "status": r.status,
        "detail": r.detail,
    }
