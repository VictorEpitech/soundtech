"""PWA : manifest, service worker à la racine, page hors-ligne.

Le service worker ne met en cache que la coquille statique publique (jamais d'API,
de pages authentifiées, d'alertes, de SSE ni de cookies).
"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

BASE = Path(__file__).resolve().parent.parent
STATIC = BASE / "static"
SW_TEMPLATE = STATIC / "sw.template.js"

router = APIRouter(include_in_schema=False)


def _icon(name: str, size: int, purpose: str) -> dict[str, str]:
    return {
        "src": f"/static/icons/{name}-{size}.png",
        "sizes": f"{size}x{size}",
        "type": "image/png",
        "purpose": purpose,
    }


MANIFEST = {
    "name": "Sound",
    "short_name": "Sound",
    "description": "Annonces, musique et alertes de sécurité du campus.",
    "lang": "fr",
    "id": "/",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "theme_color": "#2a4b5a",
    "background_color": "#f8fafc",
    "icons": [
        _icon(name, size, purpose)
        for name, size, purpose in (
            ("icon", 192, "any"),
            ("icon", 512, "any"),
            ("icon-maskable", 192, "maskable"),
            ("icon-maskable", 512, "maskable"),
        )
    ],
    "shortcuts": [
        {
            "name": "Alerte",
            "short_name": "Alerte",
            "description": "Ouvrir la grille des alertes de sécurité",
            "url": "/alerts",
            "icons": [_icon("icon", 192, "any")],
        }
    ],
}  # fmt: skip


def shell_assets() -> list[str]:
    """Fichiers statiques précachés (publics) ; /static/sw.template.js exclu."""
    files = sorted(
        p for p in STATIC.rglob("*") if p.is_file() and p != SW_TEMPLATE and p.suffix != ".map"
    )
    return ["/static/" + p.relative_to(STATIC).as_posix() for p in files]


def shell_version() -> str:
    h = hashlib.sha256()
    for url in shell_assets():
        h.update(url.encode())
        h.update((STATIC / url.removeprefix("/static/")).read_bytes())
    h.update(SW_TEMPLATE.read_bytes())
    return h.hexdigest()[:12]


@lru_cache(maxsize=1)
def sw_source() -> str:
    src = SW_TEMPLATE.read_text(encoding="utf-8")
    return src.replace("__VERSION__", shell_version()).replace(
        "__ASSETS__", json.dumps(shell_assets())
    )


@router.get("/manifest.webmanifest")
async def manifest() -> Response:
    return JSONResponse(
        MANIFEST,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/sw.js")
async def service_worker() -> Response:
    return Response(
        sw_source(),
        media_type="text/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/offline")
async def offline(request: Request) -> HTMLResponse:
    from app.ui.router import render  # import tardif : router.py importe ce module

    # Jamais de contexte utilisateur : la page est mise en cache par le service worker.
    return render(request, "offline.html", None, active="offline")  # type: ignore[return-value]
