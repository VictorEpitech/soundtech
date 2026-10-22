"""SSML Alexa : texte utilisateur toujours échappé, voix/langue en liste blanche."""

import re
from xml.sax.saxutils import escape, quoteattr

from fastapi import HTTPException

from app.config import Settings

_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def validate_voice_lang(
    settings: Settings, voice: str | None, lang: str | None
) -> tuple[str | None, str | None]:
    if voice is not None and voice not in settings.announce_voices:
        raise HTTPException(422, f"voice not allowed: {voice[:32]!r}")
    if lang is not None and lang not in settings.announce_langs:
        raise HTTPException(422, f"lang not allowed: {lang[:16]!r}")
    return voice, lang


def _text(s: str) -> str:
    return escape(_XML_INVALID.sub("", s))


def build_ssml(
    text: str, voice: str | None, lang: str | None, *, prefix: str | None = None
) -> str | None:
    """None si ni voix ni langue. Le préfixe (TEST) reste hors de `<voice>`/`<lang>`."""
    if not voice and not lang:
        return None
    inner = _text(text)
    if lang:
        inner = f"<lang xml:lang={quoteattr(lang)}>{inner}</lang>"
    if voice:
        inner = f"<voice name={quoteattr(voice)}>{inner}</voice>"
    head = f"{_text(prefix)} " if prefix else ""
    return f"<speak>{head}{inner}</speak>"
