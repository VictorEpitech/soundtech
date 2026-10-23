"""Surveillance des événements MA : stop explicite en fin de lecture si repeat est désactivé.

Les Echo Dot relancent le média en boucle même sans répétition. Fin de lecture = une queue qui
passe de `playing` à `idle` (événement `queue_updated`, ou `player_updated` pour un player sans
queue_id distinct). Si `repeat_mode` vaut `off`, on envoie `player_queues/stop` puis on retire de
la file les titres joués (ceux vus comme `current_item` pendant la lecture) pour qu'elle ne
redémarre pas seule ; sinon rien (répétition voulue). Les titres non joués sont conservés.
"""

import asyncio
import logging
from typing import Any

from app.ma.client import MAClient, MAError

log = logging.getLogger("sound.ma")

EVENTS = ("queue_updated", "player_updated")
RECONNECT_MAX = 30.0


def _val(x: Any) -> str | None:
    x = getattr(x, "value", x)
    return None if x is None else str(x).lower()


class EndOfPlayDetector:
    def __init__(self, ma: MAClient) -> None:
        self.ma = ma
        self._state: dict[str, str | None] = {}
        self._repeat: dict[str, str | None] = {}
        self._played: dict[str, list[str]] = {}
        self._pos: dict[str, tuple[float, float]] = {}

    async def handle(self, msg: dict[str, Any]) -> bool:
        """Traite un événement ; True si un stop a été envoyé."""
        ev = msg.get("event")
        data = msg.get("data")
        if ev not in EVENTS or not isinstance(data, dict):
            return False
        qid = data.get("queue_id") if ev == "queue_updated" else data.get("player_id")
        if not qid:
            return False
        state = _val(data.get("state") if ev == "queue_updated" else data.get("playback_state"))
        if ev == "queue_updated" and "repeat_mode" in data:
            self._repeat[qid] = _val(data["repeat_mode"])
        if ev == "queue_updated" and state in (None, "playing", "paused"):
            self._track_item(qid, data.get("current_item"))
            self._track_pos(qid, data)
        if state is None:
            return False
        prev = self._state.get(qid)
        self._state[qid] = state
        if not (prev == "playing" and state == "idle"):
            return False
        repeat = self._repeat.get(qid)
        if repeat is None:
            repeat = await self._fetch_repeat(qid)
        if repeat != "off":  # one/all, ou inconnu : ne jamais couper une répétition voulue
            self._played.pop(qid, None)
            return False
        try:
            await self.ma.queue_cmd(qid, "stop")
        except MAError as exc:
            log.warning("MA stop %s failed: %s", qid, exc)
            return False
        log.info("MA stop %s (fin de lecture, repeat off)", qid)
        await self._drop_played(qid, natural=self._natural_end(qid))
        return True

    def _track_pos(self, qid: str, data: dict[str, Any]) -> None:
        item = data.get("current_item")
        dur = item.get("duration") if isinstance(item, dict) else None
        el = data.get("elapsed_time")
        if isinstance(dur, int | float) and isinstance(el, int | float) and dur > 0:
            self._pos[qid] = (float(el), float(dur))

    def _natural_end(self, qid: str) -> bool:
        """Fin naturelle = position à moins de 3 s de la durée ; sinon (Stop manuel) on garde."""
        pos = self._pos.pop(qid, None)
        return pos is not None and pos[0] >= pos[1] - 3.0

    def _track_item(self, qid: str, item: Any) -> None:
        iid = item.get("queue_item_id") if isinstance(item, dict) else None
        if iid:
            seen = self._played.setdefault(qid, [])
            if str(iid) not in seen:
                seen.append(str(iid))

    async def _drop_played(self, qid: str, natural: bool) -> None:
        seen = self._played.pop(qid, [])
        # Stop manuel : le titre en cours (le dernier vu) est interrompu, pas joué : on le garde.
        for iid in seen if natural else seen[:-1]:
            try:
                await self.ma.queue_delete(qid, iid)
            except MAError as exc:  # le stop est déjà parti : le nettoyage est au mieux
                log.warning("MA delete %s/%s failed: %s", qid, iid, exc)

    async def _fetch_repeat(self, qid: str) -> str | None:
        try:
            q = await self.ma.call("player_queues/get", queue_id=qid)
        except MAError:
            return None
        return _val(q.get("repeat_mode")) if isinstance(q, dict) else None


async def run(ma: MAClient) -> None:
    """Boucle de surveillance avec reconnexion ; ne s'arrête que par annulation."""
    det = EndOfPlayDetector(ma)
    delay = 1.0
    while True:
        try:
            async with ma.session() as conn:
                delay = 1.0
                async for msg in conn.events():
                    try:
                        await det.handle(msg)
                    except Exception:
                        log.exception("MA monitor event failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("MA monitor disconnected (%s), retry in %.0fs", exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX)
