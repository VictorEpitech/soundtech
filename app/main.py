import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.admin import router as admin_router
from app.auth.router import router as auth_router
from app.bootstrap import ensure_bootstrap_admin
from app.config import get_settings
from app.db import engine
from app.ha.router import announcements_router, presets_router, speakers_router
from app.logging import setup_logging
from app.ma.router import router as music_router

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
app.include_router(speakers_router)
app.include_router(presets_router)
app.include_router(announcements_router)
app.include_router(music_router)


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
