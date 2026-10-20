import asyncio
import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from redis.asyncio import Redis

from app.config import get_settings
from app.db import SessionLocal
from app.ha import service
from app.ha.client import HAClient
from app.logging import setup_logging
from app.ma import monitor
from app.ma.client import MAClient

log = logging.getLogger("sound.worker")


async def ping(ctx: dict[str, Any]) -> str:
    log.info("ping received")
    return "pong"


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level, f"{settings.service_name}-worker")
    ctx["redis"] = Redis.from_url(settings.redis_url)
    ctx["ha"] = HAClient.from_settings(settings) if settings.ha_configured else None
    ctx["ma"] = MAClient.from_settings(settings) if settings.ma_configured else None
    ctx["ma_monitor"] = (
        asyncio.create_task(monitor.run(ctx["ma"])) if ctx["ma"] is not None else None
    )
    if ctx["ha"] is None:
        log.warning("HA_URL/HA_TOKEN absents : file d'annonces non consommée")
    log.info("worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    if ctx.get("ma_monitor") is not None:
        ctx["ma_monitor"].cancel()
        await asyncio.gather(ctx["ma_monitor"], return_exceptions=True)
    await ctx["redis"].aclose()
    if ctx["ha"] is not None:
        await ctx["ha"].aclose()


async def drain_announcements(ctx: dict[str, Any]) -> int:
    if ctx["ha"] is None:
        return 0
    return await service.drain(SessionLocal, ctx["redis"], ctx["ha"], ctx["ma"])


class WorkerSettings:
    functions = [ping]
    cron_jobs = [cron(drain_announcements, second=set(range(60)), run_at_startup=True)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    health_check_interval = 30
    health_check_key = "arq:health-check"
