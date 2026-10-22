"""Historique des annonces : filtres validés côté serveur, requêtes SQLAlchemy paramétrées."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from fastapi import HTTPException, Query
from sqlalchemy import Select, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Announcement, AnnouncementStatus, SpeakerGroup

PAGE_SIZE = 25
_TRUE = {"true", "1", "oui", "yes", "on"}
_FALSE = {"false", "0", "non", "no", "off"}


@dataclass(frozen=True)
class Filters:
    date_from: date | None = None
    date_to: date | None = None
    author: str | None = None
    is_test: bool | None = None
    status: str | None = None
    speaker_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    q: str | None = None


def _blank(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


def _parse(label: str, fn, v: str):  # type: ignore[no-untyped-def]
    try:
        return fn(v)
    except ValueError:
        raise HTTPException(422, f"invalid {label}") from None


def _bool(v: str) -> bool:
    low = v.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ValueError(v)


def filters_dep(
    date_from: str | None = Query(None, max_length=10),
    date_to: str | None = Query(None, max_length=10),
    author: str | None = Query(None, max_length=320),
    is_test: str | None = Query(None, max_length=5),
    status: str | None = Query(None, max_length=16),
    speaker_id: str | None = Query(None, max_length=36),
    group_id: str | None = Query(None, max_length=36),
    q: str | None = Query(None, max_length=200),
) -> Filters:
    """Valeur vide (formulaire HTML) = pas de filtre ; le reste est strictement typé."""
    f = {
        k: _blank(v)
        for k, v in dict(
            date_from=date_from,
            date_to=date_to,
            author=author,
            is_test=is_test,
            status=status,
            speaker_id=speaker_id,
            group_id=group_id,
            q=q,
        ).items()  # fmt: skip
    }
    st = f["status"]
    if st is not None and st not in {s.value for s in AnnouncementStatus}:
        raise HTTPException(422, "invalid status")
    out = Filters(
        date_from=_parse("date_from", date.fromisoformat, f["date_from"]) if f["date_from"] else None,  # noqa: E501
        date_to=_parse("date_to", date.fromisoformat, f["date_to"]) if f["date_to"] else None,
        author=f["author"],
        is_test=_parse("is_test", _bool, f["is_test"]) if f["is_test"] else None,
        status=st,
        speaker_id=_parse("speaker_id", uuid.UUID, f["speaker_id"]) if f["speaker_id"] else None,
        group_id=_parse("group_id", uuid.UUID, f["group_id"]) if f["group_id"] else None,
        q=f["q"],
    )  # fmt: skip
    if out.date_from and out.date_to and out.date_from > out.date_to:
        raise HTTPException(422, "date_from after date_to")
    return out


def _like(term: str) -> str:
    esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


async def _apply(session: AsyncSession, stmt: Select, f: Filters) -> Select:  # type: ignore[type-arg]
    A = Announcement
    if f.date_from:
        stmt = stmt.where(A.created_at >= datetime.combine(f.date_from, time.min, UTC))
    if f.date_to:
        stmt = stmt.where(
            A.created_at < datetime.combine(f.date_to + timedelta(days=1), time.min, UTC)
        )
    if f.author:
        stmt = stmt.where(A.author_email.ilike(_like(f.author), escape="\\"))
    if f.is_test is not None:
        stmt = stmt.where(A.is_test.is_(f.is_test))
    if f.status:
        stmt = stmt.where(A.status == f.status)
    if f.q:
        stmt = stmt.where(A.text.ilike(_like(f.q), escape="\\"))
    ids: list[uuid.UUID] = []
    if f.speaker_id:
        ids.append(f.speaker_id)
    if f.group_id:
        g = await session.get(SpeakerGroup, f.group_id)
        if g is None:
            raise HTTPException(404, "speaker group not found")
        ids.extend(s.id for s in g.members)
    if f.speaker_id or f.group_id:
        # Ciblée si l'une des enceintes (ou un membre du groupe) figure dans `targets`.
        tj = cast(A.targets, JSONB)
        conds = [tj.contains([{"id": str(i)}]) for i in dict.fromkeys(ids)]
        stmt = stmt.where(or_(*conds)) if conds else stmt.where(False)
    return stmt


async def search(
    session: AsyncSession, f: Filters, *, limit: int = PAGE_SIZE, offset: int = 0
) -> tuple[list[Announcement], int]:
    total = await session.scalar(await _apply(session, select(func.count(Announcement.id)), f))
    stmt = await _apply(session, select(Announcement), f)
    stmt = (
        stmt.order_by(Announcement.created_at.desc(), Announcement.id).limit(limit).offset(offset)
    )
    return list((await session.scalars(stmt)).all()), int(total or 0)
