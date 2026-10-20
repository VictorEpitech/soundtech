import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import tokens
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import Role, User

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
        await session.commit()


def _bearer_or_cookie(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(ACCESS_COOKIE)


async def current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    token = _bearer_or_cookie(request)
    if not token:
        raise HTTPException(401, "not authenticated")
    try:
        claims = tokens.decode(settings, token, "access")
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "invalid token") from None
    user = await session.get(User, user_id)
    if user is None or not user.is_active or user.role == Role.student:
        raise HTTPException(401, "unknown or disabled user")
    return user


def require_role(*roles: Role) -> Callable[..., Coroutine[Any, Any, User]]:
    allowed = {str(r) for r in roles}

    async def dep(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(403, "insufficient role")
        return user

    return dep
