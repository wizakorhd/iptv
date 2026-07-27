#!/usr/bin/env python3
"""Generate simple, tasteful per-genre fallback logo tiles (16:9 PNGs) used for
channels that ship no tvg-logo and aren't in the iptv-org logo registry.

One-time asset generator: output lands in docs/logos/genre/<slug>.png and is
referenced by generate_playlist.py via the raw GitHub URL. Re-run only to tweak
the tiles. Requires Pillow.
"""
import os
import re
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "docs", "logos", "genre")

W, H = 480, 270
BG = (17, 18, 22)          # near-black card
FG = (236, 238, 242)       # off-white text
SUB = (120, 124, 132)      # muted subtitle

# genre -> accent colour (muted, distinct). Used only as a small dot + subtle
# full-border tint (never a left-edge stripe).
GENRES = {
    "News": (86, 130, 214),
    "Movies": (196, 92, 92),
    "Series": (150, 106, 200),
    "Entertainment": (214, 142, 70),
    "General": (110, 118, 130),
    "Comedy": (222, 190, 78),
    "Reality": (206, 108, 160),
    "Sports": (86, 176, 110),
    "Documentary": (94, 168, 176),
    "Music": (176, 96, 190),
    "Kids": (232, 150, 96),
    "Food & Travel": (170, 158, 84),
    "Horror": (168, 70, 70),
    "Sci-Fi": (90, 150, 200),
    "Crime": (120, 120, 128),
    "Anime": (200, 96, 130),
    "Korean": (110, 140, 200),
    "TV": (96, 100, 110),   # generic default
}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_font(size: int):
    for p in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def fit_font(text: str, max_w: int, start: int):
    size = start
    while size > 16:
        f = load_font(size)
        if f.getlength(text) <= max_w:
            return f
        size -= 2
    return load_font(16)


def make(name: str, accent):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    # subtle full-border tint (not a left stripe)
    tint = tuple(int(BG[i] + (accent[i] - BG[i]) * 0.35) for i in range(3))
    d.rectangle([0, 0, W - 1, H - 1], outline=tint, width=6)
    # accent dot
    d.ellipse([28, 28, 48, 48], fill=accent)
    # genre name centred
    label = name.upper()
    f = fit_font(label, W - 80, 56)
    tw = f.getlength(label)
    bbox = f.getbbox(label)
    th = bbox[3] - bbox[1]
    d.text(((W - tw) / 2, (H - th) / 2 - 12), label, font=f, fill=FG)
    # subtitle
    sf = load_font(20)
    sub = "LIVE TV"
    sw = sf.getlength(sub)
    d.text(((W - sw) / 2, H - 52), sub, font=sf, fill=SUB)
    os.makedirs(OUT, exist_ok=True)
    img.save(os.path.join(OUT, slug(name) + ".png"))


if __name__ == "__main__":
    for name, accent in GENRES.items():
        make(name, accent)
    print(f"Wrote {len(GENRES)} genre tiles to {OUT}")
