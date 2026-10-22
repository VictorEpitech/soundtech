"""Scènes : lecture staff, lancement staff/admin (limite musique), édition admin.

Une scène réutilise le client MA et les mêmes commandes que `/music` (volume puis lecture) sur
les enceintes résolues côté serveur. Garde-fous :
- jamais pendant un incident d'alerte `sent` (la file/ducking ne protège que les annonces, pas
  une musique lancée après coup) : 409 ;
- seules les enceintes ACTIVES ayant un `ma_player_id` déclaré reçoivent la musique ;
- le mode TEST n'existe pas ici (l'éventuelle annonce n'est jamais un test).
"""

import asyncio
import logging
import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import groups
from app.auth import audit
from app.auth.deps import get_session, require_role
from app.config import Settings, get_settings
from app.ha import service as ha_service
from app.ha.router import get_redis
from app.ma.client import MAAuthError, MAClient, MAConnectionError, MAError
from app.ma.router import declared_players, get_ma, music_user
from app.models import (
    Announcement,
    AnnouncementStatus,
    Incident,
    Preset,
    Role,
    Scene,
    Speaker,
    SpeakerGroup,
    User,
)

log = logging.getLogger("sound.scenes")

staff_up = require_role(Role.admin, Role.staff)
admin_only = require_role(Role.admin)

router = APIRouter(prefix="/scenes", tags=["scenes"])

MAX_SPEAKERS = 100
ICON_PATTERN = r"^[a-z0-9_]{1,64}$"
COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"
URI_PATTERN = r"^\S{3,500}$"
MediaType = Literal["playlist", "album", "track", "radio"]
Mode = Literal["play", "replace"]
Repeat = Literal["off", "one", "all"]
POLL_STEP = 0.5


def _clean(v: str) -> str:
    v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v).strip()
    if not v:
        raise ValueError("ne doit pas être vide")
    return v


class SceneBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    icon: str | None = Field(default=None, pattern=ICON_PATTERN)
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    group_id: uuid.UUID | None = None
    speaker_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SPEAKERS)
    media_uri: str = Field(pattern=URI_PATTERN)
    media_type: MediaType
    mode: Mode = "play"
    volume: int | None = Field(default=None, ge=0, le=100)
    shuffle: bool | None = None
    repeat: Repeat | None = None
    preset_id: uuid.UUID | None = None
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        return _clean(v)


class SceneIn(SceneBase):
    pass


class ScenePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    icon: str | None = Field(default=None, pattern=ICON_PATTERN)
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    group_id: uuid.UUID | None = None
    speaker_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_SPEAKERS)
    media_uri: str | None = Field(default=None, pattern=URI_PATTERN)
    media_type: MediaType | None = None
    mode: Mode | None = None
    volume: int | None = Field(default=None, ge=0, le=100)
    shuffle: bool | None = None
    repeat: Repeat | None = None
    preset_id: uuid.UUID | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _n(cls, v: str | None) -> str | None:
        return None if v is None else _clean(v)


NOT_NULL = ("name", "media_uri", "media_type", "mode", "is_active", "speaker_ids")


def out(sc: Scene, group_name: str | None = None) -> dict[str, Any]:
    return {
        "id": str(sc.id),
        "name": sc.name,
        "icon": sc.icon,
        "color": sc.color,
        "group_id": str(sc.group_id) if sc.group_id else None,
        "group_name": group_name,
        "speaker_ids": [str(s.id) for s in sc.speakers],
        "speaker_names": [s.name for s in sc.speakers],
        "media_uri": sc.media_uri,
        "media_type": sc.media_type,
        "mode": sc.mode,
        "volume": sc.volume,
        "shuffle": sc.shuffle,
        "repeat": sc.repeat,
        "preset_id": str(sc.preset_id) if sc.preset_id else None,
        "is_active": sc.is_active,
    }


async def _group_names(session: AsyncSession) -> dict[uuid.UUID, str]:
    return {i: n for i, n in (await session.execute(select(SpeakerGroup.id, SpeakerGroup.name)))}


async def list_scenes(session: AsyncSession, *, active_only: bool = False) -> list[dict[str, Any]]:
    q = select(Scene).order_by(Scene.name)
    if active_only:
        q = q.where(Scene.is_active.is_(True))
    names = await _group_names(session)
    return [out(sc, names.get(sc.group_id)) for sc in (await session.scalars(q)).all()]


async def _check_refs(
    session: AsyncSession, group_id: uuid.UUID | None, preset_id: uuid.UUID | None
) -> None:
    if group_id is not None and await session.get(SpeakerGroup, group_id) is None:
        raise HTTPException(404, "speaker group not found")
    if preset_id is not None and await session.get(Preset, preset_id) is None:
        raise HTTPException(404, "preset not found")


async def _speakers(session: AsyncSession, ids: list[uuid.UUID]) -> list[Speaker]:
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    rows = (await session.scalars(select(Speaker).where(Speaker.id.in_(ids)))).all()
    if len(rows) != len(ids):
        raise HTTPException(404, "speaker not found")
    return list(rows)


@router.get("")
async def list_all(
    session: AsyncSession = Depends(get_session), _: User = Depends(staff_up)
) -> list[dict[str, Any]]:
    return await list_scenes(session)


@router.get("/{scene_id}")
async def get_one(
    scene_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    sc = await session.get(Scene, scene_id)
    if sc is None:
        raise HTTPException(404, "scene not found")
    return out(sc, (await _group_names(session)).get(sc.group_id))


@router.post("", status_code=201)
async def create(
    body: SceneIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    if body.group_id is None and not body.speaker_ids:
        raise HTTPException(422, "a group or at least one speaker is required")
    await _check_refs(session, body.group_id, body.preset_id)
    data = body.model_dump(exclude={"speaker_ids"})
    sc = Scene(**data, speakers=[])
    sc.speakers = await _speakers(session, body.speaker_ids)
    session.add(sc)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "scene name already exists") from None
    audit.record(
        session,
        "scene_create",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"scene_id": str(sc.id), "name": sc.name},
    )
    return out(sc, (await _group_names(session)).get(sc.group_id))


@router.patch("/{scene_id}")
async def update(
    scene_id: uuid.UUID,
    body: ScenePatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    sc = await session.get(Scene, scene_id)
    if sc is None:
        raise HTTPException(404, "scene not found")
    changes = body.model_dump(exclude_unset=True)
    if any(changes.get(k, "") is None for k in NOT_NULL):
        raise HTTPException(422, "null not allowed")
    await _check_refs(session, changes.get("group_id"), changes.get("preset_id"))
    if "speaker_ids" in changes:
        sc.speakers = await _speakers(session, changes.pop("speaker_ids"))
        fields = [*changes, "speaker_ids"]
    else:
        fields = list(changes)
    for k, v in changes.items():
        setattr(sc, k, v)
    if sc.group_id is None and not sc.speakers:
        await session.rollback()
        raise HTTPException(422, "a group or at least one speaker is required")
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "scene name already exists") from None
    audit.record(
        session,
        "scene_update",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"scene_id": str(sc.id), "name": sc.name, "fields": sorted(fields)},
    )
    return out(sc, (await _group_names(session)).get(sc.group_id))


@router.delete("/{scene_id}", status_code=204)
async def delete(
    scene_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> None:
    sc = await session.get(Scene, scene_id)
    if sc is None:
        raise HTTPException(404, "scene not found")
    await session.delete(sc)
    audit.record(
        session,
        "scene_delete",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"scene_id": str(sc.id), "name": sc.name},
    )


# --- lancement --------------------------------------------------------------


async def resolve_targets(session: AsyncSession, sc: Scene) -> list[Speaker]:
    """Groupe + enceintes explicites, actives uniquement, sans doublon. 409 si rien."""
    found: list[Speaker] = [s for s in sc.speakers if s.is_active]
    group_error: HTTPException | None = None
    if sc.group_id is not None:
        try:
            found = [*await groups.resolve_group(session, sc.group_id), *found]
        except HTTPException as e:
            group_error = e
    targets = groups.dedupe(found)
    if not targets:
        raise group_error or HTTPException(409, "scene has no active speaker")
    return targets


async def _wait_announcement(
    session: AsyncSession,
    ann_id: uuid.UUID,
    timeout: float,  # noqa: ASYNC109
) -> str | None:
    """Attend (borné) la fin de l'annonce ; renvoie son statut, ou None si non terminée."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        status = await session.scalar(select(Announcement.status).where(Announcement.id == ann_id))
        if status in (AnnouncementStatus.done.value, AnnouncementStatus.failed.value):
            return str(status)
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(POLL_STEP)


ALERT_IN_PROGRESS = "an alert incident is in progress"


async def _incident_in_progress(session: AsyncSession) -> bool:
    n = await session.scalar(
        select(func.count()).select_from(Incident).where(Incident.status == "sent")
    )
    return bool(n)


async def _play_on(ma: MAClient, player_id: str, sc: Scene) -> None:
    if sc.volume is not None:
        await ma.volume_set(player_id, sc.volume)
    await ma.play_media(player_id, sc.media_uri, sc.mode)
    if sc.shuffle is not None:
        await ma.queue_shuffle(player_id, sc.shuffle)
    if sc.repeat is not None:
        await ma.queue_repeat(player_id, sc.repeat)


async def _safe_play(ma: MAClient, player_id: str, sc: Scene) -> str | None:
    try:
        await _play_on(ma, player_id, sc)
    except MAConnectionError:
        return "unreachable"
    except MAAuthError:
        return "refused"
    except MAError:
        return "error"
    return None


@router.post("/{scene_id}/launch")
async def launch(
    scene_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(music_user),
    ma: MAClient = Depends(get_ma),
    declared: dict[str, str] = Depends(declared_players),
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    sc = await session.get(Scene, scene_id)
    if sc is None:
        raise HTTPException(404, "scene not found")
    if not sc.is_active:
        raise HTTPException(409, "scene is inactive")
    if await _incident_in_progress(session):
        raise HTTPException(409, ALERT_IN_PROGRESS)
    targets = await resolve_targets(session, sc)
    players: list[tuple[Speaker, str]] = []
    for s in targets:  # un player MA n'est piloté qu'une fois, même partagé par deux enceintes
        if s.ma_player_id in declared and all(s.ma_player_id != p for _, p in players):
            players.append((s, s.ma_player_id))
    if not players:
        raise HTTPException(409, "no music player among the scene targets")

    announcement: str | None = None
    ann_id: str | None = None
    if sc.preset_id is not None:
        preset = await session.get(Preset, sc.preset_id)
        if preset is not None and preset.is_active and settings.ha_configured:
            ann = ha_service.build_announcement(
                settings,
                text=preset.text,
                speakers=targets,
                is_test=False,
                priority=ha_service.priority_for(user),
                author=user,
            )
            try:
                await ha_service.submit(session, redis, ann, request=request)
                ann_id = str(ann.id)
                announcement = (
                    await _wait_announcement(session, ann.id, settings.scene_announce_wait)
                    or "pending"
                )
            except Exception:
                log.exception("scene announcement failed")
                announcement = "failed"
        else:
            announcement = "skipped"

    if await _incident_in_progress(session):  # une alerte a pu partir pendant l'attente
        audit.record(
            session,
            "scene_launch_refused",
            request=request,
            user_id=user.id,
            actor=user.email,
            detail={
                "scene_id": str(sc.id),
                "name": sc.name,
                "reason": "incident",
                "announcement_id": ann_id,
            },
        )
        await session.commit()
        raise HTTPException(409, ALERT_IN_PROGRESS)

    errors = await asyncio.gather(*(_safe_play(ma, pid, sc) for _, pid in players))
    ok = [s.name for (s, _), e in zip(players, errors, strict=True) if e is None]
    failed = [
        {"player": s.name, "error": e}
        for (s, _), e in zip(players, errors, strict=True)
        if e is not None
    ]
    audit.record(
        session,
        "scene_launch",
        request=request,
        user_id=user.id,
        actor=user.email,
        detail={
            "scene_id": str(sc.id),
            "name": sc.name,
            "targets": [s.entity_id for s in targets],
            "players": [pid for _, pid in players],
            "ok": len(ok),
            "failed": len(failed),
            "announcement_id": ann_id,
            "announcement": announcement,
        },
    )
    if not ok:
        await session.commit()
        raise HTTPException(502, "no player could start the scene")
    return {
        "scene": sc.name,
        "ok": ok,
        "failed": failed,
        "partial": bool(failed),
        "announcement": announcement,
    }
