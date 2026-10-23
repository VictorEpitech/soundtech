import logging
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_session, require_role
from app.config import Settings, get_settings
from app.ma import images
from app.ma.client import MAAuthError, MAClient, MAConnectionError, MAError
from app.models import Role, Speaker, User
from app.ratelimit import per_user

staff_up = require_role(Role.admin, Role.staff)
music_user = per_user("music", "rl_music_limit", staff_up)
image_user = per_user("music_image", "rl_music_image_limit", staff_up)

log = logging.getLogger("sound.ma")
OWNER_TTL = 7 * 86400

router = APIRouter(prefix="/music", tags=["music"])

MediaType = Literal["artist", "album", "track", "playlist", "radio"]
ItemId = Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")
IMAGE_CACHE = "private, max-age=86400, immutable"


def get_ma(settings: Settings = Depends(get_settings)) -> MAClient:
    if not settings.ma_configured:
        raise HTTPException(503, "music assistant not configured")
    return MAClient.from_settings(settings)


async def _run(coro: Any) -> Any:
    try:
        return await coro
    except MAConnectionError:
        raise HTTPException(503, "music assistant unreachable") from None
    except MAAuthError:
        raise HTTPException(502, "music assistant refused credentials") from None
    except MAError:
        raise HTTPException(502, "music assistant error") from None


async def declared_players(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    """Players MA autorisés : `ma_player_id` -> nom des enceintes actives déclarées en base."""
    rows = await session.execute(
        select(Speaker.ma_player_id, Speaker.name)
        .where(Speaker.is_active.is_(True), Speaker.ma_player_id.is_not(None))
        .order_by(Speaker.name)
    )
    return {pid: name for pid, name in rows}


async def declared_player(
    player_id: str, declared: dict[str, str] = Depends(declared_players)
) -> str:
    if player_id not in declared:
        raise HTTPException(403, "player not declared")
    return player_id


class PlayIn(BaseModel):
    uri: str = Field(min_length=3, max_length=500, pattern=r"^\S+$")
    option: Literal["play", "replace", "next", "add"] | None = None


class MoveIn(BaseModel):
    shift: int = Field(ge=-50, le=50)


class VolumeIn(BaseModel):
    level: int = Field(ge=0, le=100)


def _player_out(p: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    state = p.get("playback_state") or p.get("state")
    current = p.get("current_media") or {}
    return {
        "id": p.get("player_id"),
        "name": name or p.get("display_name") or p.get("name"),
        "available": bool(p.get("available", True)),
        "state": getattr(state, "value", state),
        "volume": p.get("volume_level"),
        "current": current.get("title") if isinstance(current, dict) else None,
    }


def _artist_names(i: dict[str, Any]) -> list[str] | None:
    artists = i.get("artists")
    if not isinstance(artists, list):
        return None
    return [a.get("name") for a in artists if isinstance(a, dict)]


def _item_out(i: dict[str, Any], secret: str = "") -> dict[str, Any]:
    return {
        "uri": i.get("uri"),
        "name": i.get("name"),
        "media_type": i.get("media_type"),
        "artists": _artist_names(i),
        "image": images.image_url(i, secret),
    }


def _owner_key(player_id: str) -> str:
    return f"ma:qowner:{player_id}"


def _is_admin(user: User) -> bool:
    return user.role == Role.admin


def _admin_only(user: User) -> None:
    if not _is_admin(user):
        raise HTTPException(403, "admin only")


async def _owners(request: Request, player_id: str) -> dict[str, str]:
    try:
        raw = await request.app.state.redis.hgetall(_owner_key(player_id))
    except Exception:
        log.warning("queue owners unavailable", exc_info=True)
        return {}
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in raw.items()
    }


async def _record_owners(request: Request, player_id: str, ids: list[str], user: User) -> None:
    if not ids:
        return
    try:
        r = request.app.state.redis
        key = _owner_key(player_id)
        await r.hset(key, mapping=dict.fromkeys(ids, str(user.id)))
        await r.expire(key, OWNER_TTL)
    except Exception:
        log.warning("queue owner not recorded", exc_info=True)


def _queue_ids(q: dict[str, Any]) -> list[str]:
    return [
        str(i["queue_item_id"])
        for i in q["items"]
        if isinstance(i, dict) and i.get("queue_item_id")
    ]


def _secs(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int | float) or v < 0:
        return None
    return int(v)


def _finished(i: dict[str, Any], elapsed: int | None) -> bool:
    media = i.get("media_item")
    dur = _secs(i.get("duration") or (media.get("duration") if isinstance(media, dict) else None))
    return bool(dur) and elapsed is not None and elapsed >= dur - 1


def _queue_item_out(i: dict[str, Any], secret: str, current: bool) -> dict[str, Any]:
    media = i.get("media_item")
    media = media if isinstance(media, dict) else {}
    return {
        "id": i.get("queue_item_id"),
        "name": i.get("name") or media.get("name"),
        "artists": _artist_names(media),
        "duration": i.get("duration") or media.get("duration"),
        "image": images.image_url(i, secret),
        "current": current,
    }


@router.get("/players")
async def list_players(
    _: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
    declared: dict[str, str] = Depends(declared_players),
) -> list[dict[str, Any]]:
    if not declared:
        return []
    return [
        _player_out(p, declared[p["player_id"]])
        for p in await _run(ma.players())
        if p.get("player_id") in declared
    ]


@router.get("/players/available")
async def available_players(
    _: User = Depends(require_role(Role.admin)),
    ma: MAClient = Depends(get_ma),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Tous les lecteurs MA, avec l'enceinte qui utilise déjà chaque ID (aide à la saisie)."""
    rows = await session.execute(
        select(Speaker.ma_player_id, Speaker.name)
        .where(Speaker.ma_player_id.is_not(None))
        .order_by(Speaker.name)
    )
    used: dict[str, str] = {}
    for pid, name in rows:
        used.setdefault(pid, name)
    return [
        {
            "player_id": p.get("player_id"),
            "name": p.get("display_name") or p.get("name") or p.get("player_id"),
            "provider": p.get("provider"),
            "available": bool(p.get("available", True)),
            "declared_as": used.get(p.get("player_id")),
        }
        for p in await _run(ma.players())
        if p.get("player_id")
    ]


@router.get("/search")
async def search(
    q: str = Query(min_length=1, max_length=200),
    media_types: list[MediaType] | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    _: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
    settings: Settings = Depends(get_settings),
) -> dict[str, list[dict[str, Any]]]:
    res = await _run(ma.search(q.strip(), media_types, limit))
    secret = settings.jwt_secret
    return {
        k: [_item_out(i, secret) for i in v if isinstance(i, dict)]
        for k, v in res.items()
        if isinstance(v, list)
    }


@router.post("/players/{player_id}/play", status_code=204)
async def play(
    body: PlayIn,
    request: Request,
    player_id: str = Depends(declared_player),
    user: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
) -> None:
    if _is_admin(user):
        await _run(ma.play_media(player_id, body.uri, body.option))
        return
    if body.option != "add":
        raise HTTPException(403, "staff can only add to the queue")
    before = await _run(ma.queue(player_id))
    known = set(_queue_ids(before))
    idle = not before["items"] or before.get("state") not in ("playing", "paused")
    await _run(ma.play_media(player_id, body.uri, "play" if idle else "add"))
    after = await _run(ma.queue(player_id))
    await _record_owners(request, player_id, [i for i in _queue_ids(after) if i not in known], user)


def _cmd(name: str, cmd: str, admin_only: bool = False) -> None:
    async def endpoint(
        player_id: str = Depends(declared_player),
        user: User = Depends(music_user),
        ma: MAClient = Depends(get_ma),
    ) -> None:
        if admin_only:
            _admin_only(user)
        await _run(ma.queue_cmd(player_id, cmd))

    endpoint.__name__ = name
    router.post(f"/players/{{player_id}}/{name}", status_code=204)(endpoint)


_cmd("pause", "pause")
_cmd("next", "next", admin_only=True)
_cmd("previous", "previous", admin_only=True)
_cmd("resume", "play")


@router.post("/players/{player_id}/volume", status_code=204)
async def volume(
    body: VolumeIn,
    player_id: str = Depends(declared_player),
    _: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
) -> None:
    await _run(ma.volume_set(player_id, body.level))


# --- file d'attente ------------------------------------------------------------


@router.get("/players/{player_id}/queue")
async def get_queue(
    request: Request,
    player_id: str = Depends(declared_player),
    user: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    q = await _run(ma.queue(player_id))
    cur = q["current_index"]
    owners = {} if _is_admin(user) else await _owners(request, player_id)
    upcoming: list[dict[str, Any]] = []
    now: dict[str, Any] | None = None
    elapsed = _secs(q.get("elapsed_time"))
    for idx, i in enumerate(q["items"]):
        if not isinstance(i, dict):
            continue
        is_cur = isinstance(cur, int) and idx == cur
        if isinstance(cur, int) and idx < cur:
            continue
        if is_cur and q.get("state") != "playing" and _finished(i, elapsed):
            continue
        item = _queue_item_out(i, settings.jwt_secret, is_cur)
        item["mine"] = _is_admin(user) or owners.get(str(item["id"])) == str(user.id)
        if is_cur:
            now = item
        else:
            upcoming.append(item)
    return {
        "current_index": cur,
        "state": q.get("state"),
        "elapsed": elapsed,
        "duration": _secs(now["duration"]) if now else None,
        "now": now,
        "items": upcoming,
    }


@router.post("/players/{player_id}/queue/clear", status_code=204)
async def queue_clear(
    player_id: str = Depends(declared_player),
    user: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
) -> None:
    _admin_only(user)
    await _run(ma.queue_clear(player_id))


@router.delete("/players/{player_id}/queue/items/{item_id}", status_code=204)
async def queue_delete(
    request: Request,
    item_id: str = ItemId,
    player_id: str = Depends(declared_player),
    user: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
) -> None:
    if not _is_admin(user) and (await _owners(request, player_id)).get(item_id) != str(user.id):
        raise HTTPException(403, "not your queue item")
    await _run(ma.queue_delete(player_id, item_id))


@router.post("/players/{player_id}/queue/items/{item_id}/move", status_code=204)
async def queue_move(
    body: MoveIn,
    item_id: str = ItemId,
    player_id: str = Depends(declared_player),
    user: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
) -> None:
    _admin_only(user)
    await _run(ma.queue_move(player_id, item_id, body.shift))


@router.post("/players/{player_id}/queue/items/{item_id}/play", status_code=204)
async def queue_play_item(
    item_id: str = ItemId,
    player_id: str = Depends(declared_player),
    user: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
) -> None:
    _admin_only(user)
    await _run(ma.queue_play_item(player_id, item_id))


# --- pochettes -------------------------------------------------------------------


def image_transport() -> httpx.AsyncBaseTransport | None:
    return None


def _placeholder() -> Response:
    return Response(
        images.PLACEHOLDER,
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/image")
async def image(
    provider: str = Query(max_length=64),
    path: str = Query(max_length=images.MAX_PATH),
    sig: str = Query(max_length=64),
    size: int = Query(default=128, ge=images.SIZES[0], le=images.SIZES[-1]),
    _: User = Depends(image_user),
    settings: Settings = Depends(get_settings),
    transport: httpx.AsyncBaseTransport | None = Depends(image_transport),
) -> Response:
    """Proxy de pochette : n'accepte que les références signées par ce serveur (anti-SSRF)."""
    if not images.verify(settings.jwt_secret, provider, path, sig):
        raise HTTPException(403, "invalid image reference")
    if not settings.ma_configured:
        return _placeholder()
    ma = MAClient.from_settings(settings)
    size = min(images.SIZES, key=lambda s: abs(s - size))
    got = await ma.image(
        provider, path, size, max_bytes=settings.ma_image_max_bytes, transport=transport
    )
    if got is None:
        return _placeholder()
    data, ctype = got
    return Response(
        data,
        media_type=ctype,
        headers={"Cache-Control": IMAGE_CACHE, "X-Content-Type-Options": "nosniff"},
    )
