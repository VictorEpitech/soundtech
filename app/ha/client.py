import asyncio
import logging
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger("sound.ha")


class HAError(Exception):
    """Erreur de base du client Home Assistant."""


class HAConfigError(HAError):
    """HA_URL / HA_TOKEN absents."""


class HAConnectionError(HAError):
    """Réseau, DNS ou timeout."""


class HAAuthError(HAError):
    """401/403 : token refusé."""


class HARequestError(HAError):
    """Autre 4xx : service inconnu, payload invalide."""


class HAServerError(HAError):
    """5xx côté Home Assistant."""


_RETRYABLE = (HAConnectionError, HAServerError)


class HAClient:
    def __init__(
        self,
        url: str,
        token: str,
        service: str,
        *,
        timeout: float = 10.0,
        retries: int = 2,
        backoff: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not url or not token:
            raise HAConfigError("HA_URL et HA_TOKEN sont requis pour les annonces")
        self.domain, self.service = service.split(".", 1)
        self.retries = max(0, retries)
        self.backoff = backoff
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> "HAClient":
        return cls(
            settings.ha_url,
            settings.ha_token.get_secret_value(),
            settings.ha_tts_service,
            timeout=settings.ha_timeout,
            retries=settings.ha_retries,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def payload(
        self, entity_id: str, text: str, notify_service: str | None = None
    ) -> dict[str, Any]:
        if notify_service:  # service notify propre à l'appareil : pas de target
            return {"message": text, "data": {"type": "tts"}}
        if self.domain == "notify":  # notify.alexa_media : cible = liste d'entity_id
            return {"message": text, "target": [entity_id], "data": {"type": "tts"}}
        return {"entity_id": entity_id, "message": text}

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        path = f"/api/services/{domain}/{service}"
        for attempt in range(self.retries + 1):
            try:
                await self._post(path, data)
                return
            except _RETRYABLE as exc:
                if attempt >= self.retries:
                    raise
                log.warning("HA %s failed (%s), retry %d", path, exc, attempt + 1)
                await asyncio.sleep(self.backoff * 2**attempt)

    async def _check(self, r: httpx.Response) -> None:
        if r.status_code in (401, 403):
            raise HAAuthError(f"HA a refusé le token ({r.status_code})")
        if r.status_code >= 500:
            raise HAServerError(f"HA {r.status_code}")
        if r.status_code >= 400:
            raise HARequestError(f"HA {r.status_code}: {r.text[:200]}")

    async def get_volume(self, entity_id: str) -> float | None:
        """volume_level (0..1) de l'entité media_player ; None si l'attribut est absent."""
        path = f"/api/states/{entity_id}"
        for attempt in range(self.retries + 1):
            try:
                try:
                    r = await self._http.get(path)
                except httpx.HTTPError as exc:
                    raise HAConnectionError(f"{type(exc).__name__}: {exc}") from exc
                await self._check(r)
                break
            except _RETRYABLE as exc:
                if attempt >= self.retries:
                    raise
                log.warning("HA GET %s failed (%s), retry %d", path, exc, attempt + 1)
                await asyncio.sleep(self.backoff * 2**attempt)
        try:
            level = r.json().get("attributes", {}).get("volume_level")
        except (ValueError, AttributeError) as exc:
            raise HARequestError(f"réponse d'état illisible: {exc}") from exc
        if isinstance(level, bool) or not isinstance(level, int | float):
            return None
        return min(1.0, max(0.0, float(level)))

    async def set_volume(self, entity_id: str, level: float) -> None:
        """media_player.volume_set ; level entre 0 et 1."""
        await self.call_service(
            "media_player",
            "volume_set",
            {"entity_id": entity_id, "volume_level": round(min(1.0, max(0.0, level)), 2)},
        )

    async def _post(self, path: str, data: dict[str, Any]) -> None:
        try:
            r = await self._http.post(path, json=data)
        except httpx.HTTPError as exc:
            raise HAConnectionError(f"{type(exc).__name__}: {exc}") from exc
        await self._check(r)

    async def speak(
        self,
        entity_id: str,
        text: str,
        notify_service: str | None = None,
        ssml: str | None = None,
    ) -> None:
        domain, service = notify_service.split(".", 1) if notify_service else (None, None)
        domain, service = domain or self.domain, service or self.service
        if ssml and domain == "notify":
            try:
                await self.call_service(
                    domain, service, self.payload(entity_id, ssml, notify_service)
                )
                return
            except HARequestError as exc:  # SSML refusé : une seule tentative en texte brut
                log.warning("HA refused SSML for %s (%s), plain text fallback", entity_id, exc)
        await self.call_service(domain, service, self.payload(entity_id, text, notify_service))

    async def speak_many(
        self,
        entity_ids: list[str],
        text: str,
        notify_services: dict[str, str | None] | None = None,
        ssml: str | None = None,
    ) -> dict[str, HAError | None]:
        """Un appel par enceinte, en parallèle ; une erreur n'affecte pas les autres."""
        ns = notify_services or {}
        results = await asyncio.gather(
            *(self.speak(e, text, ns.get(e), ssml) for e in entity_ids), return_exceptions=True
        )
        out: dict[str, HAError | None] = {}
        for entity_id, res in zip(entity_ids, results, strict=True):
            if isinstance(res, HAError):
                out[entity_id] = res
            elif isinstance(res, BaseException):
                raise res
            else:
                out[entity_id] = None
        return out
