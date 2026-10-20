import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def record(
    session: AsyncSession,
    action: str,
    *,
    request: Request | None = None,
    user_id: uuid.UUID | None = None,
    actor: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    ip = request.client.host if request and request.client else None
    session.add(AuditLog(action=action, user_id=user_id, actor=actor, ip=ip, detail=detail))
