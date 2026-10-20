import asyncio
import logging
from typing import Any

from app.ma.client import MAClient

log = logging.getLogger("sound.ma")

DUCK_TIMEOUT = 8.0


def _is_playing(player: dict[str, Any]) -> bool:
    state = player.get("playback_state") or player.get("state")
    return str(getattr(state, "value", state)).lower() == "playing"


async def duck(
    ma: MAClient | None,
    player_ids: list[str],
    timeout: float = DUCK_TIMEOUT,  # noqa: ASYNC109
) -> list[str]:
    """Met en pause les players musique en lecture. Best-effort : ne lève jamais.

    Renvoie les players effectivement mis en pause (à passer à `resume`).
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
                    if pid in ids and _is_playing(p):
                        try:
                            await conn.call("player_queues/pause", queue_id=pid)
                            paused.append(pid)
                        except Exception as exc:
                            log.warning("MA pause %s failed: %s", pid, exc)
    except Exception as exc:
        log.warning("MA ducking skipped: %s: %s", type(exc).__name__, exc)
    return paused


async def resume(ma: MAClient | None, player_ids: list[str]) -> None:
    """Relance les players mis en pause par `duck`. Best-effort : ne lève jamais."""
    if ma is None or not player_ids:
        return
    try:
        async with asyncio.timeout(DUCK_TIMEOUT):
            async with ma.session() as conn:
                for pid in player_ids:
                    try:
                        await conn.call("player_queues/play", queue_id=pid)
                    except Exception as exc:
                        log.warning("MA resume %s failed: %s", pid, exc)
    except Exception as exc:
        log.warning("MA resume skipped: %s: %s", type(exc).__name__, exc)
