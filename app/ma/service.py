import asyncio
import logging
from typing import Any

from redis.asyncio import Redis

from app.ma.client import MAClient

log = logging.getLogger("sound.ma")

DUCK_TIMEOUT = 8.0
DUCK_STATE_TTL = 3600


def _hk(pid: str) -> str:
    return f"sound:duck:{pid}:holders"


def _fk(pid: str) -> str:
    return f"sound:duck:{pid}:paused"


def _is_playing(player: dict[str, Any]) -> bool:
    state = player.get("playback_state") or player.get("state")
    return str(getattr(state, "value", state)).lower() == "playing"


async def duck(
    ma: MAClient | None,
    player_ids: list[str],
    timeout: float = DUCK_TIMEOUT,  # noqa: ASYNC109
    redis: Redis | None = None,
    token: str = "",
) -> list[str]:
    """Met en pause les players musique en lecture. Best-effort : ne lève jamais.

    Renvoie les players dont `token` doit assurer la reprise (à passer à `resume`). Avec Redis,
    un bail par diffusion est posé AVANT la pause (une annulation en cours de route reste
    reprise) et un player déjà mis en pause par une diffusion concurrente est rendu aussi :
    la musique ne reprend qu'à la libération du dernier bail.
    """
    ids = {p for p in player_ids if p}
    if ma is None or not ids:
        return []
    paused: list[str] = []
    try:
        async with asyncio.timeout(timeout):
            async with ma.session() as conn:
                players = await conn.call("players/all") or []
                for p in players:
                    pid = p.get("player_id")
                    if pid not in ids:
                        continue
                    held = await _hold(redis, pid, token)
                    if _is_playing(p):
                        paused.append(pid)  # avant l'appel : une annulation reste reprise
                        try:
                            await conn.call("player_queues/pause", queue_id=pid)
                            await _flag(redis, pid)
                        except Exception as exc:
                            log.warning("MA pause %s failed: %s", pid, exc)
                    elif held and await _flagged(redis, pid):
                        paused.append(pid)  # déjà en pause par une diffusion concurrente
                    elif held:
                        await _unhold(redis, pid, token)
    except Exception as exc:
        log.warning("MA ducking skipped: %s: %s", type(exc).__name__, exc)
    return paused


async def _hold(redis: Redis | None, pid: str, token: str) -> bool:
    if redis is None or not token:
        return False
    try:
        await redis.sadd(_hk(pid), token)
        await redis.expire(_hk(pid), DUCK_STATE_TTL)
        return True
    except Exception as exc:
        log.warning("MA duck %s: état Redis indisponible (%s)", pid, exc)
        return False


async def _flag(redis: Redis | None, pid: str) -> None:
    if redis is None:
        return
    try:
        await redis.set(_fk(pid), "1", ex=DUCK_STATE_TTL)
    except Exception as exc:
        log.warning("MA duck %s: drapeau Redis non posé (%s)", pid, exc)


async def _flagged(redis: Redis | None, pid: str) -> bool:
    try:
        return bool(redis is not None and await redis.exists(_fk(pid)))
    except Exception:
        return False


async def _unhold(redis: Redis | None, pid: str, token: str) -> None:
    try:
        if redis is not None:
            await redis.srem(_hk(pid), token)
    except Exception as exc:
        log.warning("MA duck %s: bail non libéré (%s)", pid, exc)


async def _should_resume(redis: Redis | None, pid: str, token: str) -> bool:
    """Libère le bail ; vrai si c'était le dernier (ou si Redis est indisponible)."""
    if redis is None or not token:
        return True
    try:
        await redis.srem(_hk(pid), token)
        if await redis.scard(_hk(pid)) > 0:
            return False
        return bool(await redis.delete(_fk(pid)))
    except Exception as exc:
        log.warning("MA resume %s: état Redis indisponible (%s), reprise directe", pid, exc)
        return True


async def resume(
    ma: MAClient | None, player_ids: list[str], redis: Redis | None = None, token: str = ""
) -> None:
    """Relance les players mis en pause par `duck` (dernier bail seulement). Ne lève jamais."""
    if ma is None or not player_ids:
        return
    try:
        async with asyncio.timeout(DUCK_TIMEOUT):
            async with ma.session() as conn:
                for pid in player_ids:
                    try:
                        if not await _should_resume(redis, pid, token):
                            continue
                        await conn.call("player_queues/play", queue_id=pid)
                    except Exception as exc:
                        log.warning("MA resume %s failed: %s", pid, exc)
    except Exception as exc:
        log.warning("MA resume skipped: %s: %s", type(exc).__name__, exc)
