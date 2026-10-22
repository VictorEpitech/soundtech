"""Génère les icônes PWA (PNG) dans app/static/icons. Usage : python scripts/gen_icons.py (Pillow requis)."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
BG = (42, 75, 90, 255)  # primary #2A4B5A
FG = (255, 255, 255, 255)
BARS = (0.35, 0.6, 0.9, 0.6, 0.35)  # égaliseur : hauteurs relatives


def draw(size: int, *, maskable: bool, scale: int = 4) -> Image.Image:
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:
        d.rectangle((0, 0, s, s), fill=BG)
        zone = 0.6  # contenu dans la zone sûre (cercle central de 80 %)
    else:
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=BG)
        zone = 0.64
    span = s * zone
    n = len(BARS)
    bar_w = span / (n * 2 - 1)
    x0 = (s - span) / 2
    for i, h in enumerate(BARS):
        bh = span * h
        x = x0 + i * 2 * bar_w
        y = (s - bh) / 2
        d.rounded_rectangle((x, y, x + bar_w, y + bh), radius=bar_w / 2, fill=FG)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        draw(size, maskable=False).save(OUT / f"icon-{size}.png", optimize=True)
        draw(size, maskable=True).save(OUT / f"icon-maskable-{size}.png", optimize=True)
    draw(180, maskable=True).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)


if __name__ == "__main__":
    main()
