"""Diffusion Teams : protocole `TeamsNotifier` + implémentations log (défaut) et Bot/Graph."""

import html
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.alerts.roster import Member
from app.config import Settings

log = logging.getLogger("sound.alerts.teams")

RESPONSE_CHOICES: list[tuple[str, str]] = [
    ("safe_campus", "En sécurité, sur le campus"),
    ("safe_offsite", "En sécurité, hors campus"),
    ("danger", "En danger / Besoin d'aide"),
]
RESPONSE_VERB = "incident_response"


class TeamsError(Exception):
    """`mentioned_ids` : destinataires déjà mentionnés avant un échec partiel de relance."""

    def __init__(self, *args: object, mentioned_ids: list[str] | None = None) -> None:
        super().__init__(*args)
        self.mentioned_ids: list[str] = mentioned_ids or []


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

    async def remind(self, notice: IncidentNotice, targets: list[Member]) -> dict[str, Any]:
        """Relance les `targets` (carte « RAPPEL » + mentions). Lève `TeamsError` en cas d'échec."""
        ...

    async def notify_user(self, aad_id: str, text: str) -> dict[str, Any]:
        """Message 1:1 à un utilisateur (texte brut, sans donnée personnelle). Lève `TeamsError`."""
        ...


class LogTeamsNotifier:
    """Défaut sans configuration : journalise seulement."""

    async def notify(self, notice: IncidentNotice) -> dict[str, Any]:
        log.warning("teams not configured: incident %s not broadcast", notice.incident_id)
        return {"skipped": "teams not configured"}

    async def remind(self, notice: IncidentNotice, targets: list[Member]) -> dict[str, Any]:
        log.warning(
            "teams not configured: reminder for incident %s not sent (%d targets)",
            notice.incident_id,
            len(targets),
        )
        return {"skipped": "teams not configured", "targets": len(targets)}

    async def notify_user(self, aad_id: str, text: str) -> dict[str, Any]:
        log.warning("teams not configured: 1:1 notification not sent")
        return {"skipped": "teams not configured"}


class GraphTeamsNotifier:
    """Poste la carte dans une conversation Teams via le Bot Framework Connector.

    Réception des réponses : `app/alerts/bot.py` (webhook Action.Execute).
    """

    TOKEN_SCOPE = "https://api.botframework.com/.default"
    MAX_MENTIONS = 20

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

    async def _post(self, activity: dict[str, Any]) -> dict[str, Any]:
        s = self._s
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

    def _card_activity(
        self, notice: IncidentNotice, prefix: str = "", text: str = "", entities: list | None = None
    ) -> dict[str, Any]:
        tag = self._s.test_prefix.rstrip(". ").upper() or "TEST"
        card = build_card(notice, tag)
        if prefix:
            card["body"][0]["text"] = f"{prefix} {card['body'][0]['text']}"
        activity: dict[str, Any] = {
            "type": "message",
            "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
            ],
        }
        if text:
            activity["text"] = text
            activity["entities"] = entities or []
        return activity

    async def notify(self, notice: IncidentNotice) -> dict[str, Any]:
        return await self._post(self._card_activity(notice))

    async def remind(self, notice: IncidentNotice, targets: list[Member]) -> dict[str, Any]:
        """Rappel par lots de `MAX_MENTIONS` mentions : tous les `targets` sont mentionnés."""
        out: dict[str, Any] = {}
        mentioned = 0
        for i in range(0, len(targets), self.MAX_MENTIONS):
            batch = targets[i : i + self.MAX_MENTIONS]
            entities = [
                {
                    "type": "mention",
                    "text": f"<at>{html.escape(m.name or m.aad_id)}</at>",
                    "mentioned": {"id": m.aad_id, "name": m.name or m.aad_id},
                }
                for m in batch
            ]
            text = " ".join(e["text"] for e in entities)
            try:
                out = await self._post(self._card_activity(notice, "RAPPEL :", text, entities))
            except TeamsError as exc:
                raise TeamsError(str(exc), mentioned_ids=[m.aad_id for m in targets[:i]]) from None
            mentioned += len(batch)
        return {**out, "targets": len(targets), "mentioned": mentioned}

    async def notify_user(self, aad_id: str, text: str) -> dict[str, Any]:
        """Message 1:1 : ouvre (ou retrouve) la conversation personnelle puis y poste `text`."""
        s = self._s
        tenant = s.teams_tenant_id or s.entra_tenant_id
        base = s.teams_service_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=s.teams_timeout, transport=self._transport) as h:
                headers = {"Authorization": f"Bearer {await self._token(h)}"}
                r = await h.post(
                    f"{base}/v3/conversations",
                    json={
                        "isGroup": False,
                        "bot": {"id": s.teams_app_id},
                        "members": [{"id": aad_id}],
                        "tenantId": tenant,
                        "channelData": {"tenant": {"id": tenant}},
                    },
                    headers=headers,
                )
                r.raise_for_status()
                conv = str(r.json()["id"])
                r = await h.post(
                    f"{base}/v3/conversations/{conv}/activities",
                    json={"type": "message", "text": text},
                    headers=headers,
                )
                r.raise_for_status()
                return {"activity_id": r.json().get("id")}
        except httpx.HTTPStatusError as exc:
            raise TeamsError(f"teams http {exc.response.status_code}") from None
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise TeamsError(f"teams error: {type(exc).__name__}") from None


def get_notifier(settings: Settings) -> TeamsNotifier:
    return GraphTeamsNotifier(settings) if settings.teams_configured else LogTeamsNotifier()
