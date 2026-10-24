"""Volume forcé pendant une diffusion, restauré ensuite.

Etat Redis par enceinte (partagé alertes/annonces) :
- `sound:vol:{entity}:orig`   volume ORIGINAL (jamais le volume forcé), ou "unknown" ;
- `sound:vol:{entity}:leases` un bail par diffusion en cours (id -> échéance + annonce) ;
- `sound:vol:{entity}:lock`   verrou court sur les deux clés ci-dessus.
Le volume n'est restauré que lorsque le dernier bail est libéré. Les clés ont un TTL de secours
et le reaper (cron) libère les baux expirés et retente les restaurations échouées.
Rien ici ne doit jamais empêcher ni retarder durablement une diffusion.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import audit
from app.config import Settings
from app.ha.client import HAClient
from app.models import Announcement

log = logging.getLogger("sound.ha.volume")

UNKNOWN = "unknown"
ACTIVE_KEY = "sound:vol:active"
STEP_TIMEOUT = 4.0  # lecture ou mise à niveau (annonce)
ALERT_STEP_TIMEOUT = 1.0  # idem pour une alerte, sous le budget global de service._boost
LOCK_WAIT = 3.0
ALERT_LOCK_WAIT = 0.4  # une alerte attend brièvement le verrou, puis force quand même
VOLUME_TOLERANCE = 0.02
ALERT_VERIFY_RETRIES = 2
LOCK_TTL = 120
LEASE_SLACK = 30.0  # marge avant que le reaper ne considère un bail comme abandonné

RESTORED = "restored"
PENDING = "pending"  # d'autres diffusions tiennent encore l'enceinte
NO_RESTORE = "no_restore"  # volume d'origine inconnu : rien d'inventé
NOTHING = "nothing"  # aucun état à restaurer
FAILED = "failed"
DEFERRED = "deferred"  # verrou indisponible : le reaper reprendra

_tasks: set[asyncio.Task[Any]] = set()


def _k(entity: str, part: str) -> str:
    return f"sound:vol:{entity}:{part}"


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


@dataclass
class Lease:
    entity: str
    lease_id: str
    announcement_id: str
    deadline: float = 0.0
    skipped: bool = False  # entité non pilotable : rien à libérer
    volume_ok: bool | None = None  # alerte : niveau confirmé / non posé / inconnu (None)
    volume_error: str = ""


@dataclass
class Outcome:
    entity: str
    status: str
    announcement_ids: list[str] = field(default_factory=list)
    new_failure: bool = False


def forced_level(target: dict[str, Any]) -> int:
    v = target.get("force_volume")
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 100 else 0


def hold_seconds(settings: Settings, text: str) -> float:
    """Durée de lecture estimée + marge, bornée (le TTS HA ne signale pas sa fin)."""
    words = len(text.split())
    est = words * 60 / max(1, settings.announce_wpm) + settings.volume_restore_margin
    return min(settings.volume_restore_max_wait, max(settings.volume_restore_margin, est))


def new_lease(entity: str, announcement_id: str, hold: float) -> Lease:
    return Lease(
        entity,
        uuid.uuid4().hex,
        announcement_id,
        deadline=time.time() + hold + LEASE_SLACK,
        skipped=not entity.startswith("media_player."),
    )


async def force_verified(
    ha: HAClient, lease: Lease, level_pct: int, step: float, retries: int = ALERT_VERIFY_RETRIES
) -> None:
    """Pose le niveau puis relit volume_level ; réessaie `retries` fois si l'écart dépasse 2 %.

    Renseigne lease.volume_ok / volume_error, ne lève jamais (sauf annulation).
    """
    entity, target = lease.entity, level_pct / 100
    err = ""
    for _ in range(retries + 1):
        try:
            await asyncio.wait_for(ha.set_volume(entity, target), step)
        except Exception as exc:
            err = f"set: {type(exc).__name__}: {exc}"
            continue
        try:
            got = await asyncio.wait_for(ha.get_volume(entity), step)
        except Exception as exc:
            got, err = None, f"lecture: {type(exc).__name__}: {exc}"
        else:
            if got is None:
                err = "volume_level absent de l'état"
        if got is None:  # set passé mais non vérifiable : inutile de réessayer à l'aveugle
            lease.volume_error = err
            log.warning("volume %s: ALERTE niveau %d%% posé, non vérifiable (%s)", entity,
                        level_pct, err)  # fmt: skip
            return
        if abs(got - target) <= VOLUME_TOLERANCE:
            lease.volume_ok, lease.volume_error = True, ""
            return
        err = f"écart: {got:.2f} au lieu de {target:.2f}"
        await asyncio.sleep(0.2)
    lease.volume_ok, lease.volume_error = False, err
    log.warning("volume %s: ALERTE niveau %d%% non confirmé (%s)", entity, level_pct, err)


async def acquire(
    redis: Redis,
    ha: HAClient,
    settings: Settings,
    lease: Lease,
    level_pct: int,
    *,
    alert: bool = False,
) -> Lease:
    """Prend le bail, mémorise le volume original (une seule fois) puis force le niveau.

    Ne lève jamais : tout échec est journalisé et la diffusion continue. Une alerte n'attend
    ni le verrou ni HA longtemps (try-lock, pas de settle) ; annonce : volume d'origine
    illisible => pas de forçage.
    """
    entity = lease.entity
    if lease.skipped:
        log.warning("volume %s: entité non media_player, volume non forcé", entity)
        if alert:
            lease.volume_ok, lease.volume_error = False, "entité non media_player"
        return lease
    step = ALERT_STEP_TIMEOUT if alert else STEP_TIMEOUT
    try:
        async with redis.lock(
            _k(entity, "lock"),
            timeout=LOCK_TTL,
            blocking=True,
            blocking_timeout=ALERT_LOCK_WAIT if alert else LOCK_WAIT,
        ):
            ttl = settings.volume_state_ttl
            leases, orig_key = _k(entity, "leases"), _k(entity, "orig")
            await redis.hset(
                leases,
                lease.lease_id,
                json.dumps({"deadline": lease.deadline, "ann": lease.announcement_id}),
            )
            await redis.sadd(_k(entity, "anns"), lease.announcement_id)
            if not await redis.exists(orig_key):
                try:
                    v = await asyncio.wait_for(ha.get_volume(entity), step)
                    orig = UNKNOWN if v is None else repr(v)
                except Exception as exc:
                    orig = UNKNOWN
                    log.warning("volume %s: lecture impossible (%s: %s)", entity,
                                type(exc).__name__, exc)  # fmt: skip
                if orig == UNKNOWN:
                    log.warning("volume %s: volume d'origine inconnu", entity)
                    if not alert:  # annonce : diffusion normale, volume intact
                        await redis.hdel(leases, lease.lease_id)
                        lease.skipped = True
                        return lease
                await redis.set(orig_key, orig, ex=ttl)
            await redis.sadd(ACTIVE_KEY, entity)
            for part in ("orig", "leases", "anns"):
                await redis.expire(_k(entity, part), ttl)
            if alert:
                await force_verified(ha, lease, level_pct, step)
            else:
                try:
                    await asyncio.wait_for(ha.set_volume(entity, level_pct / 100), step)
                except Exception as exc:
                    log.warning("volume %s: mise à niveau impossible (%s: %s)", entity,
                                type(exc).__name__, exc)  # fmt: skip
    except Exception as exc:
        log.warning("volume %s: forçage ignoré (%s: %s)", entity, type(exc).__name__, exc)
        if alert and lease.volume_ok is None:
            # verrou/Redis indisponible : l'alerte force quand même le niveau, sans état
            # (le détenteur du verrou restaure son volume d'origine ; aucun bail touché ici).
            lease.skipped = True
            await force_verified(ha, lease, level_pct, step)
        return lease
    if settings.volume_settle > 0 and not alert:
        await asyncio.sleep(settings.volume_settle)
    return lease


async def _restore_locked(redis: Redis, ha: HAClient, settings: Settings, entity: str) -> str:
    """Appelé sous verrou, sans bail restant. Garde l'état si la restauration échoue."""
    raw = await redis.get(_k(entity, "orig"))
    if raw is None:
        await redis.srem(ACTIVE_KEY, entity)
        return NOTHING
    orig = _s(raw)

    async def clear() -> None:
        await redis.delete(
            _k(entity, "orig"), _k(entity, "leases"), _k(entity, "failed"), _k(entity, "anns")
        )
        await redis.srem(ACTIVE_KEY, entity)

    if orig == UNKNOWN:
        log.warning("volume %s: volume d'origine inconnu, aucune restauration inventée", entity)
        await clear()
        return NO_RESTORE
    level = float(orig)
    last: Exception | None = None
    for attempt in range(settings.volume_restore_retries):
        try:
            await asyncio.wait_for(ha.set_volume(entity, level), STEP_TIMEOUT)
            await clear()
            return RESTORED
        except Exception as exc:
            last = exc
            log.warning("volume %s: restauration %d/%d échouée (%s: %s)", entity, attempt + 1,
                        settings.volume_restore_retries, type(exc).__name__, exc)  # fmt: skip
            await asyncio.sleep(min(2.0, 0.5 * 2**attempt))
    log.error("volume %s: RESTAURATION ECHOUEE vers %.2f (%s)", entity, level, last)
    return FAILED


async def _anns(redis: Redis, entity: str) -> list[str]:
    return [_s(a) for a in await redis.smembers(_k(entity, "anns"))]


async def _outcome(redis: Redis, entity: str, status: str, anns: list[str]) -> Outcome:
    new = False
    if status == FAILED:
        new = bool(await redis.set(_k(entity, "failed"), "1", nx=True, ex=3600))
    return Outcome(entity, status, anns, new)


async def release(redis: Redis, ha: HAClient, settings: Settings, lease: Lease) -> Outcome:
    """Libère le bail ; restaure si c'était le dernier. Ne lève jamais."""
    entity = lease.entity
    if lease.skipped:
        return Outcome(entity, PENDING)
    try:
        async with redis.lock(_k(entity, "lock"), timeout=LOCK_TTL, blocking_timeout=LOCK_WAIT * 4):
            await redis.hdel(_k(entity, "leases"), lease.lease_id)
            if await redis.hlen(_k(entity, "leases")) > 0:
                return Outcome(entity, PENDING)
            anns = {lease.announcement_id, *await _anns(redis, entity)}
            status = await _restore_locked(redis, ha, settings, entity)
            return await _outcome(redis, entity, status, sorted(anns))
    except Exception as exc:
        log.error("volume %s: libération du bail impossible (%s: %s)", entity,
                  type(exc).__name__, exc)  # fmt: skip
        return Outcome(entity, DEFERRED, [lease.announcement_id])


async def reap(redis: Redis, ha: HAClient, settings: Settings) -> list[Outcome]:
    """Libère les baux expirés (worker crashé) et retente les restaurations en échec."""
    out: list[Outcome] = []
    for raw in await redis.smembers(ACTIVE_KEY):
        entity = _s(raw)
        try:
            async with redis.lock(_k(entity, "lock"), timeout=LOCK_TTL, blocking_timeout=1):
                now = time.time()
                anns: list[str] = []
                for lid, val in (await redis.hgetall(_k(entity, "leases"))).items():
                    info = json.loads(_s(val))
                    if info.get("deadline", 0) < now:
                        await redis.hdel(_k(entity, "leases"), lid)
                        anns.append(info.get("ann", ""))
                if await redis.hlen(_k(entity, "leases")) > 0:
                    continue
                anns += await _anns(redis, entity)
                status = await _restore_locked(redis, ha, settings, entity)
                out.append(await _outcome(redis, entity, status, sorted(set(anns))))
        except Exception as exc:
            log.warning("volume reaper %s: %s: %s", entity, type(exc).__name__, exc)
    return out


async def report(db: async_sessionmaker[AsyncSession], outcome: Outcome) -> None:
    """Journalise + audit + marque les annonces concernées (restauration en échec)."""
    if outcome.status == NO_RESTORE:
        log.error("volume %s: volume d'origine inconnu, enceinte laissée au volume forcé "
                  "(alerte) : à remettre à la main", outcome.entity)  # fmt: skip
        await _record(db, outcome, "volume_restore_unknown", "restore_unknown")
        return
    if outcome.status not in (FAILED, DEFERRED):
        return
    log.error("volume %s: enceinte possiblement laissée au volume forcé (%s)", outcome.entity,
              outcome.status)  # fmt: skip
    if outcome.status == FAILED and not outcome.new_failure:
        return
    await _record(db, outcome, "volume_restore_failed", "restore_failed")


async def _record(
    db: async_sessionmaker[AsyncSession], outcome: Outcome, action: str, mark: str
) -> None:
    try:
        async with db() as s:
            await _mark(s, outcome, mark)
            audit.record(
                s,
                action,
                actor="worker",
                detail={
                    "entity_id": outcome.entity,
                    "status": outcome.status,
                    "announcements": outcome.announcement_ids,
                },
            )
            await s.commit()
    except Exception:
        log.exception("volume %s: marquage de l'échec impossible", outcome.entity)


async def _mark(s: AsyncSession, outcome: Outcome, mark: str) -> None:
    ids = []
    for a in outcome.announcement_ids:
        try:
            ids.append(uuid.UUID(a))
        except ValueError:
            continue
    for ann in (await s.scalars(select(Announcement).where(Announcement.id.in_(ids)))).all():
        res = dict(ann.results or {})
        vol = dict(res.get("_volume") or {})
        vol[outcome.entity] = mark
        res["_volume"] = vol
        ann.results = res


async def _hold_then_release(
    db: async_sessionmaker[AsyncSession],
    redis: Redis,
    ha: HAClient,
    settings: Settings,
    lease: Lease,
    delay: float,
) -> None:
    try:
        if delay > 0:
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        # arrêt du worker : ne pas couper l'annonce en cours ; le bail reste dans Redis et le
        # reaper restaure à l'échéance (immédiatement seulement si elle est déjà passée)
        if time.time() >= lease.deadline:
            await report(db, await release(redis, ha, settings, lease))
        raise
    await report(db, await release(redis, ha, settings, lease))


def track(task: asyncio.Task[Any]) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def schedule_release(
    db: async_sessionmaker[AsyncSession],
    redis: Redis,
    ha: HAClient,
    settings: Settings,
    lease: Lease,
    delay: float,
) -> None:
    """Planifie la libération en arrière-plan (ne bloque ni la file ni une alerte suivante)."""
    task = asyncio.create_task(_hold_then_release(db, redis, ha, settings, lease, delay))
    track(task)


async def cancel_pending() -> None:
    for t in list(_tasks):
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)


async def wait_pending() -> None:
    """Attend les libérations planifiées (tests, arrêt propre)."""
    await asyncio.gather(*list(_tasks), return_exceptions=True)
