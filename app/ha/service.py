import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import audit
from app.config import Settings, get_settings
from app.ha import queue, volume
from app.ha.client import HAClient, HAError
from app.ha.ssml import build_ssml
from app.ma import service as ma_service
from app.ma.client import MAClient
from app.models import Announcement, AnnouncementStatus, Priority, Role, Speaker, User

log = logging.getLogger("sound.ha")

INTERRUPTED = "interrompue"
SENDING_STALE = timedelta(minutes=2)
QUEUED_STALE = timedelta(minutes=1)
LOCK_TIMEOUT = 120
ALERT_BOOST_BUDGET = 1.5  # secondes : une alerte n'attend jamais plus pour le volume


def priority_for(user: User) -> Priority:
    """Niveau déduit du rôle ; jamais fourni par le client. `alert` n'est pas atteignable ici."""
    if user.role == Role.admin:
        return Priority.admin
    return Priority.staff


def final_text(settings: Settings, text: str, is_test: bool) -> str:
    return f"{settings.test_prefix} {text}" if is_test else text


def volume_level(settings: Settings, priority: Priority, force: bool | None = None) -> int:
    """Volume forcé (1..100) pour ce type de diffusion, 0 = ne pas forcer.

    L'interrupteur de config du type est un coupe-circuit ; `force` (case UI) ne peut que
    désactiver. Le mode TEST est traité comme une diffusion normale (volume forcé aussi).
    """
    alert = priority == Priority.alert
    enabled = settings.alert_force_volume if alert else settings.announce_force_volume
    level = settings.alert_volume if alert else settings.announce_volume
    return level if enabled and force is not False else 0


def build_announcement(
    settings: Settings,
    *,
    text: str,
    speakers: list[Speaker],
    is_test: bool,
    priority: Priority,
    author: User | None,
    voice: str | None = None,
    lang: str | None = None,
    style: str | None = None,
    force_volume: bool | None = None,
) -> Announcement:
    """Voix/langue/style : jamais pour une alerte (voix par défaut de l'enceinte)."""
    level = volume_level(settings, priority, force_volume)
    if priority == Priority.alert:
        voice = lang = style = None
    return Announcement(
        id=uuid.uuid4(),
        author_id=author.id if author else None,
        author_email=author.email if author else None,
        text=final_text(settings, text, is_test),
        priority=int(priority),
        is_test=is_test,
        voice=voice,
        lang=lang,
        style=style,
        ssml=build_ssml(
            text, voice, lang, prefix=settings.test_prefix if is_test else None, style=style
        ),
        status=AnnouncementStatus.queued.value,
        targets=[
            {
                "id": str(s.id),
                "entity_id": s.entity_id,
                "name": s.name,
                "notify_service": s.notify_service,
                "ma_player_id": s.ma_player_id,
                **({"force_volume": level} if level else {}),
            }
            for s in speakers
        ],
        results=None,
    )


async def submit(
    session: AsyncSession, redis: Redis, ann: Announcement, *, request: Any = None
) -> None:
    """Persiste + audite + commit, puis met en file (le worker doit voir la ligne)."""
    session.add(ann)
    audit.record(
        session,
        "announcement_send",
        request=request,
        user_id=ann.author_id,
        actor=ann.author_email,
        detail={
            "announcement_id": str(ann.id),
            "priority": ann.priority,
            "is_test": ann.is_test,
            "targets": [t["entity_id"] for t in ann.targets],
            "text": ann.text,
        },
    )
    await session.commit()
    try:
        await queue.push(redis, str(ann.id), Priority(ann.priority))
    except Exception:
        log.exception("queue push failed")
        ann.status = AnnouncementStatus.failed.value
        ann.results = {"_error": "queue unavailable"}
        ann.finished_at = datetime.now(UTC)
        await session.commit()
        raise


async def process_one(
    db: async_sessionmaker[AsyncSession],
    redis: Redis,
    ha: HAClient,
    ma: MAClient | None = None,
    settings: Settings | None = None,
) -> bool:
    """Traite la prochaine annonce (priorité puis FIFO). False si la file est vide."""
    raw = await queue.pop(redis)
    if raw is None:
        return False
    async with db() as s:
        ann = await s.get(Announcement, uuid.UUID(raw))
        if ann is None or ann.status != AnnouncementStatus.queued.value:
            return True
        ann.status = AnnouncementStatus.sending.value
        ann.started_at = datetime.now(UTC)
        await s.commit()
        entities = [t["entity_id"] for t in ann.targets]
        paused: list[str] = []
        outcome: dict[str, Any] | None = None
        settings = settings or get_settings()
        leases: list[volume.Lease] = []
        try:
            leases = await _boost(redis, ha, settings, ann)
            players = [t.get("ma_player_id") for t in ann.targets]
            if ann.priority == Priority.alert:
                # Une alerte n'attend jamais la musique : ducking borné, best-effort.
                paused = await ma_service.duck(ma, players, timeout=settings.alert_duck_timeout)
            else:
                paused = await ma_service.duck(ma, players)
            notify = {t["entity_id"]: t.get("notify_service") for t in ann.targets}
            outcome = await ha.speak_many(entities, ann.text, notify, ann.ssml)
        except Exception as exc:  # défense : ne jamais laisser une annonce en `sending`
            log.exception("announcement %s crashed", raw)
            outcome = {e: HAError(f"internal error: {type(exc).__name__}") for e in entities}
        finally:
            try:
                await ma_service.resume(ma, paused)
            except Exception:
                log.warning("announcement %s: music resume failed", raw)
            if outcome is None:  # annulation / BaseException
                ann.results = {"_error": INTERRUPTED}
                ann.status = AnnouncementStatus.failed.value
            else:
                ann.results = {
                    e: ({"ok": True} if err is None else {"ok": False, "error": str(err)})
                    for e, err in outcome.items()
                }
                all_ok = all(err is None for err in outcome.values())
                ann.status = (
                    AnnouncementStatus.done if all_ok else AnnouncementStatus.failed
                ).value
            ann.finished_at = datetime.now(UTC)
            try:
                await s.commit()
            finally:  # après le commit : un échec de restauration marque la ligne finale
                _release_later(db, redis, ha, settings, ann, leases, outcome)
    return True


async def _boost(
    redis: Redis, ha: HAClient, settings: Settings, ann: Announcement
) -> list[volume.Lease]:
    """Force le volume des enceintes ciblées. Ne lève jamais : la diffusion prime.

    Alerte : attente totale bornée à ALERT_BOOST_BUDGET ; au-delà la diffusion part et le boost
    se termine en arrière-plan (le bail, créé d'emblée, garantit la restauration).
    """
    try:
        alert = ann.priority == Priority.alert
        hold = volume.hold_seconds(settings, ann.text)
        leases: list[volume.Lease] = []
        tasks: list[asyncio.Task[volume.Lease]] = []
        for t in ann.targets:
            lvl = volume.forced_level(t)
            if not lvl:
                continue
            lease = volume.new_lease(t["entity_id"], str(ann.id), hold)
            leases.append(lease)
            task = asyncio.create_task(volume.acquire(redis, ha, settings, lease, lvl, alert=alert))
            volume.track(task)
            tasks.append(task)
        if tasks:
            if alert:
                await asyncio.wait(tasks, timeout=ALERT_BOOST_BUDGET)
            else:
                await asyncio.gather(*tasks, return_exceptions=True)
        return leases
    except Exception:
        log.exception("announcement %s: volume forcing skipped", ann.id)
        return []


def _release_later(
    db: async_sessionmaker[AsyncSession],
    redis: Redis,
    ha: HAClient,
    settings: Settings,
    ann: Announcement,
    leases: list[volume.Lease],
    outcome: dict[str, Any] | None,
) -> None:
    """Restauration après la fin estimée de la lecture ; immédiate si rien n'a été diffusé."""
    hold = volume.hold_seconds(settings, ann.text)
    for lease in leases:
        spoke = outcome is not None and outcome.get(lease.entity, HAError("x")) is None
        try:
            volume.schedule_release(db, redis, ha, settings, lease, hold if spoke else 0)
        except Exception:
            log.exception("volume %s: scheduling failed", lease.entity)


async def reconcile(db: async_sessionmaker[AsyncSession], redis: Redis) -> None:
    """Rattrape les annonces orphelines (crash worker, perte de file). À appeler sous verrou."""
    now = datetime.now(UTC)
    async with db() as s:
        await s.execute(
            update(Announcement)
            .where(
                Announcement.status == AnnouncementStatus.sending.value,
                Announcement.started_at < now - SENDING_STALE,
            )
            .values(
                status=AnnouncementStatus.failed.value,
                results={"_error": INTERRUPTED},
                finished_at=now,
            )
        )
        await s.commit()
        rows = (
            await s.execute(
                select(Announcement.id, Announcement.priority).where(
                    Announcement.status == AnnouncementStatus.queued.value,
                    Announcement.created_at < now - QUEUED_STALE,
                )
            )
        ).all()
    for ann_id, prio in rows:
        if await queue.requeue(redis, str(ann_id), Priority(prio)):
            log.warning("announcement %s requeued", ann_id)


async def drain(
    db: async_sessionmaker[AsyncSession],
    redis: Redis,
    ha: HAClient,
    ma: MAClient | None = None,
    settings: Settings | None = None,
) -> int:
    """Vide la file sous verrou : un seul consommateur, donc un message à la fois par enceinte."""
    lock = redis.lock(queue.LOCK_KEY, timeout=LOCK_TIMEOUT, blocking=False)
    if not await lock.acquire():
        return 0
    n = 0
    try:
        await reconcile(db, redis)
        while True:
            await lock.reacquire()  # renouvelle le TTL à chaque annonce
            if not await process_one(db, redis, ha, ma, settings):
                break
            n += 1
    finally:
        try:
            await lock.release()
        except Exception:
            log.warning("drain lock release failed")
    return n
