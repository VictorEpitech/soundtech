"""Roster : personnes attendues (membres d'un groupe Entra), base des non-répondants."""

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import SessionLocal
from app.models import Student

log = logging.getLogger("sound.alerts.roster")

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"


class RosterError(Exception):
    pass


@dataclass(frozen=True)
class Member:
    aad_id: str
    email: str | None
    name: str


class RosterProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    async def members(self) -> list[Member]:
        """Lève `RosterError` en cas d'échec."""
        ...


class LogRoster:
    """Défaut sans configuration : roster vide (aucun non-répondant calculable)."""

    configured = False

    async def members(self) -> list[Member]:
        log.warning("ROSTER_GROUP_ID not set: empty roster")
        return []


_cache: dict[str, tuple[float, list[Member]]] = {}


class GraphRoster:
    """Membres (transitifs) d'un groupe Entra via Graph, credentials client Entra existants."""

    configured = True

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._s = settings
        self._transport = transport

    async def _token(self, http: httpx.AsyncClient) -> str:
        s = self._s
        r = await http.post(
            f"{LOGIN_BASE}/{s.entra_tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": s.entra_client_id,
                "client_secret": s.entra_client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        r.raise_for_status()
        return str(r.json()["access_token"])

    async def members(self) -> list[Member]:
        s = self._s
        hit = _cache.get(s.roster_group_id)
        if hit and time.monotonic() - hit[0] < s.roster_cache_ttl:
            return hit[1]
        url: str | None = (
            f"{GRAPH}/groups/{s.roster_group_id}/transitiveMembers"
            "?$select=id,displayName,mail,userPrincipalName&$top=999"
        )
        out: dict[str, Member] = {}
        try:
            async with httpx.AsyncClient(timeout=s.roster_timeout, transport=self._transport) as h:
                headers = {"Authorization": f"Bearer {await self._token(h)}"}
                while url:
                    r = await h.get(url, headers=headers)
                    r.raise_for_status()
                    body: dict[str, Any] = r.json()
                    for m in body.get("value", []):
                        if m.get("@odata.type") != "#microsoft.graph.user" or not m.get("id"):
                            continue
                        aad = str(m["id"]).lower()
                        out[aad] = Member(
                            aad,
                            m.get("mail") or m.get("userPrincipalName"),
                            m.get("displayName") or "",
                        )
                    nxt = body.get("@odata.nextLink")
                    url = nxt if nxt and nxt.startswith("https://graph.microsoft.com/") else None
        except httpx.HTTPStatusError as exc:
            raise RosterError(f"graph http {exc.response.status_code}") from None
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise RosterError(f"graph error: {type(exc).__name__}") from None
        members = sorted(out.values(), key=lambda m: m.name.lower())
        _cache[s.roster_group_id] = (time.monotonic(), members)
        return members


class StudentRoster:
    """Mode liens : les personnes attendues sont les étudiants de l'annuaire (`students`).

    `aad_id` = `str(Student.id)`, l'identifiant porté par `Response.user_aad_id` pour les
    réponses par lien. Ouvre sa propre session (le roster est injecté hors requête, ex. worker).
    """

    configured = True

    def __init__(self, sm: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._sm = sm

    async def members(self) -> list[Member]:
        sm = self._sm or SessionLocal
        try:
            async with sm() as s:
                rows = (await s.scalars(select(Student))).all()
        except Exception as exc:
            raise RosterError(f"students error: {type(exc).__name__}") from None
        members = [
            Member(
                str(st.id),
                st.email,
                f"{st.first_name} {st.last_name}".strip() or st.email,
            )
            for st in rows
        ]
        return sorted(members, key=lambda m: m.name.lower())


def get_roster_provider(settings: Settings) -> RosterProvider:
    if settings.links_active:
        return StudentRoster()
    return GraphRoster(settings) if settings.roster_group_id else LogRoster()
