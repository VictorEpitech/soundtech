import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.admin import router as admin_router
from app.alerts.bot import router as bot_router
from app.alerts.router import router as incidents_router
from app.alerts.templates_api import router as alert_templates_router
from app.auth.router import router as auth_router
from app.bootstrap import ensure_bootstrap_admin
from app.config import get_settings
from app.db import engine
from app.groups import router as groups_router
from app.ha.router import announcements_router, presets_router, speakers_router
from app.logging import setup_logging
from app.ma.router import router as music_router
from app.planning.router import closures_router
from app.planning.router import router as schedules_router
from app.push import router as push_router
from app.scenes import router as scenes_router
from app.students import router as students_router
from app.ui.router import register as register_ui

log = logging.getLogger("sound.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level, f"{settings.service_name}-api")
    try:
        await ensure_bootstrap_admin(settings)
    except Exception:
        log.exception("bootstrap admin failed")
    app.state.redis = Redis.from_url(settings.redis_url)
    log.info("api started")
    yield
    await app.state.redis.aclose()
    await engine.dispose()


app = FastAPI(title="Sound", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(students_router)
app.include_router(speakers_router)
app.include_router(presets_router)
app.include_router(announcements_router)
app.include_router(music_router)
app.include_router(incidents_router)
app.include_router(alert_templates_router)
app.include_router(groups_router)
app.include_router(scenes_router)
app.include_router(schedules_router)
app.include_router(closures_router)
app.include_router(push_router)
app.include_router(bot_router)
register_ui(app)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        log.exception("readyz: postgres check failed")
        checks["postgres"] = "error"
    try:
        await app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception:
        log.exception("readyz: redis check failed")
        checks["redis"] = "error"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ready" if ok else "unavailable", "checks": checks},
        status_code=200 if ok else 503,
    )
