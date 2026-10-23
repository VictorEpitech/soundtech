"""Liens de réponse individuels : jetons, création, résolution (sans bot Teams ni Graph).

Le jeton (`secrets.token_urlsafe(32)`) n'est jamais stocké ni journalisé : seul son SHA-256 l'est.
Identifiant stable d'un étudiant dans `Response.user_aad_id` : `str(Student.id)` (UUID minuscule).
"""

import enum
import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Incident, IncidentStatus, ResponseLink, Student

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # token_urlsafe(32)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def link_url(settings: Settings, token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/r/{token}"


def student_aad_id(student: Student) -> str:
    return str(student.id)


def student_name(student: Student) -> str:
    return f"{student.first_name} {student.last_name}".strip()


@dataclass(frozen=True)
class Issued:
    student: Student
    url: str
    link_id: uuid.UUID


async def create_links(
    session: AsyncSession,
    settings: Settings,
    incident_id: uuid.UUID,
    students: list[Student],
    now: datetime | None = None,
) -> list[Issued]:
    """Un nouveau lien par étudiant (les anciens restent valables jusqu'à leur expiration).

    Flush seulement : l'appelant commit. Renvoie l'URL complète par étudiant.
    """
    now = now or datetime.now(UTC)
    exp = now + timedelta(hours=settings.response_link_ttl_hours)
    out: list[Issued] = []
    for st in students:
        token = new_token()
        lid = uuid.uuid4()
        session.add(
            ResponseLink(
                id=lid,
                incident_id=incident_id,
                student_id=st.id,
                token_hash=hash_token(token),
                created_at=now,
                expires_at=exp,
            )
        )
        out.append(Issued(st, link_url(settings, token), lid))
    await session.flush()
    return out


async def count_links(session: AsyncSession, incident_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ResponseLink)
            .where(ResponseLink.incident_id == incident_id)
        )
        or 0
    )


class LinkState(enum.StrEnum):
    ok = "ok"
    unknown = "unknown"  # jeton inconnu, mal formé, incident absent ou pas en cours
    expired = "expired"
    closed = "closed"


@dataclass
class Resolved:
    state: LinkState
    link: ResponseLink | None = None
    student: Student | None = None
    incident: Incident | None = None


async def resolve(session: AsyncSession, token: str, now: datetime | None = None) -> Resolved:
    """Retrouve le lien par le hash du jeton. Les motifs ne sont jamais détaillés au visiteur."""
    if not TOKEN_RE.fullmatch(token):
        return Resolved(LinkState.unknown)
    link = await session.scalar(
        select(ResponseLink).where(ResponseLink.token_hash == hash_token(token))
    )
    if link is None:
        return Resolved(LinkState.unknown)
    incident = await session.get(Incident, link.incident_id)
    student = await session.get(Student, link.student_id)
    if incident is None or student is None:
        return Resolved(LinkState.unknown)
    if incident.status == IncidentStatus.closed.value:
        return Resolved(LinkState.closed)
    if incident.status != IncidentStatus.sent.value:
        return Resolved(LinkState.unknown)
    if link.expires_at <= (now or datetime.now(UTC)):
        return Resolved(LinkState.expired)
    return Resolved(LinkState.ok, link, student, incident)
