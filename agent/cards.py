"""james · renders ar design studio cards (feed carousel slides 1080x1350, stories 1080x1920) as JPEG with Pillow.

Style: quiet luxury. near-black ground, warm off-white type, one champagne accent word per card, hairline frame, lowercase
mono label. Instagram accepts JPEG (not PNG) for feed images, so everything is saved as high-quality JPEG.
Text markup: wrap the accent word(s) in *asterisks*.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG, INK, STONE, ACCENT, LINE = (14, 14, 15), (237, 232, 224), (184, 175, 163), (183, 155, 107), (52, 51, 50)
POST_SIZE, STORY_SIZE = (1080, 1350), (1080, 1920)
HANDLE = "@ar_design_agency"
LABEL = "ar design studio"

FONT_CANDIDATES = {
    "serif": ["georgia.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", "DejaVuSerif.ttf"],
    "sans": ["arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"],
    "mono": ["consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "DejaVuSansMono.ttf"],
}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for name in FONT_CANDIDATES[kind]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def tokens(text: str) -> list[tuple[str, bool]]:
    """Splits 'a *b* c' into words with an accent flag; punctuation right after the accent stays glued to it."""
    out: list[tuple[str, bool]] = []
    for m in re.finditer(r"\*([^*]+)\*(\S*)|(\S+)", text):
        if m.group(3):
            out.append((m.group(3), False))
        else:
            words = m.group(1).split()
            words[-1] += m.group(2)
            out += [(w, True) for w in words]
    return out


def layout(words: list[tuple[str, bool]], fnt, width: int) -> list[list[tuple[str, bool]]]:
    lines, cur = [], []
    for word, acc in words:
        trial = " ".join(w for w, _ in cur + [(word, acc)])
        if cur and fnt.getlength(trial) > width:
            lines.append(cur)
            cur = []
        cur.append((word, acc))
    return lines + ([cur] if cur else [])


def draw_lines(d: ImageDraw.ImageDraw, lines, fnt, x: int, y: int, lead: float, base, accent=ACCENT) -> int:
    for line in lines:
        cx = x
        for word, acc in line:
            d.text((cx, y), word, font=fnt, fill=accent if acc else base)
            cx += fnt.getlength(word + " ")
        y += int(fnt.size * lead)
    return y


def fit_headline(text: str, width: int, max_lines: int, start: int, floor: int = 56):
    for size in range(start, floor - 1, -4):
        fnt = font("serif", size)
        lines = layout(tokens(text), fnt, width)
        if len(lines) <= max_lines:
            return fnt, lines
    return fnt, lines


def render_card(headline: str, body: str = "", index: str = "", size=POST_SIZE, footer: str = HANDLE, big: bool = False) -> Image.Image:
    w, h = size
    story = h > w * 1.5
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    pad = 84
    top_safe = 250 if story else 96  # stories: keep clear of the profile bar and reply box
    bottom_safe = 340 if story else 96

    d.rectangle([40, 40, w - 40, h - 40], outline=LINE, width=2)  # hairline frame
    d.text((pad, top_safe - 48 if story else 84), LABEL, font=font("mono", 28), fill=STONE)
    if index:
        label = font("mono", 28)
        d.text((w - pad - label.getlength(index), top_safe - 48 if story else 84), index, font=label, fill=STONE)

    text_w = w - 2 * pad
    fnt, lines = fit_headline(headline, text_w, max_lines=6 if not big else 7, start=132 if big else 104)
    block_h = len(lines) * int(fnt.size * 1.12)
    body_font = font("sans", 38)
    body_lines = layout(tokens(body), body_font, text_w) if body else []
    body_h = len(body_lines) * int(38 * 1.5) + (48 if body else 0)
    free_top, free_bottom = top_safe + 80, h - bottom_safe - 60
    y = free_top + max(0, (free_bottom - free_top - block_h - body_h) // 2)
    y = draw_lines(d, lines, fnt, pad, y, 1.12, INK)
    if body_lines:
        d.line([pad, y + 18, pad + 72, y + 18], fill=ACCENT, width=3)
        draw_lines(d, body_lines, body_font, pad, y + 48, 1.5, STONE, accent=INK)

    fy = h - bottom_safe + (0 if story else -12)
    d.line([pad, fy - 24, w - pad, fy - 24], fill=LINE, width=2)
    d.text((pad, fy), footer, font=font("mono", 30), fill=STONE)
    return img


def save(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=92, subsampling=0, optimize=True)
    return path


def render_carousel(slides: list[dict], out_dir: Path, stem: str) -> list[Path]:
    total = len(slides)
    paths = []
    for i, s in enumerate(slides, 1):
        img = render_card(s["h"], s.get("b", ""), f"{i:02d} / {total:02d}", POST_SIZE, big=(i == 1))
        paths.append(save(img, out_dir / f"{stem}-{i}.jpg"))
    return paths


def render_story(story: dict, out_dir: Path, stem: str) -> Path:
    return save(render_card(story["h"], story.get("b", ""), "", STORY_SIZE, big=True), out_dir / f"{stem}.jpg")
