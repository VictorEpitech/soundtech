#!/usr/bin/env python3
"""Génère une paire de clés VAPID (P-256) pour le Web Push : à copier dans l'environnement."""

import base64
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> int:
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    print(f"VAPID_PRIVATE_KEY={b64url(private)}")
    print(f"VAPID_PUBLIC_KEY={b64url(public)}")
    print("VAPID_SUBJECT=mailto:admin@example.org  # à adapter (contact de l'exploitant)")
    print("La clé privée est un secret : ne la commitez pas.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
