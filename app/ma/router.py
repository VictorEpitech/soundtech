from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.deps import require_role
from app.config import Settings, get_settings
from app.ma.client import MAAuthError, MAClient, MAConnectionError, MAError
from app.models import Role, User
from app.ratelimit import per_user

staff_up = require_role(Role.admin, Role.staff)
music_user = per_user("music", "rl_music_limit", staff_up)

router = APIRouter(prefix="/music", tags=["music"])

MediaType = Literal["artist", "album", "track", "playlist", "radio"]


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


class PlayIn(BaseModel):
    uri: str = Field(min_length=3, max_length=500, pattern=r"^\S+$")
    option: Literal["play", "replace", "next", "add"] | None = None


class VolumeIn(BaseModel):
    level: int = Field(ge=0, le=100)


def _player_out(p: dict[str, Any]) -> dict[str, Any]:
    state = p.get("playback_state") or p.get("state")
    current = p.get("current_media") or {}
    return {
        "id": p.get("player_id"),
        "name": p.get("display_name") or p.get("name"),
        "available": bool(p.get("available", True)),
        "state": getattr(state, "value", state),
        "volume": p.get("volume_level"),
        "current": current.get("title") if isinstance(current, dict) else None,
    }


def _item_out(i: dict[str, Any]) -> dict[str, Any]:
    artists = i.get("artists")
    return {
        "uri": i.get("uri"),
        "name": i.get("name"),
        "media_type": i.get("media_type"),
        "artists": [a.get("name") for a in artists if isinstance(a, dict)]
        if isinstance(artists, list)
        else None,
    }


@router.get("/players")
async def list_players(
    _: User = Depends(music_user), ma: MAClient = Depends(get_ma)
) -> list[dict[str, Any]]:
    return [_player_out(p) for p in await _run(ma.players())]


@router.get("/search")
async def search(
    q: str = Query(min_length=1, max_length=200),
    media_types: list[MediaType] | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    _: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
) -> dict[str, list[dict[str, Any]]]:
    res = await _run(ma.search(q.strip(), media_types, limit))
    return {
        k: [_item_out(i) for i in v if isinstance(i, dict)]
        for k, v in res.items()
        if isinstance(v, list)
    }


@router.post("/players/{player_id}/play", status_code=204)
async def play(
    player_id: str, body: PlayIn, _: User = Depends(music_user), ma: MAClient = Depends(get_ma)
) -> None:
    await _run(ma.play_media(player_id, body.uri, body.option))


def _cmd(name: str, cmd: str) -> None:
    async def endpoint(
        player_id: str, _: User = Depends(music_user), ma: MAClient = Depends(get_ma)
    ) -> None:
        await _run(ma.queue_cmd(player_id, cmd))

    endpoint.__name__ = name
    router.post(f"/players/{{player_id}}/{name}", status_code=204)(endpoint)


_cmd("pause", "pause")
_cmd("next", "next")
_cmd("previous", "previous")
_cmd("resume", "play")


@router.post("/players/{player_id}/volume", status_code=204)
async def volume(
    player_id: str, body: VolumeIn, _: User = Depends(music_user), ma: MAClient = Depends(get_ma)
) -> None:
    await _run(ma.volume_set(player_id, body.level))
