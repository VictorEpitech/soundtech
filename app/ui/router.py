import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import admin as users_api
from app import groups as groups_api
from app import scenes as scenes_api
from app.alerts import responses as responses_svc
from app.alerts import router as alerts_api
from app.alerts import templates_api as alert_templates_api
from app.alerts.roster import RosterProvider
from app.auth.deps import get_session
from app.config import Settings, get_settings
from app.ha import history as history_api
from app.ha import router as ha_api
from app.ma import router as ma_api
from app.models import Announcement, Role, User
from app.models import Response as ResponseRow
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
    return templates.TemplateResponse(
        request, name, {"user": user, "role_labels": ROLE_LABELS, "roles": list(Role), **ctx}
    )


def register(app: FastAPI) -> None:
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
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
    return render(
        request, "dashboard.html", user, active="alerts", incident=incident, speakers=speakers
    )


@router.get("/admin")
async def admin_home(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    return render(
        request,
        "admin.html",
        user,
        active="admin",
        voices=settings.announce_voices,
        langs=settings.announce_langs,
        defaults=await ha_api.get_defaults(session, settings),
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
        queue = await ma_api.get_queue(pid, user, ma, settings)
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
    rows, total = await history_api.search(session, filters, limit=size, offset=(page - 1) * size)
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


@router.get("/ui/admin/speakers")
async def speakers_fragment(
    request: Request,
    user: User = Depends(admin_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = await ha_api.list_speakers(session, user)
    return render(request, "partials/speakers.html", user, rows=rows)


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
