"""Post layouts (Pillow). Everything visual lives here; branding comes from config.py / env vars.

Carousel structure:  cover  ->  numbered content slides  ->  call-to-action slide
Single post:         full-bleed picture + kicker + headline
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from config import settings

W, H = 1080, 1350  # 4:5 portrait
MARGIN = 80
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

_FONT_FILES = {
    "bold": ["Poppins-Bold.ttf", "Bold.ttf"],
    "medium": ["Poppins-Medium.ttf"],
    "regular": ["Poppins-Regular.ttf"],
}
_SYSTEM_FONTS = {
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    "medium": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
               "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
    "regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
}

# Gradient palettes used for "designed" backgrounds (also when no AI picture is available).
_PALETTES = [
    ((14, 11, 42), (66, 28, 118)),
    ((7, 22, 46), (18, 86, 128)),
    ((38, 11, 38), (124, 38, 86)),
    ((9, 28, 30), (18, 104, 88)),
    ((28, 17, 12), (116, 66, 28)),
    ((13, 13, 33), (56, 58, 146)),
]


# ----------------------------------------------------------------------------- basics
@lru_cache(maxsize=64)
def font(size: int, weight: str = "bold"):
    candidates = [str(FONT_DIR / f) for f in _FONT_FILES[weight]] + _SYSTEM_FONTS[weight]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def accent() -> tuple[int, int, int]:
    value = settings.brand_accent.lstrip("#")
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except (ValueError, IndexError):
        return (139, 92, 246)


def fit_cover(img: Image.Image) -> Image.Image:
    return ImageOps.fit(img.convert("RGB"), (W, H), Image.LANCZOS)


def _tw(draw, text, f, tracking: int = 0) -> float:
    if not tracking:
        return draw.textlength(text, font=f)
    return sum(draw.textlength(c, font=f) for c in text) + tracking * max(len(text) - 1, 0)


def _text(draw, xy, text, f, fill, tracking: int = 0, shadow: bool = False) -> None:
    x, y = xy
    if shadow:
        _text(draw, (x + 2, y + 4), text, f, (0, 0, 0, 120), tracking)
    if not tracking:
        draw.text((x, y), text, font=f, fill=fill)
        return
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill)
        x += draw.textlength(ch, font=f) + tracking


def _wrap(draw, text: str, f, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=f) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit_block(draw, text, weight, max_w, max_h, sizes, spacing=1.2, max_lines=None):
    """Largest font size from `sizes` for which the wrapped text fits the box."""
    for size in sizes:
        f = font(size, weight)
        lines = _wrap(draw, text, f, max_w)
        lh = int(size * spacing)
        if len(lines) * lh <= max_h and (max_lines is None or len(lines) <= max_lines):
            return f, lines, lh
    size = sizes[-1]
    f, lh = font(size, weight), int(size * spacing)
    lines = _wrap(draw, text, f, max_w)
    keep = max(1, min(max_lines or 99, max_h // lh))
    if len(lines) > keep:
        lines = lines[:keep]
        lines[-1] = lines[-1].rstrip(" .,;:") + "…"
    return f, lines, lh


# ----------------------------------------------------------------------------- backgrounds
def designed_background(i: int = 0) -> Image.Image:
    """A branded gradient with soft glows. Different for every slide index."""
    c1, c2 = _PALETTES[i % len(_PALETTES)]
    mask = Image.linear_gradient("L").resize((W, H))
    base = Image.composite(Image.new("RGB", (W, H), c2), Image.new("RGB", (W, H), c1), mask)

    glow = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(glow)
    ac = accent()
    for k in range(2):
        cx = int(W * (0.15 + 0.7 * (((i * 0.37) + k * 0.53) % 1)))
        cy = int(H * (0.10 + 0.8 * (((i * 0.61) + k * 0.29) % 1)))
        r = 360 + k * 120
        color = tuple(int(v * 0.55) for v in ac) if k == 0 else tuple(int(v * 0.8) for v in c2)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    glow = glow.filter(ImageFilter.GaussianBlur(150))
    return ImageChops.screen(base, glow)


def _scrim(img: Image.Image, top: int, bottom: int) -> None:
    """Dark vertical gradient so white text stays readable on any picture."""
    mask = Image.linear_gradient("L").resize((W, H)).point(lambda v: int(top + (bottom - top) * v / 255))
    img.paste((5, 5, 16), (0, 0, W, H), mask)


# ----------------------------------------------------------------------------- chrome
def _header(draw, index: int, total: int) -> None:
    ac = accent()
    draw.ellipse((MARGIN, 90, MARGIN + 16, 106), fill=ac)
    _text(draw, (MARGIN + 32, 80), settings.brand_kicker.upper(), font(26, "bold"), (255, 255, 255, 235), tracking=4)
    if total > 1:
        label = f"{index:02d} / {total:02d}"
        f = font(26, "medium")
        _text(draw, (W - MARGIN - _tw(draw, label, f), 80), label, f, (255, 255, 255, 200))


def _footer(draw, index: int, total: int, left_text: str = "", swipe: bool = False) -> None:
    ac = accent()
    if total > 1:
        y = H - 120
        draw.rounded_rectangle((MARGIN, y, W - MARGIN, y + 6), radius=3, fill=(255, 255, 255, 60))
        draw.rounded_rectangle((MARGIN, y, MARGIN + int((W - 2 * MARGIN) * index / total), y + 6), radius=3, fill=ac)
    f = font(26, "medium")
    if left_text:
        _text(draw, (MARGIN, H - 86), left_text, f, (255, 255, 255, 190))
    if swipe:
        label = "SWIPE"
        lf = font(26, "bold")
        w = _tw(draw, label, lf, 4)
        x = W - MARGIN - w - 56
        _text(draw, (x, H - 86), label, lf, ac + (255,), tracking=4)
        ax, ay = W - MARGIN - 40, H - 70  # drawn arrow
        draw.line((ax, ay, ax + 38, ay), fill=ac + (255,), width=4)
        draw.line((ax + 26, ay - 12, ax + 38, ay), fill=ac + (255,), width=4)
        draw.line((ax + 26, ay + 12, ax + 38, ay), fill=ac + (255,), width=4)


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _pill(draw, x: int, y: int, text: str) -> int:
    """Accent-coloured topic label. Returns its height."""
    f = font(26, "bold")
    pad_x, h = 26, 54
    w = int(_tw(draw, text, f, 3))
    draw.rounded_rectangle((x, y, x + w + 2 * pad_x, y + h), radius=h // 2, fill=accent() + (255,))
    _text(draw, (x + pad_x, y + 8), text, f, (255, 255, 255, 255), tracking=3)
    return h


# ----------------------------------------------------------------------------- layouts
def render_cover(bg: Image.Image, title: str, subtitle: str, index: int, total: int, topic: str = "") -> Image.Image:
    img = fit_cover(bg)
    _scrim(img, 90, 238)
    draw = ImageDraw.Draw(img, "RGBA")
    _header(draw, index, total)

    max_w, bottom = W - 2 * MARGIN, H - 170
    tf, tl, tlh = _fit_block(draw, title, "bold", max_w, 560, range(104, 55, -4), 1.14, max_lines=6)
    sf, sl, slh = (None, [], 0)
    if subtitle:
        sf, sl, slh = _fit_block(draw, subtitle, "regular", max_w, 170, range(44, 29, -2), 1.35, max_lines=3)

    lead_h = 54 if topic else 10
    block = lead_h + 34 + len(tl) * tlh + (34 + len(sl) * slh if sl else 0)
    y = bottom - block
    if topic:
        _pill(draw, MARGIN, y, topic)
    else:
        draw.rounded_rectangle((MARGIN, y, MARGIN + 130, y + 10), radius=5, fill=accent())
    y += lead_h + 34
    for line in tl:
        _text(draw, (MARGIN, y), line, tf, (255, 255, 255, 255), shadow=True)
        y += tlh
    y += 34
    for line in sl:
        _text(draw, (MARGIN, y), line, sf, (255, 255, 255, 215))
        y += slh
    _footer(draw, index, total, settings.brand_handle, swipe=total > 1)
    return img


def render_content(bg: Image.Image, number: int, title: str, body: str, index: int, total: int) -> Image.Image:
    img = fit_cover(bg)
    _scrim(img, 125, 215)
    draw = ImageDraw.Draw(img, "RGBA")
    _header(draw, index, total)

    ac = accent()
    _text(draw, (MARGIN - 6, 140), f"{number:02d}", font(210, "bold"), ac + (235,))

    pad = 52
    inner_w = W - 2 * MARGIN - 2 * pad
    tf, tl, tlh = _fit_block(draw, title, "bold", inner_w, 300, range(70, 43, -4), 1.2, max_lines=4)
    bl, bf, blh = [], None, 0
    if body:
        bf, bl, blh = _fit_block(draw, body, "regular", inner_w, 330, range(44, 29, -2), 1.4, max_lines=7)

    card_h = pad + len(tl) * tlh + (28 + len(bl) * blh if bl else 0) + pad + 10 + 28
    bottom = H - 170
    top = bottom - card_h
    draw.rounded_rectangle((MARGIN, top, W - MARGIN, bottom), radius=40, fill=(8, 8, 22, 190),
                           outline=(255, 255, 255, 45), width=2)
    draw.rounded_rectangle((MARGIN + pad, top + pad - 4, MARGIN + pad + 90, top + pad + 2), radius=3, fill=ac)

    y = top + pad + 28
    for line in tl:
        _text(draw, (MARGIN + pad, y), line, tf, (255, 255, 255, 255))
        y += tlh
    if bl:
        y += 28
        for line in bl:
            _text(draw, (MARGIN + pad, y), line, bf, (255, 255, 255, 215))
            y += blh
    _footer(draw, index, total, settings.brand_handle)
    return img


def render_cta(bg: Image.Image, title: str, body: str, index: int, total: int, source: str = "") -> Image.Image:
    img = fit_cover(bg)
    _scrim(img, 150, 232)
    draw = ImageDraw.Draw(img, "RGBA")
    _header(draw, index, total)

    ac = accent()
    max_w = W - 2 * MARGIN
    tf, tl, tlh = _fit_block(draw, title, "bold", max_w, 360, range(88, 51, -4), 1.18, max_lines=5)
    bf, bl, blh = (None, [], 0)
    if body:
        bf, bl, blh = _fit_block(draw, body, "regular", max_w - 60, 200, range(42, 29, -2), 1.4, max_lines=4)

    btn_h = 112
    block = len(tl) * tlh + (30 + len(bl) * blh if bl else 0) + 60 + btn_h + 50 + 30
    y = int(H * 0.5 - block / 2)
    for line in tl:
        _text(draw, ((W - _tw(draw, line, tf)) / 2, y), line, tf, (255, 255, 255, 255), shadow=True)
        y += tlh
    if bl:
        y += 30
        for line in bl:
            _text(draw, ((W - _tw(draw, line, bf)) / 2, y), line, bf, (255, 255, 255, 215))
            y += blh
    y += 60
    label = f"FOLLOW {settings.brand_handle}" if settings.brand_handle else "FOLLOW FOR MORE"
    lf = font(38, "bold")
    btn_w = int(_tw(draw, label, lf, 3) + 110)
    x0 = (W - btn_w) // 2
    draw.rounded_rectangle((x0, y, x0 + btn_w, y + btn_h), radius=btn_h // 2, fill=ac + (255,))
    _text(draw, (x0 + 55, y + 28), label, lf, (255, 255, 255, 255), tracking=3)
    y += btn_h + 50
    hint = "SAVE   •   SHARE   •   COMMENT"
    hf = font(26, "medium")
    _text(draw, ((W - _tw(draw, hint, hf, 4)) / 2, y), hint, hf, (255, 255, 255, 190), tracking=4)

    _footer(draw, index, total, f"Source: {_short(source, 48)}" if source else "")
    return img


def render_single(bg: Image.Image, headline: str, source: str = "", topic: str = "") -> Image.Image:
    img = fit_cover(bg)
    _scrim(img, 45, 228)
    draw = ImageDraw.Draw(img, "RGBA")
    _header(draw, 1, 1)

    max_w, bottom = W - 2 * MARGIN, H - 150
    tf, tl, tlh = _fit_block(draw, headline, "bold", max_w, 520, range(96, 55, -4), 1.14, max_lines=5)
    lead_h = 54 if topic else 10
    y = bottom - (lead_h + 34 + len(tl) * tlh)
    if topic:
        _pill(draw, MARGIN, y, topic)
    else:
        draw.rounded_rectangle((MARGIN, y, MARGIN + 130, y + 10), radius=5, fill=accent())
    y += lead_h + 34
    for line in tl:
        _text(draw, (MARGIN, y), line, tf, (255, 255, 255, 255), shadow=True)
        y += tlh
    left = f"Source: {_short(source, 48)}" if source else settings.brand_handle
    _footer(draw, 1, 1, left)
    return img


def render_carousel(backgrounds: list[Image.Image], post: dict, article: dict) -> list[Image.Image]:
    titles = post["slides_text"]
    bodies = list(post.get("slides_body") or [])
    n = len(titles)
    source = article.get("source", "")
    out = []
    for i, title in enumerate(titles, 1):
        body = bodies[i - 1] if i - 1 < len(bodies) else ""
        bg = backgrounds[i - 1]
        if i == 1:
            out.append(render_cover(bg, title, body, i, n, post.get("topic", "")))
        elif i == n and n >= 3:
            out.append(render_cta(bg, title, body, i, n, source))
        else:
            out.append(render_content(bg, i - 1, title, body, i, n))
    return out
