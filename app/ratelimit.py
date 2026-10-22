import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, HTTPException, Request

from app.config import Settings, get_settings
from app.models import User

log = logging.getLogger("sound.ratelimit")


async def hit(request: Request, key: str, limit: int, window: int) -> None:
    """Fenêtre fixe. Fail-open si Redis est absent ou indisponible. limit <= 0 désactive."""
    if limit <= 0:
        return
    try:
        pipe = request.app.state.redis.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, window, nx=True)
        n = (await pipe.execute())[0]
    except Exception:
        log.warning("rate limit unavailable", exc_info=True)
        return
    if n > limit:
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(window)})


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def per_ip(name: str, limit_attr: str) -> Callable[..., Awaitable[None]]:
    async def dep(request: Request, settings: Settings = Depends(get_settings)) -> None:
        await hit(
            request,
            f"rl:{name}:ip:{client_ip(request)}",
            getattr(settings, limit_attr),
            settings.rl_window,
        )

    return dep


def per_user(
    name: str, limit_attr: str, user_dep: Callable[..., Any]
) -> Callable[..., Awaitable[User]]:
    async def dep(
        request: Request,
        user: User = Depends(user_dep),
        settings: Settings = Depends(get_settings),
    ) -> User:
        await hit(
            request, f"rl:{name}:user:{user.id}", getattr(settings, limit_attr), settings.rl_window
        )
        return user

    return dep
