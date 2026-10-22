"""Groupes d'enceintes : lecture staff, écriture admin. Résolution toujours côté serveur."""

import re
import uuid
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.deps import get_session, require_role
from app.models import Role, Scene, Speaker, SpeakerGroup, User

staff_up = require_role(Role.admin, Role.staff)
admin_only = require_role(Role.admin)

router = APIRouter(prefix="/speaker-groups", tags=["speaker-groups"])

MAX_MEMBERS = 100


def _clean(v: str) -> str:
    v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v).strip()
    if not v:
        raise ValueError("ne doit pas être vide")
    return v


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    member_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_MEMBERS)

    @field_validator("name")
    @classmethod
    def _n(cls, v: str) -> str:
        return _clean(v)


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    member_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_MEMBERS)

    @field_validator("name")
    @classmethod
    def _n(cls, v: str | None) -> str | None:
        return None if v is None else _clean(v)


def _speaker_brief(s: Speaker) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "name": s.name,
        "is_active": s.is_active,
        "ma_player_id": s.ma_player_id,
    }


def out(g: SpeakerGroup) -> dict[str, Any]:
    return {
        "id": str(g.id),
        "name": g.name,
        "description": g.description,
        "is_active": g.is_active,
        "members": [_speaker_brief(s) for s in g.members],
        "member_ids": [str(s.id) for s in g.members],
        "active_count": sum(1 for s in g.members if s.is_active),
    }


async def list_groups(session: AsyncSession, *, active_only: bool = False) -> list[SpeakerGroup]:
    q = select(SpeakerGroup).order_by(SpeakerGroup.name)
    if active_only:
        q = q.where(SpeakerGroup.is_active.is_(True))
    return list((await session.scalars(q)).all())


async def resolve_group(session: AsyncSession, group_id: uuid.UUID) -> list[Speaker]:
    """Enceintes ACTIVES du groupe à l'instant T (404 inconnu, 409 inactif/vide)."""
    g = await session.get(SpeakerGroup, group_id)
    if g is None:
        raise HTTPException(404, "speaker group not found")
    if not g.is_active:
        raise HTTPException(409, f"group '{g.name}' is inactive")
    active = [s for s in g.members if s.is_active]
    if not active:
        raise HTTPException(409, f"group '{g.name}' has no active speaker")
    return active


def dedupe(speakers: Iterable[Speaker]) -> list[Speaker]:
    """Union sans doublon, ordre de première apparition."""
    return list({s.id: s for s in speakers}.values())


async def _members(session: AsyncSession, ids: list[uuid.UUID]) -> list[Speaker]:
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
    return [out(g) for g in await list_groups(session)]


@router.get("/{group_id}")
async def get_one(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    g = await session.get(SpeakerGroup, group_id)
    if g is None:
        raise HTTPException(404, "speaker group not found")
    return out(g)


@router.get("/{group_id}/resolve")
async def resolve(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    active = await resolve_group(session, group_id)
    return {
        "speakers": [_speaker_brief(s) for s in active],
        "player_ids": [s.ma_player_id for s in active if s.ma_player_id],
    }


@router.post("", status_code=201)
async def create(
    body: GroupIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    g = SpeakerGroup(
        name=body.name, description=body.description, is_active=body.is_active, members=[]
    )
    g.members = await _members(session, body.member_ids)
    session.add(g)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "group name already exists") from None
    audit.record(
        session,
        "speaker_group_create",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"group_id": str(g.id), "name": g.name, "members": len(g.members)},
    )
    return out(g)


@router.patch("/{group_id}")
async def update(
    group_id: uuid.UUID,
    body: GroupPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    g = await session.get(SpeakerGroup, group_id)
    if g is None:
        raise HTTPException(404, "speaker group not found")
    changes = body.model_dump(exclude_unset=True)
    if any(changes.get(k, "") is None for k in ("name", "is_active", "member_ids")):
        raise HTTPException(422, "null not allowed")
    if "member_ids" in changes:
        g.members = await _members(session, body.member_ids or [])
        del changes["member_ids"]
        changes["members"] = None
    for k, v in changes.items():
        if k != "members":
            setattr(g, k, v)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "group name already exists") from None
    audit.record(
        session,
        "speaker_group_update",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"group_id": str(g.id), "name": g.name, "fields": sorted(changes)},
    )
    return out(g)


@router.delete("/{group_id}", status_code=204)
async def delete(
    group_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> None:
    g = await session.get(SpeakerGroup, group_id)
    if g is None:
        raise HTTPException(404, "speaker group not found")
    used = await session.scalar(
        select(func.count()).select_from(Scene).where(Scene.group_id == g.id)
    )
    if used:
        raise HTTPException(409, "group is used by a scene")
    await session.delete(g)
    audit.record(
        session,
        "speaker_group_delete",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"group_id": str(g.id), "name": g.name},
    )
