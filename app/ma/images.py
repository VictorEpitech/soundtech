"""Références d'images MA signées : le navigateur ne peut demander que ce que le serveur a émis."""

import hashlib
import hmac
import re
from typing import Any
from urllib.parse import urlencode

PROVIDER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_PATH = 1000
SIZES = (64, 128, 256, 512)
PLACEHOLDER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" '
    b'fill="#dfe5e8"/><path d="M26 18v21a6 6 0 1 0 4 5V26h12v-8z" fill="#5a717b"/></svg>'
)


def valid_ref(provider: str, path: str) -> bool:
    return (
        bool(PROVIDER_RE.fullmatch(provider)) and 0 < len(path) <= MAX_PATH and path.isprintable()
    )


def sign(secret: str, provider: str, path: str) -> str:
    msg = f"{provider}\n{path}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def verify(secret: str, provider: str, path: str, sig: str) -> bool:
    return valid_ref(provider, path) and hmac.compare_digest(sign(secret, provider, path), sig)


def _find(item: dict[str, Any]) -> dict[str, Any] | None:
    img = item.get("image")
    if isinstance(img, dict) and img.get("path"):
        return img
    meta = item.get("metadata")
    images = meta.get("images") if isinstance(meta, dict) else None
    if isinstance(images, list):
        for i in images:
            if isinstance(i, dict) and i.get("path"):
                return i
    for key in ("media_item", "album"):
        sub = item.get(key)
        if isinstance(sub, dict) and sub is not item:
            found = _find(sub)
            if found:
                return found
    return None


def image_url(item: dict[str, Any], secret: str) -> str | None:
    """URL `/music/image?...` signée pour l'image d'un média MA, ou None."""
    img = _find(item)
    if not img:
        return None
    provider, path = str(img.get("provider") or ""), str(img["path"])
    if not valid_ref(provider, path):
        return None
    return "/music/image?" + urlencode(
        {"provider": provider, "path": path, "sig": sign(secret, provider, path)}
    )
