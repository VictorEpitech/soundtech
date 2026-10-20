import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import audit
from app.config import Settings, get_settings
from app.ha import queue
from app.ha.client import HAClient, HAError
from app.ma import service as ma_service
from app.ma.client import MAClient
from app.models import Announcement, AnnouncementStatus, Priority, Role, Speaker, User

log = logging.getLogger("sound.ha")

INTERRUPTED = "interrompue"
SENDING_STALE = timedelta(minutes=2)
QUEUED_STALE = timedelta(minutes=1)
LOCK_TIMEOUT = 120


def priority_for(user: User) -> Priority:
    """Niveau déduit du rôle ; jamais fourni par le client. `alert` n'est pas atteignable ici."""
    if user.role == Role.admin:
        return Priority.admin
    return Priority.staff


def final_text(settings: Settings, text: str, is_test: bool) -> str:
    return f"{settings.test_prefix} {text}" if is_test else text


def build_announcement(
    settings: Settings,
    *,
    text: str,
    speakers: list[Speaker],
    is_test: bool,
    priority: Priority,
    author: User | None,
) -> Announcement:
    return Announcement(
        id=uuid.uuid4(),
        author_id=author.id if author else None,
        author_email=author.email if author else None,
        text=final_text(settings, text, is_test),
        priority=int(priority),
        is_test=is_test,
        status=AnnouncementStatus.queued.value,
        targets=[
            {
                "id": str(s.id),
                "entity_id": s.entity_id,
                "name": s.name,
                "notify_service": s.notify_service,
                "ma_player_id": s.ma_player_id,
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
    db: async_sessionmaker[AsyncSession], redis: Redis, ha: HAClient, ma: MAClient | None = None
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
        try:
            players = [t.get("ma_player_id") for t in ann.targets]
            if ann.priority == Priority.alert:
                # Une alerte n'attend jamais la musique : ducking borné, best-effort.
                paused = await ma_service.duck(
                    ma, players, timeout=get_settings().alert_duck_timeout
                )
            else:
                paused = await ma_service.duck(ma, players)
            notify = {t["entity_id"]: t.get("notify_service") for t in ann.targets}
            outcome = await ha.speak_many(entities, ann.text, notify)
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
            await s.commit()
    return True


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
    db: async_sessionmaker[AsyncSession], redis: Redis, ha: HAClient, ma: MAClient | None = None
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
            if not await process_one(db, redis, ha, ma):
                break
            n += 1
    finally:
        try:
            await lock.release()
        except Exception:
            log.warning("drain lock release failed")
    return n
