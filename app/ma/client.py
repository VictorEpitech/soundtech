import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets
from websockets.exceptions import WebSocketException

from app.config import Settings

log = logging.getLogger("sound.ma")


class MAError(Exception):
    """Erreur de base du client Music Assistant."""


class MAConfigError(MAError):
    """MA_URL absent."""


class MAConnectionError(MAError):
    """Réseau, DNS, timeout ou protocole WebSocket."""


class MAAuthError(MAError):
    """Authentification refusée ou token requis."""


class MACommandError(MAError):
    """Commande rejetée par Music Assistant."""


def ws_url(url: str) -> str:
    base = url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base if base.endswith("/ws") else base + "/ws"


def http_url(url: str) -> str:
    base = url.rstrip("/")
    if base.endswith("/ws"):
        base = base[: -len("/ws")]
    if base.startswith("ws://"):
        base = "http://" + base[len("ws://") :]
    elif base.startswith("wss://"):
        base = "https://" + base[len("wss://") :]
    return base


class MAConnection:
    """Une session WebSocket authentifiée ; les commandes y sont séquentielles."""

    def __init__(self, ws: Any, timeout: float) -> None:
        self._ws = ws
        self._timeout = timeout

    async def call(self, command: str, **args: Any) -> Any:
        mid = uuid.uuid4().hex
        msg: dict[str, Any] = {"message_id": mid, "command": command}
        if args:
            msg["args"] = args
        try:
            await self._ws.send(json.dumps(msg))
            async with asyncio.timeout(self._timeout):
                while True:
                    data = json.loads(await self._ws.recv())
                    if isinstance(data, dict) and data.get("message_id") == mid:
                        break  # le reste (événements, bannière) est ignoré
        except (TimeoutError, OSError, WebSocketException, ValueError) as exc:
            raise MAConnectionError(f"{type(exc).__name__}: {exc}") from exc
        if "error_code" in data:
            detail = str(data.get("details", ""))[:200]
            if data["error_code"] in (20, 21) or "uthenticat" in detail:
                raise MAAuthError(f"MA: {detail}")
            raise MACommandError(f"MA {data['error_code']}: {detail}")
        return data.get("result")

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Événements poussés par MA (sans message_id), sans timeout de lecture."""
        try:
            while True:
                data = json.loads(await self._ws.recv())
                if isinstance(data, dict) and "event" in data:
                    yield data
        except (OSError, WebSocketException, ValueError) as exc:
            raise MAConnectionError(f"{type(exc).__name__}: {exc}") from exc


class MAClient:
    def __init__(self, url: str, token: str = "", *, timeout: float = 10.0) -> None:
        if not url:
            raise MAConfigError("MA_URL est requis pour la musique")
        self.url = ws_url(url)
        self.http_url = http_url(url)
        self._token = token
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> "MAClient":
        return cls(
            settings.ma_url, settings.ma_token.get_secret_value(), timeout=settings.ma_timeout
        )

    def session(self) -> "_Session":
        return _Session(self)

    async def call(self, command: str, **args: Any) -> Any:
        async with self.session() as conn:
            return await conn.call(command, **args)

    async def aclose(self) -> None:  # connexions éphémères : rien à fermer
        return None

    # --- commandes -----------------------------------------------------------

    async def players(self) -> list[dict[str, Any]]:
        return await self.call("players/all") or []

    async def search(
        self, query: str, media_types: list[str] | None = None, limit: int = 10
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"search_query": query, "limit": limit}
        if media_types:
            args["media_types"] = media_types
        return await self.call("music/search", **args) or {}

    async def play_media(self, queue_id: str, uri: str, option: str | None = None) -> None:
        args: dict[str, Any] = {"queue_id": queue_id, "media": uri}
        if option:
            args["option"] = option
        await self.call("player_queues/play_media", **args)

    async def queue_cmd(self, queue_id: str, cmd: str) -> None:
        if cmd not in ("next", "previous", "play_pause", "pause", "play", "stop"):
            raise ValueError(cmd)
        await self.call(f"player_queues/{cmd}", queue_id=queue_id)

    async def queue_shuffle(self, queue_id: str, enabled: bool) -> None:
        await self.call("player_queues/shuffle", queue_id=queue_id, shuffle_enabled=enabled)

    async def queue_repeat(self, queue_id: str, mode: str) -> None:
        if mode not in ("off", "one", "all"):
            raise ValueError(mode)
        await self.call("player_queues/repeat", queue_id=queue_id, repeat_mode=mode)

    async def queue(self, queue_id: str, limit: int = 100) -> dict[str, Any]:
        """État de la file (`current_index`) et ses éléments, sur une seule session."""
        async with self.session() as conn:
            q = await conn.call("player_queues/get", queue_id=queue_id) or {}
            items = await conn.call("player_queues/items", queue_id=queue_id, limit=limit, offset=0)
        return {
            "current_index": q.get("current_index"),
            "state": q.get("state"),
            "elapsed_time": q.get("elapsed_time"),
            "items": items or [],
        }

    async def queue_delete(self, queue_id: str, item_id: str) -> None:
        await self.call("player_queues/delete_item", queue_id=queue_id, item_id_or_index=item_id)

    async def queue_move(self, queue_id: str, item_id: str, shift: int) -> None:
        await self.call(
            "player_queues/move_item", queue_id=queue_id, queue_item_id=item_id, pos_shift=shift
        )

    async def queue_clear(self, queue_id: str) -> None:
        await self.call("player_queues/clear", queue_id=queue_id)

    async def queue_play_item(self, queue_id: str, item_id: str) -> None:
        await self.call("player_queues/play_index", queue_id=queue_id, index=item_id)

    async def image(
        self,
        provider: str,
        path: str,
        size: int,
        *,
        max_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> tuple[bytes, str] | None:
        """Image servie par MA (`/imageproxy`) avec le token côté serveur ; None si indisponible.

        L'hôte est toujours celui de MA_URL ; `path` n'est qu'un paramètre de requête.
        """
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        params = {"provider": provider, "path": path, "size": str(size)}
        base = self.http_url
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False, transport=transport
            ) as http:
                async with http.stream(
                    "GET", f"{base}/imageproxy", params=params, headers=headers
                ) as r:
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if r.status_code != 200 or not ctype.startswith("image/") or "svg" in ctype:
                        return None
                    declared = r.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        return None
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > max_bytes:
                            return None
        except httpx.HTTPError:
            return None
        return (bytes(buf), ctype) if buf else None

    async def volume_set(self, player_id: str, level: int) -> None:
        await self.call("players/cmd/volume_set", player_id=player_id, volume_level=level)


class _Session:
    def __init__(self, client: MAClient) -> None:
        self._c = client
        self._ws: Any = None

    async def __aenter__(self) -> MAConnection:
        c = self._c
        try:
            async with asyncio.timeout(c.timeout):
                self._ws = await websockets.connect(c.url, open_timeout=c.timeout)
                await self._ws.recv()  # bannière ServerInfo
        except (TimeoutError, OSError, WebSocketException) as exc:
            await self._close()
            raise MAConnectionError(f"{type(exc).__name__}: {exc}") from exc
        conn = MAConnection(self._ws, c.timeout)
        try:
            if c._token:
                await conn.call("auth", token=c._token)
        except BaseException:
            await self._close()
            raise
        return conn

    async def _close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def __aexit__(self, *exc: object) -> None:
        await self._close()
