#!/usr/bin/env python3
"""Генератор статических B2B-каруселей (Instagram / LinkedIn): JSON -> slide_N.png, 1080x1350 (4:5).

Дизайн Quiet Luxury: графитовый фон #141414 с тонкой рамкой, внутри UI-карточка #232323 со скруглением и золотым
бордюром #B89B67; заголовок - Playfair Display, подзаголовок - чистый sans (Segoe UI / Arial); слева сверху бренд,
справа сверху маркер N/M; на CTA-слайде @r_design_space_bot выделен золотым.

Запуск (pillow, шрифт Playfair лежит в ../reels_01/assets/fonts):
  python build_carousel.py                                  # carousel_01_es.json -> output_01/
  python build_carousel.py --json carousel_02.json --out output_02
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONTS = HERE.parent / "reels_01" / "assets" / "fonts"
WIN = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"

W, H = 1080, 1350
BG, SURFACE, TEXT, GOLD = (20, 20, 20), (35, 35, 35), (242, 239, 232), (184, 155, 103)
STONE = tuple(int(TEXT[i] * 0.62 + BG[i] * 0.38) for i in range(3))

FRAME = 28  # отступ внешней рамки
CARD = (72, 128, W - 72, H - 128)  # UI-карточка
PAD = 76  # внутренний отступ карточки
HANDLE = "@r_design_space_bot"

_cache: dict = {}


def font(path: Path, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    key = (str(path), size, weight)
    if key not in _cache:
        f = ImageFont.truetype(str(path), size)
        if weight is not None:
            try:
                f.set_variation_by_axes([weight])
            except Exception:
                pass
        _cache[key] = f
    return _cache[key]


def serif(size: int, weight: int = 500) -> ImageFont.FreeTypeFont:
    p = FONTS / "PlayfairDisplay.ttf"
    return font(p if p.exists() else WIN / "georgia.ttf", size, weight if p.exists() else None)


def sans(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arial.ttf"):
        if (WIN / name).exists():
            return font(WIN / name, size)
    raise SystemExit("Не найден sans-шрифт (Segoe UI / Arial)")


def wrap(d: ImageDraw.ImageDraw, text: str, f, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=f) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_title(d, text: str, sizes, max_w: int, max_lines: int):
    for size in sizes:
        f = serif(size)
        lines = wrap(d, text, f, max_w)
        if len(lines) <= max_lines:
            return lines, f, size
    f = serif(sizes[-1])
    return wrap(d, text, f, max_w), f, sizes[-1]


def draw_rich(d, x: int, y: int, text: str, f, fb, max_w: int, base, accent, lh: int) -> int:
    """Абзац со словами-акцентами (@handle) золотым жирным; возвращает y после абзаца."""
    tokens = text.split(" ")
    line, width = [], 0.0
    space = d.textlength(" ", font=f)
    rows: list[list[str]] = []
    for tok in tokens:
        fw = fb if tok.startswith("@") else f
        w = d.textlength(tok, font=fw)
        if line and width + space + w > max_w:
            rows.append(line)
            line, width = [], 0.0
        width += (space if line else 0) + w
        line.append(tok)
    if line:
        rows.append(line)
    for row in rows:
        cx = x
        for tok in row:
            is_acc = tok.startswith("@") or tok.strip(".,:;").startswith("@")
            ff = fb if is_acc else f
            if is_acc:  # знаки препинания после handle остаются обычным цветом
                m = re.match(r"(@[\w]+)(.*)", tok)
                d.text((cx, y), m.group(1), font=ff, fill=accent)
                cx += d.textlength(m.group(1), font=ff)
                d.text((cx, y), m.group(2), font=f, fill=base)
                cx += d.textlength(m.group(2), font=f)
            else:
                d.text((cx, y), tok, font=ff, fill=base)
                cx += d.textlength(tok, font=ff)
            cx += space
        y += lh
    return y


def chevron(d, x: int, y: int, color, width: int = 3):
    """Тонкая стрелка «→» (в шрифтах её может не быть, поэтому рисуем линиями)."""
    d.line([(x, y), (x + 54, y)], fill=color, width=width)
    d.line([(x + 40, y - 12), (x + 54, y), (x + 40, y + 12)], fill=color, width=width, joint="curve")


def base_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle((FRAME, FRAME, W - FRAME, H - FRAME), outline=(58, 58, 58), width=1)  # тонкая рамка по периметру
    d.rounded_rectangle(CARD, radius=44, fill=SURFACE, outline=GOLD, width=2)  # карточка с золотым бордюром
    return img, d


def render_slide(slide: dict, total: int, brand: str) -> Image.Image:
    img, d = base_canvas()
    n, typ = int(slide["slide_number"]), slide["type"].upper()
    x0, x1 = CARD[0] + PAD, CARD[2] - PAD
    max_w = x1 - x0

    # шапка: бренд слева, маркер справа
    d.text((x0, CARD[1] + 62), brand, font=sans(26, True), fill=GOLD, anchor="lm")
    d.text((x1, CARD[1] + 62), f"{n}/{total}", font=serif(30, 500), fill=STONE, anchor="rm")
    d.line([(x0, CARD[1] + 104), (x1, CARD[1] + 104)], fill=(60, 60, 60), width=1)

    title, sub = slide["title"].strip(), slide["subtitle"].strip()
    num = None
    m = re.match(r"^(\d{1,2})\.\s*(.+)$", title)
    if m and typ in ("KEY_POINT", "INSIGHT"):
        num, title = m.group(1), m.group(2)

    top, bottom = CARD[1] + 150, CARD[3] - 130  # рабочая область
    if typ == "COVER":
        sizes, max_lines = (104, 94, 84, 76), 5
    elif typ == "CTA":
        sizes, max_lines = (92, 84, 76, 68), 5
    else:
        sizes, max_lines = (84, 76, 68, 60), 5
    lines, f_t, size = fit_title(d, title, sizes, max_w, max_lines)
    lh_t = int(size * 1.2)
    f_s, f_sb = sans(38), sans(38, True)
    sub_lines = wrap(d, sub, f_s, max_w)
    lh_s = 58
    block_h = len(lines) * lh_t + 56 + len(sub_lines) * lh_s + (170 if num else 0)
    y = top + max(0, (bottom - top - block_h) // 2)

    if num:  # крупный золотой номер пункта
        d.text((x0, y), num, font=serif(150, 400), fill=GOLD)
        y += 170
    for line in lines:
        d.text((x0, y), line, font=f_t, fill=TEXT)
        y += lh_t
    y += 24
    d.line([(x0, y), (x0 + (200 if typ in ("COVER", "CTA") else 110), y)], fill=GOLD, width=3)  # золотая линия-акцент
    y += 32
    y = draw_rich(d, x0, y, sub, f_s, f_sb, max_w, (205, 201, 193), GOLD, lh_s)

    if typ == "CTA":  # кнопка с ботом
        by = min(y + 30, CARD[3] - 190)
        label = HANDLE
        fw = sans(40, True)
        bw = int(d.textlength(label, font=fw)) + 96
        d.rounded_rectangle((x0, by, x0 + bw, by + 96), radius=48, outline=GOLD, width=3)
        d.text((x0 + bw / 2, by + 48), label, font=fw, fill=GOLD, anchor="mm")
    elif n < total:  # подсказка «листайте»
        chevron(d, x1 - 54, CARD[3] - 62, GOLD)

    # прогресс-полоса внизу карточки
    py = CARD[3] - 36
    d.line([(x0, py), (x1, py)], fill=(60, 60, 60), width=3)
    d.line([(x0, py), (x0 + int(max_w * n / total), py)], fill=GOLD, width=3)
    return img


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", default=str(HERE / "carousel_01_es.json"))
    ap.add_argument("--out", default="output_01", help="папка результата (относительно carousels/)")
    args = ap.parse_args()

    data = json.loads(Path(args.json).read_text(encoding="utf-8-sig"))
    slides = sorted(data["slides"], key=lambda s: s["slide_number"])
    out = (HERE / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    brand = data.get("brand", "R DESIGN SPACE")
    for s in slides:
        path = out / f"slide_{s['slide_number']}.png"
        render_slide(s, len(slides), brand).save(path, optimize=True)
        print(f"{path.name}  {s['type']:9s} {s['title'][:52]}")
    print(f"Готово: {len(slides)} слайдов 1080x1350 -> {out}")


if __name__ == "__main__":
    sys.exit(main())
