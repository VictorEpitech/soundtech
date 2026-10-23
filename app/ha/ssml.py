"""SSML Alexa : texte utilisateur toujours échappé, voix/langue en liste blanche."""

import re
from xml.sax.saxutils import escape, quoteattr

from fastapi import HTTPException

from app.config import Settings

_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


STYLES: dict[str, str] = {
    "whisper": "Chuchoté",
    "slow": "Lent",
    "fast": "Rapide",
    "soft": "Doux",
    "loud": "Fort",
    "low": "Grave",
    "high": "Aigu",
    "emphasis": "Appuyé",
}

_STYLE_TAGS: dict[str, tuple[str, str]] = {
    "whisper": ('<amazon:effect name="whispered">', "</amazon:effect>"),
    "slow": ('<prosody rate="slow">', "</prosody>"),
    "fast": ('<prosody rate="fast">', "</prosody>"),
    "soft": ('<prosody volume="soft">', "</prosody>"),
    "loud": ('<prosody volume="loud">', "</prosody>"),
    "low": ('<prosody pitch="low">', "</prosody>"),
    "high": ('<prosody pitch="high">', "</prosody>"),
    "emphasis": ('<emphasis level="strong">', "</emphasis>"),
}


def validate_style(style: str | None) -> str | None:
    if style is not None and style not in STYLES:
        raise HTTPException(422, f"style not allowed: {style[:16]!r}")
    return style


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
    text: str,
    voice: str | None,
    lang: str | None,
    *,
    prefix: str | None = None,
    style: str | None = None,
) -> str | None:
    """None si ni voix, ni langue, ni style. Le préfixe (TEST) reste hors de tout habillage."""
    if not voice and not lang and not style:
        return None
    inner = _text(text)
    if style:
        open_, close = _STYLE_TAGS[style]
        inner = f"{open_}{inner}{close}"
    if lang:
        inner = f"<lang xml:lang={quoteattr(lang)}>{inner}</lang>"
    if voice:
        inner = f"<voice name={quoteattr(voice)}>{inner}</voice>"
    head = f"{_text(prefix)} " if prefix else ""
    return f"<speak>{head}{inner}</speak>"
