import csv
import io
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import audit
from app.auth.deps import get_session, require_role
from app.models import Role, User

router = APIRouter(prefix="/admin/users", tags=["admin"])
admin_only = require_role(Role.admin)

MAX_IMPORT_BYTES = 1_000_000


class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    display_name: str = Field(default="", max_length=255)
    role: Role = Role.student
    is_active: bool = True


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=255)
    role: Role | None = None
    is_active: bool | None = None


def _out(u: User) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role,
        "is_active": u.is_active,
        "has_logged_in": u.entra_oid is not None,
    }


async def _is_last_admin(session: AsyncSession, user: User) -> bool:
    if user.role != Role.admin or not user.is_active:
        return False
    n = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == Role.admin.value, User.is_active.is_(True), User.id != user.id)
    )
    return n == 0


async def _get(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    return user


@router.get("")
async def list_users(
    role: Role | None = None,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(admin_only),
) -> list[dict[str, Any]]:
    q = select(User).order_by(User.email)
    if role:
        q = q.where(User.role == role.value)
    return [_out(u) for u in (await session.scalars(q)).all()]


@router.post("", status_code=201)
async def create_user(
    body: UserIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(admin_only),
) -> dict[str, Any]:
    user = User(
        email=body.email.strip().lower(),
        display_name=body.display_name,
        role=body.role.value,
        is_active=body.is_active,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(409, "email already exists") from None
    audit.record(
        session,
        "user_create",
        request=request,
        user_id=actor.id,
        actor=actor.email,
        detail={"target": user.email, "role": user.role},
    )
    return _out(user)


@router.get("/{user_id}")
async def get_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(admin_only),
) -> dict[str, Any]:
    return _out(await _get(session, user_id))


@router.patch("/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    body: UserPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(admin_only),
) -> dict[str, Any]:
    user = await _get(session, user_id)
    new_role = body.role.value if body.role else user.role
    new_active = user.is_active if body.is_active is None else body.is_active
    if (new_role != Role.admin.value or not new_active) and await _is_last_admin(session, user):
        raise HTTPException(409, "cannot remove the last active admin")
    changes: dict[str, Any] = {}
    if new_role != user.role:
        changes["role"] = {"from": user.role, "to": new_role}
        user.role = new_role
    if new_active != user.is_active:
        changes["is_active"] = {"from": user.is_active, "to": new_active}
        user.is_active = new_active
    if body.display_name is not None and body.display_name != user.display_name:
        changes["display_name"] = {"from": user.display_name, "to": body.display_name}
        user.display_name = body.display_name
    if changes:
        audit.record(
            session,
            "user_update",
            request=request,
            user_id=actor.id,
            actor=actor.email,
            detail={"target": user.email, "changes": changes},
        )
    return _out(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(admin_only),
) -> None:
    user = await _get(session, user_id)
    if await _is_last_admin(session, user):
        raise HTTPException(409, "cannot remove the last active admin")
    audit.record(
        session,
        "user_delete",
        request=request,
        user_id=actor.id,
        actor=actor.email,
        detail={"target": user.email, "role": user.role},
    )
    await session.delete(user)


@router.post("/import")
async def import_csv(
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(admin_only),
) -> dict[str, Any]:
    """Corps text/csv brut, en-têtes `email,nom,role` (role vide = student)."""
    raw = await request.body()
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "csv too large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "csv must be utf-8") from None
    reader = csv.DictReader(io.StringIO(text))
    fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])}
    if "email" not in fields:
        raise HTTPException(400, "missing 'email' column")
    existing = {e for e in await session.scalars(select(User.email))}
    created, skipped, errors = 0, 0, []
    for line, row in enumerate(reader, start=2):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        email = r.get("email", "").lower()
        try:
            data = UserIn(
                email=email,
                display_name=r.get("nom") or r.get("name", ""),
                role=r.get("role") or Role.student,
            )
        except ValueError:
            errors.append({"line": line, "error": "invalid email or role"})
            continue
        if data.email in existing:
            skipped += 1
            continue
        existing.add(data.email)
        session.add(User(email=data.email, display_name=data.display_name, role=data.role.value))
        created += 1
    audit.record(
        session,
        "user_import",
        request=request,
        user_id=actor.id,
        actor=actor.email,
        detail={"created": created, "skipped": skipped, "errors": len(errors)},
    )
    return {"created": created, "skipped": skipped, "errors": errors}
