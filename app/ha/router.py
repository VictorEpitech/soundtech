import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import groups
from app.auth import audit
from app.auth.deps import get_session, require_role
from app.config import Settings, get_settings
from app.ha import history, service
from app.ha.ssml import STYLES, build_ssml, validate_style, validate_voice_lang
from app.models import AnnounceDefaults, Announcement, Preset, Role, Speaker, User
from app.ratelimit import per_user

admin_only = require_role(Role.admin)
staff_up = require_role(Role.admin, Role.staff)
announce_user = per_user("announce", "rl_announce_limit", staff_up)

speakers_router = APIRouter(prefix="/speakers", tags=["speakers"])
presets_router = APIRouter(prefix="/presets", tags=["presets"])
announcements_router = APIRouter(prefix="/announcements", tags=["announcements"])

MAX_PRESET_LEN = 1000
MAX_TARGETS = 50
NOTIFY_SERVICE_PATTERN = r"^[a-z0-9_]+\.[a-z0-9_]+$"


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


def _clean(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("ne doit pas être vide")
    return v


# --- speakers ---------------------------------------------------------------


class SpeakerIn(BaseModel):
    entity_id: str = Field(min_length=3, max_length=255, pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=255)
    is_active: bool = True
    group: str | None = Field(default=None, max_length=100)
    notify_service: str | None = Field(default=None, max_length=255, pattern=NOTIFY_SERVICE_PATTERN)
    ma_player_id: str | None = Field(
        default=None, min_length=1, max_length=255, pattern=r"^\S(.*\S)?$"
    )

    _name = field_validator("name")(_clean)


class SpeakerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None
    group: str | None = Field(default=None, max_length=100)
    notify_service: str | None = Field(default=None, max_length=255, pattern=NOTIFY_SERVICE_PATTERN)
    ma_player_id: str | None = Field(
        default=None, min_length=1, max_length=255, pattern=r"^\S(.*\S)?$"
    )

    @field_validator("name")
    @classmethod
    def _n(cls, v: str | None) -> str | None:
        return None if v is None else _clean(v)


def _speaker_out(s: Speaker) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "entity_id": s.entity_id,
        "name": s.name,
        "is_active": s.is_active,
        "group": s.group,
        "notify_service": s.notify_service,
        "ma_player_id": s.ma_player_id,
    }


@speakers_router.get("")
async def list_speakers(
    session: AsyncSession = Depends(get_session), _: User = Depends(staff_up)
) -> list[dict[str, Any]]:
    rows = await session.scalars(select(Speaker).order_by(Speaker.name))
    return [_speaker_out(s) for s in rows]


@speakers_router.get("/{speaker_id}")
async def get_speaker(
    speaker_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    s = await session.get(Speaker, speaker_id)
    if s is None:
        raise HTTPException(404, "speaker not found")
    return _speaker_out(s)


@speakers_router.post("", status_code=201)
async def create_speaker(
    body: SpeakerIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    s = Speaker(**body.model_dump())
    session.add(s)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(409, "entity_id already exists") from None
    audit.record(
        session,
        "speaker_create",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"entity_id": s.entity_id},
    )
    return _speaker_out(s)


@speakers_router.patch("/{speaker_id}")
async def update_speaker(
    speaker_id: uuid.UUID,
    body: SpeakerPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    s = await session.get(Speaker, speaker_id)
    if s is None:
        raise HTTPException(404, "speaker not found")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("name", "") is None or changes.get("is_active", False) is None:
        raise HTTPException(422, "null not allowed")
    for k, v in changes.items():
        setattr(s, k, v)
    audit.record(
        session,
        "speaker_update",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"entity_id": s.entity_id, "fields": sorted(changes)},
    )
    return _speaker_out(s)


@speakers_router.delete("/{speaker_id}", status_code=204)
async def delete_speaker(
    speaker_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> None:
    s = await session.get(Speaker, speaker_id)
    if s is None:
        raise HTTPException(404, "speaker not found")
    await session.delete(s)
    audit.record(
        session,
        "speaker_delete",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"entity_id": s.entity_id},
    )


# --- presets ----------------------------------------------------------------


class PresetIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=MAX_PRESET_LEN)
    is_active: bool = True

    _title = field_validator("title")(_clean)
    _text = field_validator("text")(_clean)


class PresetPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    text: str | None = Field(default=None, min_length=1, max_length=MAX_PRESET_LEN)
    is_active: bool | None = None

    @field_validator("title", "text")
    @classmethod
    def _c(cls, v: str | None) -> str | None:
        return None if v is None else _clean(v)


def _preset_out(p: Preset) -> dict[str, Any]:
    return {"id": str(p.id), "title": p.title, "text": p.text, "is_active": p.is_active}


@presets_router.get("")
async def list_presets(
    session: AsyncSession = Depends(get_session), _: User = Depends(staff_up)
) -> list[dict[str, Any]]:
    rows = await session.scalars(select(Preset).order_by(Preset.title))
    return [_preset_out(p) for p in rows]


@presets_router.get("/{preset_id}")
async def get_preset(
    preset_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    p = await session.get(Preset, preset_id)
    if p is None:
        raise HTTPException(404, "preset not found")
    return _preset_out(p)


@presets_router.post("", status_code=201)
async def create_preset(
    body: PresetIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    p = Preset(**body.model_dump())
    session.add(p)
    await session.flush()
    audit.record(
        session,
        "preset_create",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"preset_id": str(p.id)},
    )
    return _preset_out(p)


@presets_router.patch("/{preset_id}")
async def update_preset(
    preset_id: uuid.UUID,
    body: PresetPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    p = await session.get(Preset, preset_id)
    if p is None:
        raise HTTPException(404, "preset not found")
    changes = body.model_dump(exclude_unset=True)
    if any(v is None for v in changes.values()):
        raise HTTPException(422, "null not allowed")
    for k, v in changes.items():
        setattr(p, k, v)
    audit.record(
        session,
        "preset_update",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"preset_id": str(p.id), "fields": sorted(changes)},
    )
    return _preset_out(p)


@presets_router.delete("/{preset_id}", status_code=204)
async def delete_preset(
    preset_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> None:
    p = await session.get(Preset, preset_id)
    if p is None:
        raise HTTPException(404, "preset not found")
    await session.delete(p)
    audit.record(
        session,
        "preset_delete",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"preset_id": str(p.id)},
    )


# --- announcements ----------------------------------------------------------


class AnnouncementIn(BaseModel):
    """Aucun champ de priorité : le niveau vient du rôle."""

    text: str | None = None
    preset_id: uuid.UUID | None = None
    speaker_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_TARGETS)
    group_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_TARGETS)
    is_test: bool = False
    voice: str | None = Field(default=None, max_length=32)
    lang: str | None = Field(default=None, max_length=16)
    style: str | None = Field(default=None, max_length=16)
    force_volume: bool | None = None  # None = réglage de config ; False = ne pas forcer

    @model_validator(mode="after")
    def _one_source(self) -> "AnnouncementIn":
        if (self.text is None) == (self.preset_id is None):
            raise ValueError("fournir exactement un de text ou preset_id")
        if not self.speaker_ids and not self.group_ids:
            raise ValueError("fournir au moins une enceinte ou un groupe")
        return self


class DefaultsIn(BaseModel):
    voice: str | None = Field(default=None, max_length=32)
    lang: str | None = Field(default=None, max_length=16)


def _ann_out(a: Announcement) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "author_email": a.author_email,
        "text": a.text,
        "priority": a.priority,
        "is_test": a.is_test,
        "voice": a.voice,
        "lang": a.lang,
        "style": a.style,
        "status": a.status,
        "targets": a.targets,
        "results": a.results,
        "created_at": a.created_at.isoformat() if isinstance(a.created_at, datetime) else None,
        "started_at": a.started_at.isoformat() if a.started_at else None,
        "finished_at": a.finished_at.isoformat() if a.finished_at else None,
    }


async def get_defaults(session: AsyncSession, settings: Settings) -> dict[str, str | None]:
    row = await session.get(AnnounceDefaults, 1)
    if row is not None:
        return {"voice": row.voice, "lang": row.lang}
    return {"voice": settings.announce_default_voice, "lang": settings.announce_default_lang}


async def _voice_lang(
    session: AsyncSession, settings: Settings, body: AnnouncementIn
) -> tuple[str | None, str | None]:
    validate_voice_lang(settings, body.voice, body.lang)
    d = await get_defaults(session, settings)
    voice, lang = body.voice or d["voice"], body.lang or d["lang"]
    # un préréglage devenu invalide (config modifiée) ne doit pas bloquer l'envoi
    if voice not in settings.announce_voices:
        voice = None
    if lang not in settings.announce_langs:
        lang = None
    return voice, lang


def _style(body: AnnouncementIn) -> str | None:
    return validate_style(body.style) or None


async def _text_of(session: AsyncSession, body: AnnouncementIn) -> str:
    if body.preset_id is not None:
        preset = await session.get(Preset, body.preset_id)
        if preset is None or not preset.is_active:
            raise HTTPException(404, "preset not found")
        return preset.text
    return (body.text or "").strip()


async def _resolve_speakers(
    session: AsyncSession, body: AnnouncementIn, *, skip_inactive: bool = False
) -> tuple[list[Speaker], list[Speaker]]:
    """(enceintes retenues, inactives ignorées). Sans `skip_inactive`, une inactive donne 409."""
    ids = list(dict.fromkeys(body.speaker_ids))
    found = (await session.scalars(select(Speaker).where(Speaker.id.in_(ids)))).all()
    by_id = {s.id: s for s in found}
    if len(by_id) != len(ids):
        raise HTTPException(404, "speaker not found")
    ignored = [s for s in found if not s.is_active]
    if ignored and not skip_inactive:
        raise HTTPException(409, "speaker inactive")
    from_groups: list[Speaker] = []
    for gid in dict.fromkeys(body.group_ids):
        try:
            from_groups += await groups.resolve_group(session, gid)
        except HTTPException as exc:
            if not (skip_inactive and exc.status_code == 409):
                raise
    chosen = groups.dedupe([by_id[i] for i in ids if by_id[i].is_active] + from_groups)
    return chosen, ignored


@announcements_router.post("", status_code=202)
async def create_announcement(
    body: AnnouncementIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    user: User = Depends(announce_user),
) -> dict[str, Any]:
    if not settings.ha_configured:
        raise HTTPException(503, "home assistant not configured")
    voice, lang = await _voice_lang(session, settings, body)
    style = _style(body)
    text = await _text_of(session, body)
    if not text:
        raise HTTPException(422, "text must not be empty")
    if len(text) > settings.announcement_max_length:
        raise HTTPException(422, f"text too long (max {settings.announcement_max_length})")
    speakers, _ = await _resolve_speakers(session, body)
    ann = service.build_announcement(
        settings,
        text=text,
        speakers=speakers,
        is_test=body.is_test,
        priority=service.priority_for(user),
        author=user,
        voice=voice,
        lang=lang,
        style=style,
        force_volume=body.force_volume,
    )
    try:
        await service.submit(session, redis, ann, request=request)
    except Exception:
        raise HTTPException(503, "queue unavailable") from None
    return _ann_out(ann)


@announcements_router.post("/preview")
async def preview_announcement(
    body: AnnouncementIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    user: User = Depends(staff_up),
) -> dict[str, Any]:
    """Aucun effet : ni écriture, ni audit, ni file."""
    voice, lang = await _voice_lang(session, settings, body)
    style = _style(body)
    text = await _text_of(session, body)
    if not text:
        raise HTTPException(422, "text must not be empty")
    speakers, ignored = await _resolve_speakers(session, body, skip_inactive=True)
    final = service.final_text(settings, text, body.is_test)
    seconds = max(1, round(len(final.split()) * 60 / max(1, settings.announce_wpm)))
    warnings: list[str] = []
    if len(text) > settings.announcement_max_length:
        warnings.append(f"Texte trop long ({len(text)} / {settings.announcement_max_length})")
    if not speakers:
        warnings.append("Aucune enceinte active ciblée")
    warnings += [
        f"Enceinte « {s.name} » sans notify_service" for s in speakers if not s.notify_service
    ]
    if (voice or lang or style) and any(not s.notify_service for s in speakers):
        warnings.append("Voix/langue/style ignorés sur les enceintes sans notify_service")
    return {
        "text": final,
        "is_test": body.is_test,
        "voice": voice,
        "lang": lang,
        "style": style,
        "style_label": STYLES.get(style) if style else None,
        "ssml": build_ssml(
            text,
            voice,
            lang,
            prefix=settings.test_prefix if body.is_test else None,
            style=style,
        ),
        "speakers": [
            {"id": str(s.id), "name": s.name, "has_notify_service": bool(s.notify_service)}
            for s in speakers
        ],
        "ignored": [{"id": str(s.id), "name": s.name} for s in ignored],
        "estimated_seconds": seconds,
        "volume": service.volume_level(settings, service.priority_for(user), body.force_volume)
        or None,
        "warnings": warnings,
    }


@announcements_router.get("/defaults")
async def read_defaults(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: User = Depends(staff_up),
) -> dict[str, Any]:
    return {
        **await get_defaults(session, settings),
        "voices": settings.announce_voices,
        "langs": settings.announce_langs,
    }


@announcements_router.put("/defaults")
async def update_defaults(
    body: DefaultsIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    admin: User = Depends(admin_only),
) -> dict[str, Any]:
    voice, lang = validate_voice_lang(settings, body.voice, body.lang)
    row = await session.get(AnnounceDefaults, 1)
    if row is None:
        row = AnnounceDefaults(id=1)
        session.add(row)
    row.voice, row.lang = voice, lang
    audit.record(
        session,
        "announce_defaults_update",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={"voice": voice, "lang": lang},
    )
    return {"voice": voice, "lang": lang}


@announcements_router.get("")
async def list_announcements(
    response: Response,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    filters: history.Filters = Depends(history.filters_dep),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(staff_up),
) -> list[dict[str, Any]]:
    rows, total = await history.search(session, filters, limit=limit, offset=offset, viewer=user)
    response.headers["X-Total-Count"] = str(total)
    return [_ann_out(a) for a in rows]


@announcements_router.get("/{announcement_id}")
async def get_announcement(
    announcement_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(staff_up),
) -> dict[str, Any]:
    a = await session.get(Announcement, announcement_id)
    if a is None or (user.role != Role.admin and a.author_id != user.id):
        raise HTTPException(404, "announcement not found")
    return _ann_out(a)


@announcements_router.delete("/{announcement_id}", status_code=204)
async def delete_announcement(
    announcement_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(admin_only),
) -> Response:
    a = await session.get(Announcement, announcement_id)
    if a is None:
        raise HTTPException(404, "announcement not found")
    audit.record(
        session,
        "announcement_delete",
        request=request,
        user_id=admin.id,
        actor=admin.email,
        detail={
            "announcement_id": str(a.id),
            "author_email": a.author_email,
            "status": a.status,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        },
    )
    await session.delete(a)
    return Response(status_code=204)
