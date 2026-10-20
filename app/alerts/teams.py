"""Diffusion Teams : protocole `TeamsNotifier` + implémentations log (défaut) et Bot/Graph."""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import Settings

log = logging.getLogger("sound.alerts.teams")

RESPONSE_CHOICES: list[tuple[str, str]] = [
    ("safe_campus", "En sécurité, sur le campus"),
    ("safe_offcampus", "En sécurité, hors campus"),
    ("danger", "En danger / Besoin d'aide"),
]
RESPONSE_VERB = "incident_response"


class TeamsError(Exception):
    pass


@dataclass(frozen=True)
class IncidentNotice:
    incident_id: str
    title: str
    message: str
    is_test: bool


def build_card(notice: IncidentNotice, test_prefix: str = "TEST") -> dict[str, Any]:
    """Adaptive Card : 3 réponses exclusives + texte libre optionnel."""
    title = f"[{test_prefix}] {notice.title}" if notice.is_test else notice.title
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "msteams": {"width": "Full"},
        "body": [
            {
                "type": "TextBlock",
                "text": title,
                "weight": "Bolder",
                "size": "Large",
                "color": "Attention",
                "wrap": True,
            },
            {"type": "TextBlock", "text": notice.message, "wrap": True},
            {
                "type": "Input.ChoiceSet",
                "id": "status",
                "style": "expanded",
                "isRequired": True,
                "errorMessage": "Choisissez une réponse",
                "choices": [{"title": t, "value": v} for v, t in RESPONSE_CHOICES],
            },
            {
                "type": "Input.Text",
                "id": "details",
                "isMultiline": True,
                "maxLength": 500,
                "placeholder": "Précisions (optionnel)",
            },
        ],
        "actions": [
            {
                "type": "Action.Execute",
                "title": "Envoyer ma réponse",
                "verb": RESPONSE_VERB,
                "data": {"incident_id": notice.incident_id},
            }
        ],
    }


class TeamsNotifier(Protocol):
    async def notify(self, notice: IncidentNotice) -> dict[str, Any]:
        """Diffuse la carte. Renvoie un dict de détails ; lève `TeamsError` en cas d'échec."""
        ...


class LogTeamsNotifier:
    """Défaut sans configuration : journalise seulement."""

    async def notify(self, notice: IncidentNotice) -> dict[str, Any]:
        log.warning("teams not configured: incident %s not broadcast", notice.incident_id)
        return {"skipped": "teams not configured"}


class GraphTeamsNotifier:
    """Poste la carte dans une conversation Teams via le Bot Framework Connector.

    Réception des réponses (webhook Action.Execute) : phase 6.
    """

    TOKEN_SCOPE = "https://api.botframework.com/.default"

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._s = settings
        self._transport = transport

    async def _token(self, http: httpx.AsyncClient) -> str:
        tenant = self._s.teams_tenant_id or self._s.entra_tenant_id
        r = await http.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._s.teams_app_id,
                "client_secret": self._s.teams_app_secret.get_secret_value(),
                "scope": self.TOKEN_SCOPE,
            },
        )
        r.raise_for_status()
        return str(r.json()["access_token"])

    async def notify(self, notice: IncidentNotice) -> dict[str, Any]:
        s = self._s
        card = build_card(notice, s.test_prefix.rstrip(". ").upper() or "TEST")
        activity = {
            "type": "message",
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
            ],
        }
        base = s.teams_service_url.rstrip("/")
        url = f"{base}/v3/conversations/{s.teams_conversation_id}/activities"
        try:
            async with httpx.AsyncClient(timeout=s.teams_timeout, transport=self._transport) as h:
                token = await self._token(h)
                r = await h.post(url, json=activity, headers={"Authorization": f"Bearer {token}"})
                r.raise_for_status()
                return {"activity_id": r.json().get("id")}
        except httpx.HTTPStatusError as exc:
            raise TeamsError(f"teams http {exc.response.status_code}") from None
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise TeamsError(f"teams error: {type(exc).__name__}") from None


def get_notifier(settings: Settings) -> TeamsNotifier:
    return GraphTeamsNotifier(settings) if settings.teams_configured else LogTeamsNotifier()
