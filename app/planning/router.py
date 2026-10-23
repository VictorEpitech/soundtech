"""API planification : schedules, journal et calendrier de fermeture (admin écrit, staff lit)."""

import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.deps import get_session, require_role
from app.config import Settings, get_settings
from app.ha.router import get_redis
from app.ma.client import MAClient
from app.models import (
    Closure,
    PlanningSettings,
    Preset,
    Role,
    Scene,
    Schedule,
    ScheduleRun,
    ScheduleRunStatus,
    Speaker,
    SpeakerGroup,
    User,
)
from app.planning import service
from app.planning.holidays import fr_holidays
from app.planning.recurrence import (
    TIME_PATTERN,
    load_tz,
    local_to_utc,
    next_occurrences,
    parse_time,
)
from app.ratelimit import per_user

staff_up = require_role(Role.admin, Role.staff)
admin_only = require_role(Role.admin)
plan_write = per_user("planning", "rl_planning_limit", admin_only)
plan_run = per_user("planning_run", "rl_planning_run_limit", admin_only)

router = APIRouter(prefix="/schedules", tags=["planning"])
closures_router = APIRouter(prefix="/closures", tags=["planning"])

MAX_TARGETS = 100
MAX_CLOSURE_DAYS = 400
PREVIEW_COUNT = 10
Action = Literal["announcement", "scene", "stop"]
ClosureKindT = Literal["vacances", "fermeture", "ferie"]


def _clean(v: str) -> str:
    v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v).strip()
    if not v:
        raise ValueError("ne doit pas être vide")
    return v


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    is_active: bool = True
    timezone: str = Field(default="Europe/Paris", max_length=64)
    days: list[int] = Field(default_factory=list, max_length=7)
    time_of_day: str = Field(pattern=TIME_PATTERN)
    once_date: date | None = None
    action: Action
    text: str | None = Field(default=None, max_length=1000)
    preset_id: uuid.UUID | None = None
    voice: str | None = Field(default=None, max_length=32)
    lang: str | None = Field(default=None, max_length=16)
    group_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_TARGETS)
    speaker_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_TARGETS)
    scene_id: uuid.UUID | None = None
    stop_volume: int | None = Field(default=None, ge=0, le=100)
    is_test: bool = False
    respect_closures: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean(v)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        load_tz(v)
        return v

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d < 1 or d > 7 for d in v):
            raise ValueError("jours ISO attendus : 1 (lundi) à 7 (dimanche)")
        return sorted(set(v))

    @field_validator("text")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        return _clean(v) if v is not None and v.strip() else None

    @model_validator(mode="after")
    def _coherent(self) -> "ScheduleIn":
        if bool(self.days) == (self.once_date is not None):
            raise ValueError("choisir soit des jours de semaine, soit une date unique")
        if self.action == "announcement":
            if (self.text is None) == (self.preset_id is None):
                raise ValueError("un texte libre OU un message pré-enregistré est requis")
            if not (self.group_ids or self.speaker_ids):
                raise ValueError("au moins un groupe ou une enceinte est requis")
            self.scene_id, self.stop_volume = None, None
        elif self.action == "scene":
            if self.scene_id is None:
                raise ValueError("une scène est requise")
            self.text = self.preset_id = self.voice = self.lang = self.stop_volume = None
            self.group_ids, self.speaker_ids = [], []
        else:
            if not (self.group_ids or self.speaker_ids):
                raise ValueError("au moins un groupe ou une enceinte est requis")
            self.text = self.preset_id = self.voice = self.lang = self.scene_id = None
        return self


class SchedulePatch(BaseModel):
    """Champs optionnels ; le résultat fusionné est re-validé par `ScheduleIn`."""

    model_config = {"extra": "forbid"}
    name: str | None = None
    is_active: bool | None = None
    timezone: str | None = None
    days: list[int] | None = None
    time_of_day: str | None = None
    once_date: date | None = None
    action: Action | None = None
    text: str | None = None
    preset_id: uuid.UUID | None = None
    voice: str | None = None
    lang: str | None = None
    group_ids: list[uuid.UUID] | None = None
    speaker_ids: list[uuid.UUID] | None = None
    scene_id: uuid.UUID | None = None
    stop_volume: int | None = None
    is_test: bool | None = None
    respect_closures: bool | None = None


FIELDS = tuple(ScheduleIn.model_fields)
RECURRENCE_FIELDS = {"days", "time_of_day", "once_date", "timezone"}


def out(sc: Schedule, next_run: datetime | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {k: getattr(sc, k) for k in FIELDS}
    d.update(
        id=str(sc.id),
        once_date=sc.once_date.isoformat() if sc.once_date else None,
        preset_id=str(sc.preset_id) if sc.preset_id else None,
        scene_id=str(sc.scene_id) if sc.scene_id else None,
        group_ids=list(sc.group_ids or []),
        speaker_ids=list(sc.speaker_ids or []),
        days=list(sc.days or []),
        created_by_email=sc.created_by_email,
        last_run_at=sc.last_run_at.isoformat() if sc.last_run_at else None,
        next_run=next_run.isoformat() if next_run else None,
    )
    return d


def _future_check(body: ScheduleIn) -> None:
    if body.once_date is None:
        return
    when = local_to_utc(body.once_date, parse_time(body.time_of_day), load_tz(body.timezone))
    if when <= datetime.now(UTC):
        raise HTTPException(422, "la date unique doit être dans le futur")


async def _check_refs(session: AsyncSession, body: ScheduleIn) -> None:
    async def count(model: Any, ids: list[uuid.UUID], *extra: Any) -> int:
        if not ids:
            return 0
        return int(
            await session.scalar(
                select(func.count()).select_from(model).where(model.id.in_(set(ids)), *extra)
            )
            or 0
        )

    if await count(Speaker, body.speaker_ids) != len(set(body.speaker_ids)):
        raise HTTPException(422, "unknown speaker")
    if await count(SpeakerGroup, body.group_ids) != len(set(body.group_ids)):
        raise HTTPException(422, "unknown speaker group")
    if body.preset_id is not None and not await count(
        Preset, [body.preset_id], Preset.is_active.is_(True)
    ):
        raise HTTPException(422, "unknown or inactive preset")
    if body.scene_id is not None and not await count(Scene, [body.scene_id]):
        raise HTTPException(422, "unknown scene")


def _validated(data: dict[str, Any]) -> ScheduleIn:
    try:
        return ScheduleIn.model_validate(data)
    except ValidationError as e:
        raise HTTPException(
            422, json.loads(e.json(include_url=False, include_context=False))
        ) from None


def _voice_lang_check(settings: Settings, body: ScheduleIn) -> None:
    from app.ha.ssml import validate_voice_lang

    validate_voice_lang(settings, body.voice, body.lang)


def _stored(body: ScheduleIn) -> dict[str, Any]:
    d = body.model_dump()
    d["group_ids"] = [str(i) for i in body.group_ids]
    d["speaker_ids"] = [str(i) for i in body.speaker_ids]
    return d


async def _get(session: AsyncSession, schedule_id: uuid.UUID) -> Schedule:
    sc = await session.get(Schedule, schedule_id)
    if sc is None:
        raise HTTPException(404, "schedule not found")
    return sc


async def next_runs(
    session: AsyncSession, sc: Schedule, count: int, now: datetime | None = None
) -> list[datetime]:
    now = now or datetime.now(UTC)
    is_closed = None
    if sc.respect_closures:
        last = now.date() + timedelta(days=830)
        if sc.once_date is not None:
            last = max(last, sc.once_date + timedelta(days=3))
        is_closed = await service.closure_checker(session, now.date() - timedelta(days=2), last)
    try:
        return next_occurrences(sc, now, count, is_closed)
    except ValueError:
        return []


async def upcoming(session: AsyncSession, limit: int = 3) -> list[dict[str, Any]]:
    """Prochaines exécutions des planifications actives : une requête fermetures, horizon 60 j."""
    now = datetime.now(UTC)
    rows = list(await session.scalars(select(Schedule).where(Schedule.is_active.is_(True))))
    if not rows:
        return []
    last = now.date() + timedelta(days=60)
    for sc in rows:
        if sc.once_date is not None:
            last = max(last, sc.once_date + timedelta(days=3))
    is_closed = None
    if any(sc.respect_closures for sc in rows):
        is_closed = await service.closure_checker(session, now.date() - timedelta(days=2), last)
    res = []
    for sc in rows:
        try:
            nxt = next_occurrences(sc, now, 1, is_closed if sc.respect_closures else None)
        except ValueError:
            continue
        if nxt:
            res.append(out(sc, nxt[0]))
    return sorted(res, key=lambda r: r["next_run"])[:limit]


def _times(sc: Schedule, runs: list[datetime]) -> list[dict[str, str]]:
    tz = load_tz(sc.timezone)
    return [
        {"utc": r.isoformat(), "local": r.astimezone(tz).strftime("%Y-%m-%d %H:%M")} for r in runs
    ]


@router.get("")
async def list_schedules(
    session: AsyncSession = Depends(get_session), _: User = Depends(staff_up)
) -> list[dict[str, Any]]:
    rows = list(await session.scalars(select(Schedule).order_by(Schedule.name)))
    res = []
    for sc in rows:
        nxt = await next_runs(session, sc, 1) if sc.is_active else []
        res.append(out(sc, nxt[0] if nxt else None))
    return res


@router.get("/runs")
async def list_runs(
    schedule_id: uuid.UUID | None = None,
    status: ScheduleRunStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> list[dict[str, Any]]:
    q = select(ScheduleRun).order_by(ScheduleRun.scheduled_for.desc()).limit(limit)
    if schedule_id is not None:
        q = q.where(ScheduleRun.schedule_id == schedule_id)
    if status is not None:
        q = q.where(ScheduleRun.status == status.value)
    return [service.run_out(r) for r in await session.scalars(q)]


@router.get("/{schedule_id}")
async def get_schedule(
    schedule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    sc = await _get(session, schedule_id)
    nxt = await next_runs(session, sc, 1) if sc.is_active else []
    return out(sc, nxt[0] if nxt else None)


@router.get("/{schedule_id}/preview")
async def preview(
    schedule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    sc = await _get(session, schedule_id)
    return {
        "timezone": sc.timezone,
        "next": _times(sc, await next_runs(session, sc, PREVIEW_COUNT)),
    }


@router.post("", status_code=201)
async def create_schedule(
    body: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    admin: User = Depends(plan_write),
) -> dict[str, Any]:
    data = _validated(body)
    _future_check(data)
    _voice_lang_check(settings, data)
    await _check_refs(session, data)
    sc = Schedule(
        **_stored(data), created_by=admin.id, created_by_email=admin.email, id=uuid.uuid4()
    )
    session.add(sc)
    await session.flush()
    await session.refresh(sc)
    audit.record(
        session,
        "schedule_create",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"schedule_id": str(sc.id), "name": sc.name, "action": sc.action},
    )
    nxt = await next_runs(session, sc, 1) if sc.is_active else []
    return out(sc, nxt[0] if nxt else None)


@router.patch("/{schedule_id}")
async def update_schedule(
    schedule_id: uuid.UUID,
    body: SchedulePatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    admin: User = Depends(plan_write),
) -> dict[str, Any]:
    sc = await _get(session, schedule_id)
    patch = body.model_dump(exclude_unset=True)
    if any(patch.get(k, "") is None for k in ("name", "is_active", "timezone", "time_of_day")):
        raise HTTPException(422, "null not allowed")
    current = out(sc)
    merged = {k: current[k] for k in FIELDS} | patch
    data = _validated(merged)
    if RECURRENCE_FIELDS & patch.keys():
        _future_check(data)
    _voice_lang_check(settings, data)
    await _check_refs(session, data)
    new = _stored(data)
    changed = sorted(k for k, v in new.items() if current[k] != _plain(v))
    reactivated = data.is_active and not sc.is_active
    for k, v in new.items():
        setattr(sc, k, v)
    if reactivated or RECURRENCE_FIELDS & set(changed):
        sc.armed_at = datetime.now(UTC)
    await session.flush()
    audit.record(
        session,
        "schedule_update",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"schedule_id": str(sc.id), "name": sc.name, "fields": changed},
    )
    nxt = await next_runs(session, sc, 1) if sc.is_active else []
    return out(sc, nxt[0] if nxt else None)


def _plain(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date) else v


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(plan_write),
) -> None:
    sc = await _get(session, schedule_id)
    audit.record(
        session,
        "schedule_delete",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"schedule_id": str(sc.id), "name": sc.name},
    )
    await session.delete(sc)


@router.post("/{schedule_id}/run")
async def run_schedule_now(
    schedule_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
    admin: User = Depends(plan_run),
) -> dict[str, Any]:
    sc = await _get(session, schedule_id)
    ma = MAClient.from_settings(settings) if settings.ma_configured else None
    run = await service.run_now(session, redis, settings, ma, sc, admin, request)
    if run.status == ScheduleRunStatus.skipped_alert.value:
        raise HTTPException(409, "an alert incident is in progress")
    return service.run_out(run)


# --- calendrier de fermeture -------------------------------------------------


class ClosureIn(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    kind: ClosureKindT = "vacances"
    start_date: date
    end_date: date
    is_active: bool = True

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return _clean(v)

    @model_validator(mode="after")
    def _range(self) -> "ClosureIn":
        if self.end_date < self.start_date:
            raise ValueError("la fin doit suivre le début")
        if (self.end_date - self.start_date).days >= MAX_CLOSURE_DAYS:
            raise ValueError(f"plage limitée à {MAX_CLOSURE_DAYS} jours")
        return self


class ClosurePatch(BaseModel):
    model_config = {"extra": "forbid"}
    label: str | None = None
    kind: ClosureKindT | None = None
    start_date: date | None = None
    end_date: date | None = None
    is_active: bool | None = None


class PlanningSettingsIn(BaseModel):
    observe_holidays: bool


CLOSURE_FIELDS = tuple(ClosureIn.model_fields)


def closure_out(c: Closure) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "label": c.label,
        "kind": c.kind,
        "start_date": c.start_date.isoformat(),
        "end_date": c.end_date.isoformat(),
        "is_active": c.is_active,
    }


async def list_closures(session: AsyncSession) -> list[Closure]:
    return list(await session.scalars(select(Closure).order_by(Closure.start_date)))


def holidays_out(year: int) -> list[dict[str, str]]:
    return [{"date": d.isoformat(), "label": n} for d, n in sorted(fr_holidays(year).items())]


@closures_router.get("")
async def get_calendar(
    year: int | None = Query(default=None, ge=2000, le=2100),
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    year = year or datetime.now(UTC).year
    return {
        "observe_holidays": await service.observe_holidays(session),
        "year": year,
        "holidays": holidays_out(year),
        "closures": [closure_out(c) for c in await list_closures(session)],
    }


@closures_router.put("/settings")
async def put_settings(
    body: PlanningSettingsIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(plan_write),
) -> dict[str, Any]:
    row = await session.get(PlanningSettings, 1)
    if row is None:
        row = PlanningSettings(id=1, observe_holidays=body.observe_holidays)
        session.add(row)
    else:
        row.observe_holidays = body.observe_holidays
    audit.record(
        session,
        "planning_settings_update",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"observe_holidays": body.observe_holidays},
    )
    return {"observe_holidays": body.observe_holidays}


@closures_router.post("", status_code=201)
async def create_closure(
    body: ClosureIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(plan_write),
) -> dict[str, Any]:
    c = Closure(id=uuid.uuid4(), **body.model_dump())
    session.add(c)
    audit.record(
        session,
        "closure_create",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={
            "closure_id": str(c.id),
            "label": c.label,
            "from": str(c.start_date),
            "to": str(c.end_date),
        },
    )
    return closure_out(c)


@closures_router.patch("/{closure_id}")
async def update_closure(
    closure_id: uuid.UUID,
    body: ClosurePatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(plan_write),
) -> dict[str, Any]:
    c = await session.get(Closure, closure_id)
    if c is None:
        raise HTTPException(404, "closure not found")
    patch = body.model_dump(exclude_unset=True)
    if any(v is None for v in patch.values()):
        raise HTTPException(422, "null not allowed")
    cur = closure_out(c)
    try:
        data = ClosureIn.model_validate({k: cur[k] for k in CLOSURE_FIELDS} | patch)
    except ValidationError as e:
        raise HTTPException(
            422, json.loads(e.json(include_url=False, include_context=False))
        ) from None
    for k, v in data.model_dump().items():
        setattr(c, k, v)
    audit.record(
        session,
        "closure_update",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"closure_id": str(c.id), "label": c.label, "fields": sorted(patch)},
    )
    return closure_out(c)


@closures_router.delete("/{closure_id}", status_code=204)
async def delete_closure(
    closure_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(plan_write),
) -> None:
    c = await session.get(Closure, closure_id)
    if c is None:
        raise HTTPException(404, "closure not found")
    audit.record(
        session,
        "closure_delete",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"closure_id": str(c.id), "label": c.label},
    )
    await session.delete(c)
