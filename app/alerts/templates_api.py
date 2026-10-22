"""Modèles d'alerte : un par type d'incident. Lecture staff, écriture admin."""

import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.deps import get_session, require_role
from app.models import AlertTemplate, IncidentType, Role, User

staff_up = require_role(Role.admin, Role.staff)
admin_only = require_role(Role.admin)

router = APIRouter(prefix="/alert-templates", tags=["alert-templates"])

LABEL_MAX = 60
TITLE_MAX = 255
MESSAGE_MAX = 500
TYPE_ORDER = [t.value for t in IncidentType]
ICON_PATTERN = r"^[a-z0-9_]{1,64}$"
COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"


def _clean(v: str) -> str:
    v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v).strip()
    if not v:
        raise ValueError("ne doit pas être vide")
    return v


class TemplateBase(BaseModel):
    label: str = Field(min_length=1, max_length=LABEL_MAX)
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    message: str = Field(min_length=1, max_length=MESSAGE_MAX)
    icon: str | None = Field(default=None, pattern=ICON_PATTERN)
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    is_active: bool = True

    @field_validator("label", "title", "message")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _clean(v)


class TemplateIn(TemplateBase):
    type: IncidentType


class TemplatePatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX)
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    message: str | None = Field(default=None, min_length=1, max_length=MESSAGE_MAX)
    icon: str | None = Field(default=None, pattern=ICON_PATTERN)
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    is_active: bool | None = None

    @field_validator("label", "title", "message")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return None if v is None else _clean(v)


def out(t: AlertTemplate) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "type": t.type,
        "label": t.label,
        "title": t.title,
        "message": t.message,
        "icon": t.icon,
        "color": t.color,
        "is_active": t.is_active,
    }


async def list_templates(
    session: AsyncSession, *, active_only: bool = False
) -> list[AlertTemplate]:
    q = select(AlertTemplate)
    if active_only:
        q = q.where(AlertTemplate.is_active.is_(True))
    rows = list((await session.scalars(q)).all())
    rows.sort(key=lambda t: TYPE_ORDER.index(t.type) if t.type in TYPE_ORDER else len(TYPE_ORDER))
    return rows


@router.get("")
async def list_all(
    session: AsyncSession = Depends(get_session), _: User = Depends(staff_up)
) -> list[dict[str, Any]]:
    return [out(t) for t in await list_templates(session)]


@router.get("/{template_id}")
async def get_one(
    template_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    t = await session.get(AlertTemplate, template_id)
    if t is None:
        raise HTTPException(404, "alert template not found")
    return out(t)


@router.post("", status_code=201)
async def create(
    body: TemplateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    t = AlertTemplate(**{**body.model_dump(), "type": body.type.value})
    session.add(t)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "a template already exists for this type") from None
    audit.record(
        session,
        "alert_template_create",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"template_id": str(t.id), "type": t.type},
    )
    return out(t)


@router.patch("/{template_id}")
async def update(
    template_id: uuid.UUID,
    body: TemplatePatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    t = await session.get(AlertTemplate, template_id)
    if t is None:
        raise HTTPException(404, "alert template not found")
    changes = body.model_dump(exclude_unset=True)
    if any(v is None for k, v in changes.items() if k not in ("icon", "color")):
        raise HTTPException(422, "null not allowed")
    for k, v in changes.items():
        setattr(t, k, v)
    audit.record(
        session,
        "alert_template_update",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"template_id": str(t.id), "type": t.type, "fields": sorted(changes)},
    )
    return out(t)


@router.delete("/{template_id}", status_code=204)
async def delete(
    template_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> None:
    t = await session.get(AlertTemplate, template_id)
    if t is None:
        raise HTTPException(404, "alert template not found")
    await session.delete(t)
    audit.record(
        session,
        "alert_template_delete",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"template_id": str(t.id), "type": t.type},
    )
