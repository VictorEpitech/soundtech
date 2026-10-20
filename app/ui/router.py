from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app import admin as users_api
from app.alerts import router as alerts_api
from app.auth.deps import get_session
from app.config import Settings, get_settings
from app.ha import router as ha_api
from app.ma import router as ma_api
from app.models import Role, User
from app.ui.deps import PageError, PageRedirect, admin_page, optional_user, staff_page

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=BASE / "templates")

router = APIRouter(include_in_schema=False)

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
) -> Response:
    players, error = await _players(settings, user)
    return render(request, "music.html", user, active="music", players=players, error=error)


@router.get("/announce")
async def announce_page(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    speakers = [s for s in await ha_api.list_speakers(session, user) if s["is_active"]]
    presets = [p for p in await ha_api.list_presets(session, user) if p["is_active"]]
    return render(
        request, "announce.html", user, active="announce", speakers=speakers, presets=presets
    )


@router.get("/alerts")
async def alerts_page(request: Request, user: User = Depends(staff_page)) -> Response:
    return render(request, "alerts.html", user, active="alerts")


@router.get("/admin")
async def admin_home(request: Request, user: User = Depends(admin_page)) -> Response:
    return render(request, "admin.html", user, active="admin")


# --- fragments HTMX ---------------------------------------------------------


async def _players(settings: Settings, user: User) -> tuple[list[dict[str, Any]], str | None]:
    try:
        ma = ma_api.get_ma(settings)
        return await ma_api.list_players(user, ma), None
    except HTTPException as e:
        return [], str(e.detail)


@router.get("/ui/music/players")
async def players_fragment(
    request: Request,
    user: User = Depends(staff_page),
    settings: Settings = Depends(get_settings),
) -> Response:
    players, error = await _players(settings, user)
    return render(request, "partials/players.html", user, players=players, error=error)


@router.get("/ui/music/search")
async def search_fragment(
    request: Request,
    q: str = Query(default="", max_length=200),
    user: User = Depends(staff_page),
    settings: Settings = Depends(get_settings),
) -> Response:
    q = q.strip()
    if not q:
        return render(request, "partials/search.html", user, results=None)
    try:
        ma = ma_api.get_ma(settings)
        results = await ma_api.search(q, None, 10, user, ma)
    except HTTPException as e:
        return _fragment_error(request, e)
    return render(request, "partials/search.html", user, results=results, q=q)


@router.get("/ui/announce/history")
async def history_fragment(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = await ha_api.list_announcements(limit=15, offset=0, session=session, _=user)
    return render(request, "partials/history.html", user, rows=rows)


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


@router.get("/ui/alerts/list")
async def incidents_fragment(
    request: Request,
    user: User = Depends(staff_page),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    rows = await alerts_api.list_incidents(limit=20, offset=0, session=session, _=user)
    return render(
        request,
        "partials/incidents.html",
        user,
        rows=rows,
        alert_word=settings.alert_confirm_text,
        test_word=settings.alert_test_confirm_text,
    )
