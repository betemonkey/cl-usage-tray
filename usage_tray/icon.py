"""Draws the tray icon: a ring that fills clockwise with the estimated %."""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

SIZE = 64
GREEN, YELLOW, RED, TRACK = "#0ca30c", "#fab219", "#d03b3b", "#5a5a5a"  # same status colours as the UI


def color_for(pct: float, yellow_at: float = 60, red_at: float = 85) -> str:
    if pct > red_at:
        return RED
    if pct >= yellow_at:
        return YELLOW
    return GREEN


def _font(size: int):
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def ring_icon(pct: float, yellow_at: float = 60, red_at: float = 85) -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    box, width = (6, 6, SIZE - 6, SIZE - 6), 14
    d.arc(box, 0, 360, fill=TRACK, width=width)
    pct = max(0.0, min(pct, 100.0))
    if pct > 0:
        d.arc(box, -90, -90 + 360 * pct / 100, fill=color_for(pct, yellow_at, red_at), width=width)
    return img


def lock_icon(seconds_left: float) -> Image.Image:
    """Solid red disc with the time left until reset (e.g. '2h', '45m')."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((1, 1, SIZE - 1, SIZE - 1), fill=RED)
    m = max(0, int(seconds_left // 60))
    text = f"{round(m / 60)}h" if m >= 90 else f"{m}m"
    font = _font(30 if len(text) <= 2 else 24)
    d.text((SIZE / 2, SIZE / 2), text, fill="white", font=font, anchor="mm")
    return img
