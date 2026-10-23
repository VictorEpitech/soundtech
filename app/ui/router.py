import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import admin as users_api
from app import groups as groups_api
from app import scenes as scenes_api
from app import students as students_api
from app.alerts import links as links_svc
from app.alerts import responses as responses_svc
from app.alerts import router as alerts_api
from app.alerts import templates_api as alert_templates_api
from app.alerts.roster import RosterProvider
from app.auth.deps import get_session
from app.config import Settings, get_settings
from app.ha import history as history_api
from app.ha import router as ha_api
from app.ha import service as ha_service
from app.ha.ssml import STYLES
from app.ma import router as ma_api
from app.models import (
    Announcement,
    AuditLog,
    Incident,
    IncidentStatus,
    Priority,
    Role,
    ScheduleRunStatus,
    Student,
    User,
)
from app.models import Response as ResponseRow
from app.planning import router as planning_api
from app.ui import pwa
from app.ui.deps import PageError, PageRedirect, admin_page, optional_user, staff_page

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=BASE / "templates")

router = APIRouter(include_in_schema=False)

SEARCH_GROUPS = (
    ("tracks", "Titres"),
    ("albums", "Albums"),
    ("artists", "Artistes"),
    ("playlists", "Playlists"),
)
MEDIA_TYPES = (("playlist", "Playlist"), ("album", "Album"), ("track", "Piste"), ("radio", "Radio"))
ROLE_LABELS = {"admin": "Administrateur", "staff": "Personnel", "student": "Étudiant"}


def render(request: Request, name: str, user: User | None = None, **ctx: Any) -> HTMLResponse:
    base: dict[str, Any] = {"user": user, "role_labels": ROLE_LABELS, "roles": list(Role)}
    if user is not None and user.role in (Role.admin, Role.staff):
        # Mots de confirmation : garde-fou anti-erreur servi à toute page (feuille globale).
        settings = get_settings()
        base["alert_word"] = settings.alert_confirm_text
        base["test_word"] = settings.alert_test_confirm_text
    return templates.TemplateResponse(request, name, {**base, **ctx})


def register(app: FastAPI) -> None:
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.include_router(pwa.router)
    app.include_router(router)

    @app.exception_handler(PageRedirect)
    async def _redirect(request: Request, exc: PageRedirect) -> Response:
        if request.headers.get("HX-Request"):
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        return RedirectResponse(exc.url, status_code=302)

    @app.exception_handler(PageError)
    async def _error(request: Request, exc: PageError) -> Response:
        resp = render(request, "error.html", status=exc.status, message=exc.message)
        resp.status_code = exc.status
        return resp


def _group_rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [groups_api.out(g) for g in rows]


def _fragment_error(request: Request, exc: HTTPException) -> HTMLResponse:
    return render(request, "partials/error.html", message=str(exc.detail))


# --- pages ------------------------------------------------------------------


@router.get("/login")
async def login_page(request: Request, user: User | None = Depends(optional_user)) -> Response:
    if user is not None:
        return RedirectResponse("/", status_code=302)
    return render(request, "login.html")


@router.get("/")
async def home(request: Request, user: User = Depends(staff_page)) -> Response:
    return render(request, "home.html", user, active="home")


@router.get("/music")
async def music_page(
    request: Request,
    user: User = Depends(staff_page),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    ctx = await _players(settings, user, session)
    ctx["scenes"] = await scenes_api.list_scenes(session, active_only=True)
    ctx["groups"] = _group_rows(await groups_api.list_groups(session, active_only=True))
    ctx["incident_active"] = await session.scalar(
        select(Incident).where(Incident.status == IncidentStatus.sent.value).limit(1)
    )
    return render(request, "music.html", user, active="music", **ctx)


@router.get("/announce")
async def announce_page(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    speakers = [s for s in await ha_api.list_speakers(session, user) if s["is_active"]]
    presets = [p for p in await ha_api.list_presets(session, user) if p["is_active"]]
    grp = _group_rows(await groups_api.list_groups(session, active_only=True))
    return render(
        request,
        "announce.html",
        user,
        active="announce",
        speakers=speakers,
        presets=presets,
        groups=grp,
        voices=settings.announce_voices,
        langs=settings.announce_langs,
        styles=STYLES,
        volume_level=ha_service.volume_level(settings, Priority.staff),
        defaults=await ha_api.get_defaults(session, settings),
        all_groups=_group_rows(await groups_api.list_groups(session)),
        all_speakers=await ha_api.list_speakers(session, user),
    )


@router.get("/alerts")
async def alerts_page(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    tpls = [
        alert_templates_api.out(t)
        for t in await alert_templates_api.list_templates(session, active_only=True)
    ]
    return render(
        request,
        "alerts.html",
        user,
        active="alerts",
        templates=tpls,
        alert_word=settings.alert_confirm_text,
        test_word=settings.alert_test_confirm_text,
    )


@router.get("/alerts/{incident_id}")
async def alert_dashboard(
    request: Request,
    incident_id: uuid.UUID,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        incident = await alerts_api.get_incident(incident_id, session, user)
    except HTTPException:
        raise PageError(404, "Incident introuvable") from None
    ann = (
        await session.get(Announcement, uuid.UUID(incident["announcement_id"]))
        if incident["announcement_id"]
        else None
    )
    speakers = [t.get("name") or t.get("entity_id") for t in (ann.targets if ann else [])]
    link_info = None
    if settings.links_active:
        last = await session.scalar(
            select(AuditLog)
            .where(
                AuditLog.action == "webhook_dispatch",
                AuditLog.detail["incident_id"].as_string() == str(incident_id),
            )
            .order_by(AuditLog.ts.desc())
            .limit(1)
        )
        if not settings.alert_webhook_url.get_secret_value():
            channel = "non configuré (mode journal)"
        elif not settings.alert_webhook_enabled:
            channel = "désactivé"
        else:
            channel = "actif"
        link_info = {
            "channel": channel,
            "count": await links_svc.count_links(session, incident_id),
            "last": last.detail if last else None,
            "ttl": settings.response_link_ttl_hours,
        }
    return render(
        request,
        "dashboard.html",
        user,
        active="alerts",
        incident=incident,
        speakers=speakers,
        link_info=link_info,
    )


@router.post("/ui/alerts/{incident_id}/links")
async def student_link_fragment(
    request: Request,
    incident_id: uuid.UUID,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(ha_api.get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    form = parse_qs((await request.body())[:4096].decode("utf-8", "replace"))
    email = (form.get("email") or [""])[0]
    try:
        out = await alerts_api.create_student_link(
            incident_id, alerts_api.LinkIn(email=email), request, session, redis, settings, user
        )
    except HTTPException as e:
        return _fragment_error(request, e)
    return render(
        request, "partials/student_link.html", user, url=out["url"], ttl=out["expires_in_hours"]
    )


ADMIN_SECTIONS = (
    ("/admin/utilisateurs", "users", "group", "Utilisateurs"),
    ("/admin/etudiants", "students", "school", "Étudiants"),
    ("/admin/enceintes", "speakers", "speaker", "Enceintes"),
    ("/admin/musique", "music", "music_note", "Scènes"),
    ("/admin/annonces", "announce", "campaign", "Annonces"),
    ("/admin/alertes", "alerts", "emergency_home", "Alertes"),
)
ADMIN_BLURBS = {
    "users": "Comptes et rôles.",
    "students": "Annuaire des étudiants, import CSV annuel.",
    "speakers": "Enceintes, lecteurs Music Assistant détectés, groupes.",
    "music": "Scènes lancées en un clic depuis la page Musique.",
    "announce": "Voix par défaut et messages pré-enregistrés.",
    "alerts": "Textes diffusés pour chaque type d'alerte.",
}


def _admin_page(request: Request, user: User, section: str, **ctx: Any) -> Response:
    name = "admin.html" if section == "index" else f"admin_{section}.html"
    return render(
        request,
        name,
        user,
        active="admin",
        section=section,
        sections=ADMIN_SECTIONS,
        blurbs=ADMIN_BLURBS,
        **ctx,
    )


@router.get("/admin")
async def admin_home(request: Request, user: User = Depends(admin_page)) -> Response:
    return _admin_page(request, user, "index")


@router.get("/admin/utilisateurs")
async def admin_users_page(request: Request, user: User = Depends(admin_page)) -> Response:
    return _admin_page(request, user, "users")


@router.get("/admin/etudiants")
async def admin_students_page(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    promos = (await session.scalars(select(Student.promo).distinct().order_by(Student.promo))).all()
    cursus = (
        await session.scalars(select(Student.cursus).distinct().order_by(Student.cursus))
    ).all()
    return _admin_page(
        request,
        user,
        "students",
        promos=[p for p in promos if p],
        cursus_list=[c for c in cursus if c],
        max_mb=students_api.MAX_IMPORT_BYTES // 1_000_000,
    )


@router.get("/admin/enceintes")
async def admin_speakers_page(request: Request, user: User = Depends(admin_page)) -> Response:
    return _admin_page(request, user, "speakers")


@router.get("/admin/musique")
async def admin_music_page(request: Request, user: User = Depends(admin_page)) -> Response:
    return _admin_page(request, user, "music")


@router.get("/admin/alertes")
async def admin_alerts_page(request: Request, user: User = Depends(admin_page)) -> Response:
    return _admin_page(request, user, "alerts")


@router.get("/admin/annonces")
async def admin_announce_page(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    return _admin_page(
        request,
        user,
        "announce",
        voices=settings.announce_voices,
        langs=settings.announce_langs,
        defaults=await ha_api.get_defaults(session, settings),
    )


@router.get("/admin/planning")
async def planning_legacy(user: User = Depends(admin_page)) -> Response:
    return RedirectResponse("/planning", status_code=301)


@router.get("/planning")
async def planning_page(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    schedules = await planning_api.list_schedules(session, user)
    return render(
        request,
        "planning.html",
        user,
        active="planning",
        can_edit=user.role == Role.admin,
        schedules=schedules,
        statuses=[s.value for s in ScheduleRunStatus if s is not ScheduleRunStatus.running],
    )


# --- fragments HTMX ---------------------------------------------------------


async def _players(settings: Settings, user: User, session: AsyncSession) -> dict[str, Any]:
    declared = await ma_api.declared_players(session)
    if not declared:
        return {"players": [], "error": None, "undeclared": True}
    try:
        ma = ma_api.get_ma(settings)
        return {"players": await ma_api.list_players(user, ma, declared), "error": None}
    except HTTPException as e:
        return {"players": [], "error": str(e.detail)}


@router.get("/ui/music/players")
async def players_fragment(
    request: Request,
    user: User = Depends(staff_page),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    ctx = await _players(settings, user, session)
    return render(request, "partials/players.html", user, **ctx)


@router.get("/ui/music/search")
async def search_fragment(
    request: Request,
    q: str = Query(default="", max_length=200),
    user: User = Depends(staff_page),
    settings: Settings = Depends(get_settings),
) -> Response:
    q = q.strip()
    if len(q) < 2:
        return render(request, "partials/search.html", user, results=None)
    try:
        ma = ma_api.get_ma(settings)
        res = await ma_api.search(q, None, 6, user, ma, settings)
    except HTTPException as e:
        return _fragment_error(request, e)
    groups = [(label, res[k]) for k, label in SEARCH_GROUPS if res.get(k)]
    return render(request, "partials/search.html", user, groups=groups, q=q)


@router.get("/ui/music/queue")
async def queue_fragment(
    request: Request,
    player_id: str = Query(default="", max_length=200),
    user: User = Depends(staff_page),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if not player_id:
        return render(request, "partials/queue.html", user, queue=None)
    try:
        declared = await ma_api.declared_players(session)
        pid = await ma_api.declared_player(player_id, declared)
        ma = ma_api.get_ma(settings)
        queue = await ma_api.get_queue(request, pid, user, ma, settings)
    except HTTPException as e:
        return _fragment_error(request, e)
    return render(request, "partials/queue.html", user, queue=queue, player_id=pid)


@router.get("/ui/announce/history")
async def history_fragment(
    request: Request,
    page: int = Query(default=1, ge=1, le=100000),
    filters: history_api.Filters = Depends(history_api.filters_dep),
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    size = history_api.PAGE_SIZE
    rows, total = await history_api.search(
        session, filters, limit=size, offset=(page - 1) * size, viewer=user
    )
    pages = max(1, -(-total // size))
    return render(
        request,
        "partials/history.html",
        user,
        rows=[ha_api._ann_out(a) for a in rows],
        page=page,
        pages=pages,
        total=total,
    )


@router.get("/ui/announce/{announcement_id}")
async def announcement_detail_fragment(
    request: Request,
    announcement_id: uuid.UUID,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        a = await ha_api.get_announcement(announcement_id, session, user)
    except HTTPException as e:
        return _fragment_error(request, e)
    results = a["results"] or {}
    per_speaker = [
        {
            "name": t.get("name") or t.get("entity_id"),
            "entity_id": t.get("entity_id"),
            "result": results.get(t.get("entity_id")),
        }
        for t in a["targets"]
    ]
    return render(
        request,
        "partials/announce_detail.html",
        user,
        a=a,
        styles=STYLES,
        per_speaker=per_speaker,
        global_error=results.get("_error"),
    )


@router.get("/ui/admin/users")
async def users_fragment(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = await users_api.list_users(role=None, session=session, _=user)
    return render(request, "partials/users.html", user, rows=rows)


STUDENTS_PAGE_SIZE = 50


@router.get("/ui/admin/students")
async def students_fragment(
    request: Request,
    q: str = Query("", max_length=100),
    promo: str = Query("", max_length=100),
    cursus: str = Query("", max_length=100),
    page: int = Query(1, ge=1, le=100_000),
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    cl = students_api.filter_clause(q, promo, cursus)
    total_all = await session.scalar(select(func.count()).select_from(Student)) or 0
    total = await session.scalar(select(func.count()).select_from(Student).where(*cl)) or 0
    pages = max(1, -(-total // STUDENTS_PAGE_SIZE))
    page = min(page, pages)
    rows = (
        await session.scalars(
            select(Student)
            .where(*cl)
            .order_by(Student.promo, Student.last_name, Student.first_name, Student.email)
            .limit(STUDENTS_PAGE_SIZE)
            .offset((page - 1) * STUDENTS_PAGE_SIZE)
        )
    ).all()
    by_promo = (
        await session.execute(
            select(Student.promo, func.count()).group_by(Student.promo).order_by(Student.promo)
        )
    ).all()
    by_cursus = (
        await session.execute(
            select(Student.cursus, func.count()).group_by(Student.cursus).order_by(Student.cursus)
        )
    ).all()

    def url(p: int) -> str:
        return "/ui/admin/students?" + urlencode(
            {"q": q, "promo": promo, "cursus": cursus, "page": p}
        )

    return render(
        request,
        "partials/students.html",
        user,
        rows=rows,
        total=total,
        total_all=total_all,
        page=page,
        pages=pages,
        prev_url=url(page - 1) if page > 1 else None,
        next_url=url(page + 1) if page < pages else None,
        by_promo=[(k, n) for k, n in by_promo if k],
        by_cursus=[(k, n) for k, n in by_cursus if k],
        q=q,
        promo=promo,
        cursus=cursus,
        delete_word=students_api.DELETE_ALL_WORD,
    )


@router.post("/ui/admin/students/preview")
async def students_preview_fragment(
    request: Request,
    body: students_api.CsvIn,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    plan = await students_api.build_plan(session, body.csv)
    return render(
        request,
        "partials/students_preview.html",
        user,
        plan=plan,
        csv_text=body.csv,
        errors=plan.parsed.errors[:100],
    )


@router.get("/ui/admin/speakers")
async def speakers_fragment(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = await ha_api.list_speakers(session, user)
    return render(request, "partials/speakers.html", user, rows=rows)


@router.get("/ui/admin/ma-players")
async def ma_players_fragment(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    speakers = await ha_api.list_speakers(session, user)
    players: list[dict[str, Any]] = []
    error: str | None = None
    if not settings.ma_configured:
        error = "Music Assistant n'est pas configuré."
    else:
        try:
            players = await ma_api.available_players(user, ma_api.get_ma(settings), session)
        except HTTPException as e:
            error = f"Music Assistant indisponible ({e.detail})."
    known = {p["player_id"] for p in players}
    orphans = (
        [s for s in speakers if s["ma_player_id"] and s["ma_player_id"] not in known]
        if not error
        else []
    )
    return render(
        request,
        "partials/ma_players.html",
        user,
        players=players,
        speakers=speakers,
        orphans=orphans,
        error=error,
    )


@router.get("/ui/admin/presets")
async def presets_fragment(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = await ha_api.list_presets(session, user)
    return render(request, "partials/presets.html", user, rows=rows)


@router.get("/ui/admin/alert-templates")
async def alert_templates_fragment(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = [alert_templates_api.out(t) for t in await alert_templates_api.list_templates(session)]
    return render(request, "partials/alert_templates.html", user, rows=rows)


@router.get("/ui/admin/speaker-groups")
async def speaker_groups_fragment(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = _group_rows(await groups_api.list_groups(session))
    speakers = await ha_api.list_speakers(session, user)
    return render(request, "partials/speaker_groups.html", user, rows=rows, speakers=speakers)


@router.get("/ui/admin/scenes")
async def scenes_fragment(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = await scenes_api.list_scenes(session)
    speakers = await ha_api.list_speakers(session, user)
    grp = _group_rows(await groups_api.list_groups(session))
    presets = [p for p in await ha_api.list_presets(session, user) if p["is_active"]]
    return render(
        request,
        "partials/scenes.html",
        user,
        rows=rows,
        speakers=speakers,
        groups=grp,
        presets=presets,
        media_types=MEDIA_TYPES,
    )


HISTORY_SIZE = 25


async def _health(
    request: Request, settings: Settings, session: AsyncSession
) -> list[dict[str, Any]]:
    """Résumé de santé sans appel externe bloquant : configuration + ping local borné."""

    async def ok(coro: Any) -> bool:
        try:
            await asyncio.wait_for(coro, 1.0)
            return True
        except Exception:
            return False

    redis = getattr(request.app.state, "redis", None)
    return [
        {"name": "Base de données", "ok": await ok(session.execute(select(1)))},
        {"name": "Redis", "ok": redis is not None and await ok(redis.ping())},
        {
            "name": "Home Assistant",
            "ok": bool(settings.ha_url and settings.ha_token.get_secret_value()),
            "cfg": True,
        },
        {"name": "Music Assistant", "ok": bool(settings.ma_url), "cfg": True},
        {
            "name": "Teams",
            "ok": bool(settings.teams_app_id and settings.teams_conversation_id),
            "cfg": True,
        },
    ]


@router.get("/ui/home/summary")
async def home_summary_fragment(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    active = await session.scalar(
        select(Incident)
        .where(Incident.status.in_((IncidentStatus.sent.value, IncidentStatus.failed.value)))
        .order_by(Incident.created_at.desc())
        .limit(1)
    )
    last_incident = active or await session.scalar(
        select(Incident).order_by(Incident.created_at.desc()).limit(1)
    )
    scenes = (await scenes_api.list_scenes(session, active_only=True))[:3]
    upcoming = await planning_api.upcoming(session, 3)
    rows, _ = await history_api.search(
        session, history_api.Filters(), limit=5, offset=0, viewer=user
    )
    return render(
        request,
        "partials/home_summary.html",
        user,
        active_incident=active,
        last_incident=last_incident,
        scenes=scenes,
        upcoming=upcoming,
        announcements=[ha_api._ann_out(a) for a in rows],
        health=await _health(request, settings, session),
        action_labels=ACTION_LABELS,
    )


@router.get("/ui/alerts/sheet")
async def alert_sheet_fragment(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    tpls = [
        alert_templates_api.out(t)
        for t in await alert_templates_api.list_templates(session, active_only=True)
    ]
    return render(request, "partials/alert_sheet.html", user, templates=tpls)


@router.get("/ui/alerts/active")
async def active_incident_fragment(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    q = (
        select(Incident)
        .where(Incident.status.in_((IncidentStatus.sent.value, IncidentStatus.failed.value)))
        .order_by(Incident.created_at.desc())
        .limit(1)
    )
    inc = await session.scalar(q)
    return render(request, "partials/incident_banner.html", user, incident=inc)


@router.get("/ui/alerts/list")
async def incidents_fragment(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
    roster: RosterProvider = Depends(alerts_api.get_roster),
) -> Response:
    rows = await alerts_api.list_incidents(limit=HISTORY_SIZE, offset=0, session=session, _=user)
    counts: dict[str, int] = {}
    if rows:
        q = (
            select(ResponseRow.incident_id, func.count())
            .where(ResponseRow.incident_id.in_([uuid.UUID(r["id"]) for r in rows]))
            .group_by(ResponseRow.incident_id)
        )
        counts = {str(i): n for i, n in (await session.execute(q)).all()}
    total: int | None = None
    if roster.configured:
        try:
            total = len(await roster.members())
        except Exception:  # roster indisponible : le total est facultatif
            total = None
    for r in rows:
        r["responded"] = counts.get(r["id"], 0)
    return render(request, "partials/incidents.html", user, rows=rows, roster_total=total)


@router.get("/ui/alerts/{incident_id}/responses")
async def responses_fragment(
    request: Request,
    incident_id: uuid.UUID,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
    roster: RosterProvider = Depends(alerts_api.get_roster),
) -> Response:
    try:
        await alerts_api.get_incident(incident_id, session, user)
    except HTTPException as e:
        return _fragment_error(request, e)
    data, _ = await responses_svc.report(session, roster, incident_id)
    return render(request, "partials/responses.html", user, data=data, incident_id=incident_id)


DAY_LABELS = (
    ("1", "Lun"),
    ("2", "Mar"),
    ("3", "Mer"),
    ("4", "Jeu"),
    ("5", "Ven"),
    ("6", "Sam"),
    ("7", "Dim"),
)
CLOSURE_KINDS = (("vacances", "Vacances"), ("fermeture", "Fermeture"), ("ferie", "Jour férié"))
ACTION_LABELS = {"announcement": "Annonce", "scene": "Scène", "stop": "Pause / extinction"}


@router.get("/ui/planning/schedules")
async def schedules_fragment(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    return render(
        request,
        "partials/schedules.html",
        user,
        can_edit=user.role == Role.admin,
        rows=await planning_api.list_schedules(session, user),
        speakers=[s for s in await ha_api.list_speakers(session, user) if s["is_active"]],
        groups=[g for g in _group_rows(await groups_api.list_groups(session)) if g["is_active"]],
        scenes=[s for s in await scenes_api.list_scenes(session) if s["is_active"]],
        presets=[p for p in await ha_api.list_presets(session, user) if p["is_active"]],
        voices=settings.announce_voices,
        langs=settings.announce_langs,
        days=DAY_LABELS,
        action_labels=ACTION_LABELS,
    )


@router.get("/ui/planning/preview/{schedule_id}")
async def schedule_preview_fragment(
    request: Request,
    schedule_id: uuid.UUID,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    data = await planning_api.preview(schedule_id, session, user)
    return render(request, "partials/schedule_preview.html", user, data=data)


@router.get("/ui/planning/closures")
async def closures_fragment(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    year = datetime.now(UTC).year
    cal = await planning_api.get_calendar(year, session, user)
    nxt = planning_api.holidays_out(year + 1)
    return render(
        request,
        "partials/closures.html",
        user,
        can_edit=user.role == Role.admin,
        cal=cal,
        next_holidays=nxt,
        kinds=CLOSURE_KINDS,
    )


@router.get("/ui/planning/runs")
async def runs_fragment(
    request: Request,
    schedule_id: str = "",
    status: str = "",
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        sid = uuid.UUID(schedule_id) if schedule_id else None
        st = ScheduleRunStatus(status) if status else None
    except ValueError:
        raise HTTPException(422, "invalid filter") from None
    rows = await planning_api.list_runs(sid, st, 50, session, user)
    return render(request, "partials/schedule_runs.html", user, rows=rows)
