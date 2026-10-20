from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, get_session
from app.config import Settings, get_settings
from app.models import Role, User


class PageRedirect(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


class PageError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message


async def optional_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User | None:
    try:
        return await current_user(request, session, settings)
    except HTTPException:
        return None


def page_user(*roles: Role) -> Callable[..., Coroutine[Any, Any, User]]:
    """Comme `require_role`, mais redirige vers /login (302) ou rend une page 403."""
    allowed = {str(r) for r in roles}

    async def dep(user: User | None = Depends(optional_user)) -> User:
        if user is None:
            raise PageRedirect("/login")
        if user.role not in allowed:
            raise PageError(403, "Accès réservé")
        return user

    return dep


staff_page = page_user(Role.admin, Role.staff)
admin_page = page_user(Role.admin)
