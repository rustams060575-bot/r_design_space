#!/usr/bin/env python3
"""Автосборка Reels 01 («Before / After: шаблон -> Quiet Luxury») в reel_01.mp4.

Что делает:
  1. читает закадровый текст из script.md (блок ```voiceover```);
  2. озвучивает каждую сцену (OpenAI tts-1-hd -> edge-tts -> Windows SAPI, см. --tts);
  3. рисует кадры 1080x1920 (PIL: фон, сетка, шрифты Playfair Display / Georgia, анимации) и отдаёт их в ffmpeg;
  4. длительность сцены = max(план, озвучка + пауза), поэтому голос и картинка не расходятся;
  5. сводит видео, голос и тихий ambient-фон в reel_01.mp4 (H.264, yuv420p, AAC, 30 fps).

Запуск:
  pip install pillow numpy requests edge-tts      (ffmpeg должен быть в PATH)
  python build_video.py                           # озвучка: OpenAI, если есть OPENAI_API_KEY, иначе edge-tts
  python build_video.py --tts openai --voice onyx
  python build_video.py --tts edge --voice ru-RU-DmitryNeural
  python build_video.py --preview                 # только стоп-кадры в build/preview, без видео

Формат batch_scripts (dict со scripts[], сцены с layout HERO/BENTO/METRIC/QUOTE; испанский):
  python build_video.py --json ../reels_02/batch_scripts_es_02.json --build-dir ../reels_02/build --tts openai --voice onyx

Пакетный режим (JSON-массив роликов -> build/<id>.mp4 + build/manifest.json):
  python build_video.py --json reels.json                  # все ролики из файла
  python build_video.py --json reels.json --only reel_02   # один или несколько id
  Поля ролика: id, title, hook_screen, voiceover_text, screen_text[], cta.
  Сцены: hook_screen -> screen_text[1:-1] -> финальная CTA-сцена; озвучка режется на предложения и раскладывается
  по сценам по порядку (предложения про Telegram/bio идут в последнюю сцену). Язык (ru/en) определяется автоматически.

OPENAI_API_KEY берётся из окружения или из agent/.env (файл в .gitignore).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
BUILD = HERE / "build"
OUT = HERE / "reel_01.mp4"

W, H, FPS = 1080, 1920, 30

# палитра из content_strategy.md
BG = (14, 14, 15)
SURFACE = (23, 23, 26)
LIGHT = (237, 232, 224)
STONE = (184, 175, 163)
GOLD = (183, 155, 107)

PLAYFAIR = HERE / "assets" / "fonts" / "PlayfairDisplay.ttf"
PLAYFAIR_IT = HERE / "assets" / "fonts" / "PlayfairDisplay-Italic.ttf"
GEORGIA = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "georgia.ttf"
GEORGIA_IT = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "georgiai.ttf"
ARIAL_BD = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arialbd.ttf"


# ----------------------------------------------------------------------------- fonts

_font_cache: dict = {}


def font(path: Path, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    key = (str(path), size, weight)
    if key not in _font_cache:
        if not path.exists():
            raise SystemExit(f"Не найден шрифт: {path}")
        f = ImageFont.truetype(str(path), size)
        if weight is not None:
            try:
                f.set_variation_by_axes([weight])
            except Exception:
                pass
        _font_cache[key] = f
    return _font_cache[key]


def serif(size, weight=400):
    return font(PLAYFAIR, size, weight)


def serif_it(size, weight=400):
    return font(PLAYFAIR_IT, size, weight)


def text_font(size):
    return font(GEORGIA if GEORGIA.exists() else PLAYFAIR, size)


# ----------------------------------------------------------------------------- easing / drawing helpers


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease_out(t):  # приближение cubic-bezier(0.22, 1, 0.36, 1)
    t = clamp(t)
    return 1 - (1 - t) ** 4


def ease_io(t):
    t = clamp(t)
    return 3 * t * t - 2 * t**3


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def with_alpha(color, a):
    return (*color, int(255 * clamp(a)))


def make_background() -> Image.Image:
    """Тёмный фон с мягким светом слева сверху и виньеткой."""
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    glow = np.exp(-(((x - W * 0.2) / (W * 0.9)) ** 2 + ((y - H * 0.18) / (H * 0.45)) ** 2))
    vig = 1 - 0.55 * (((x - W / 2) / (W / 2)) ** 2 * 0.5 + ((y - H / 2) / (H / 2)) ** 2 * 0.5)
    base = np.array(BG, dtype=np.float32)[None, None, :]
    img = base * (0.85 + 0.55 * glow[..., None]) * vig[..., None]
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")


_rng = np.random.default_rng(7)
GRAIN = [(_rng.normal(0, 3.2, (H, W, 1))).astype(np.float32) for _ in range(6)]


def text_size(draw, s, f):
    b = draw.textbbox((0, 0), s, font=f)
    return b[2] - b[0], b[3] - b[1]


def draw_text(img, xy, s, f, fill, alpha=1.0, anchor="la", rise=0.0):
    """Текст с прозрачностью и подъёмом (rise — смещение в px, уменьшающееся к нулю)."""
    if alpha <= 0.01:
        return
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.text((xy[0], xy[1] + rise), s, font=f, fill=with_alpha(fill, alpha), anchor=anchor)
    img.alpha_composite(layer) if img.mode == "RGBA" else img.paste(layer, (0, 0), layer)


def wrap(draw, s, f, max_w):
    lines, cur = [], ""
    for word in s.split():
        trial = (cur + " " + word).strip()
        if text_size(draw, trial, f)[0] <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def caption(img, lines, t, y, size=62, f=None, fill=LIGHT, accent=None, delay=0.0, stagger=0.18, center=True):
    """Несколько строк по центру/слева, появляются по очереди (fade + подъём 16 px)."""
    d = ImageDraw.Draw(img)
    f = f or serif(size)
    for i, line in enumerate(lines):
        a = ease_out((t - delay - i * stagger) / 0.55)
        words = line.split(" ")
        total_w = text_size(d, line, f)[0]
        x = (W - total_w) / 2 if center else 96
        cx = x
        for w_ in words:
            piece = w_ + " "
            col = GOLD if (accent and w_.strip(".,—«»?!") in accent) else fill
            draw_text(img, (cx, y + i * int(size * 1.28)), piece, f, col, a, rise=16 * (1 - a))
            cx += text_size(d, piece, f)[0] if piece.strip() else 0


def hline(img, x0, x1, y, color=GOLD, alpha=0.8, width=2):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).line([(x0, y), (x1, y)], fill=with_alpha(color, alpha), width=width)
    img.paste(layer, (0, 0), layer)


def rect(img, box, fill, alpha=1.0, outline=None, radius=0, width=2):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(box, radius=radius, fill=with_alpha(fill, alpha) if fill else None,
                        outline=with_alpha(outline, alpha) if outline else None, width=width)
    img.paste(layer, (0, 0), layer)


def marker(img, t, num, title):
    """Метка «01 / Типографика» вверху: Georgia + тонкая линия."""
    a = ease_out(t / 0.5)
    draw_text(img, (96, 150), num, text_font(34), GOLD, a, rise=12 * (1 - a))
    draw_text(img, (170, 150), title.upper(), text_font(30), STONE, a, rise=12 * (1 - a))
    hline(img, 96, 96 + int(888 * ease_out(t / 0.9)), 210, LIGHT, 0.22 * a, 1)


# ----------------------------------------------------------------------------- mock "sites"


def cheap_site(img, box, t=0.0):
    """Шаблонный сайт «за $300»: яркие блоки, три шрифта, плотная вёрстка (абстракция, без реального бренда)."""
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(img)
    d.rectangle(box, fill=(236, 240, 247))
    d.rectangle((x0, y0, x1, y0 + 90), fill=(30, 100, 220))
    d.text((x0 + 24, y0 + 22), "MyCompany™", font=font(ARIAL_BD, 34), fill=(255, 255, 255))
    d.text((x0 + 24, y0 + 120), "WELCOME!!!", font=font(ARIAL_BD, 72), fill=(220, 50, 50))
    d.text((x0 + 24, y0 + 215), "Best services for you", font=font(Path("C:/Windows/Fonts/ariali.ttf"), 38), fill=(40, 40, 40))
    d.text((x0 + 24, y0 + 275), "Лучшие услуги • Низкие цены", font=font(Path("C:/Windows/Fonts/comic.ttf") if Path("C:/Windows/Fonts/comic.ttf").exists() else GEORGIA, 34), fill=(20, 140, 60))
    bx = [(x0 + 24, y0 + 360, x0 + 240, y0 + 440, (255, 140, 0)), (x0 + 256, y0 + 360, x0 + 470, y0 + 440, (220, 40, 160)),
          (x0 + 24, y0 + 460, x0 + 240, y0 + 540, (40, 180, 70)), (x0 + 256, y0 + 460, x0 + 470, y0 + 540, (30, 100, 220))]
    for (a, b, c, e, col) in bx:
        d.rectangle((a, b, c, e), fill=col)
    for i in range(6):
        d.rectangle((x0 + 24, y0 + 570 + i * 46, x1 - 24, y0 + 596 + i * 46), fill=(200, 205, 215))


def lux_site(img, box, grid_t=1.0, reveal=1.0):
    """Тот же контент в Quiet Luxury: тёмный фон, воздух, тонкая сетка, засечный заголовок."""
    x0, y0, x1, y1 = box
    rect(img, box, SURFACE, 1.0)
    cols = 6
    cw = (x1 - x0) / cols
    for i in range(cols + 1):
        hline(img, int(x0 + i * cw), int(x0 + i * cw), y0, LIGHT, 0)  # заглушка для единообразия
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).line([(x0 + i * cw, y0), (x0 + i * cw, y0 + (y1 - y0) * ease_out(grid_t))],
                                   fill=with_alpha(GOLD, 0.16), width=1)
        img.paste(layer, (0, 0), layer)
    a = ease_out(reveal)
    draw_text(img, (x0 + cw * 0.5, y0 + 110), "R Design Space", text_font(28), STONE, a, rise=14 * (1 - a))
    draw_text(img, (x0 + cw * 0.5, y0 + 260), "Interfaces", serif(104, 400), LIGHT, a, rise=20 * (1 - a))
    draw_text(img, (x0 + cw * 0.5, y0 + 385), "that feel", serif_it(104, 400), GOLD, ease_out(reveal - 0.1), rise=20 * (1 - a))
    draw_text(img, (x0 + cw * 0.5, y0 + 510), "expensive.", serif(104, 400), LIGHT, ease_out(reveal - 0.2), rise=20 * (1 - a))
    hline(img, int(x0 + cw * 0.5), int(x0 + cw * 0.5 + 240 * ease_out(reveal)), y0 + 690, GOLD, 0.8 * a, 2)
    al = ease_out(reveal - 0.3)
    draw_text(img, (x0 + cw * 0.5, y0 + 720), "Начать проект", text_font(32), LIGHT, al)
    ax = x0 + cw * 0.5 + 250
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).line([(ax, y0 + 738), (ax + 44, y0 + 738), (ax + 34, y0 + 729), (ax + 44, y0 + 738), (ax + 34, y0 + 747)], fill=with_alpha(GOLD, al), width=2)
    img.paste(layer, (0, 0), layer)


# ----------------------------------------------------------------------------- scenes


@dataclass
class Scene:
    key: str
    nominal: float  # плановая длительность, с
    fn: object
    duration: float = 0.0  # итоговая, с
    audio: Path | None = None
    start: float = 0.0
    text: str = ""  # закадровый текст сцены
    bgfn: object = None  # (t, dur) -> фон сцены (параллакс); None = общий фон


def scene_hook(img, t, dur):
    # 0..0.5 слева «дешёвый» сайт, справа «дорогой»; к 1.4 с правая половина расширяется
    split = W // 2
    wipe = ease_io((t - 1.2) / 1.2)
    cut = int(split + (W - split) * wipe)
    box_l = (60, 340, W - 60, 1380)
    cheap_site(img, box_l)
    lux = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    lux_site(lux, (60, 340, W - 60, 1380), grid_t=ease_out(t / 1.2), reveal=ease_out((t - 0.2) / 1.2))
    # левая часть затемняется по мере расширения правой
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rectangle((int(split - (split * wipe)), 0, W, H), fill=255)
    img.paste(Image.alpha_composite(img.convert("RGBA"), lux).convert("RGB"), (0, 0), mask)
    # линия-разделитель
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    lx = int(split - split * wipe)
    ImageDraw.Draw(layer).line([(lx, 0), (lx, H)], fill=with_alpha(GOLD, 0.9 * (1 - wipe * 0.6)), width=3)
    img.paste(layer, (0, 0), layer)
    # цены
    a = ease_out(t / 0.5)
    pf = serif(120, 500)
    d = ImageDraw.Draw(img)
    draw_text(img, (split // 2, 210), "$300", pf, (250, 250, 250), a * clamp(1 - wipe * 2.5), anchor="mm", rise=18 * (1 - a))
    draw_text(img, (int(split + split // 2 + (W // 2 - split - split // 2) * wipe), 210), "$3 000", pf, GOLD, a, anchor="mm", rise=18 * (1 - a))
    # хук
    shade = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shade).rectangle((0, 1400, W, H), fill=(14, 14, 15, 215))
    img.paste(shade, (0, 0), shade)
    caption(img, ["Выглядит на", "$300. Стоил $3 000."], t, 1500, size=74, f=serif(74, 500), accent={"$300."}, delay=1.0)


def scene_type(img, t, dur):
    sw = ease_io((t - 1.0) / 0.7)  # переключение A -> B
    a_alpha = 1 - sw
    if a_alpha > 0:
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.rectangle((96, 400, W - 96, 1050), fill=(236, 240, 247, 255))
        ld.text((130, 450), "WELCOME!!!", font=font(ARIAL_BD, 96), fill=(220, 50, 50, 255))
        ld.text((130, 590), "Best services", font=font(Path("C:/Windows/Fonts/ariali.ttf"), 70), fill=(30, 100, 220, 255))
        ld.text((130, 700), "Лучшие услуги", font=font(GEORGIA, 64), fill=(20, 140, 60, 255))
        for box in [(120, 440, 880, 560), (120, 575, 760, 680), (120, 690, 700, 780)]:
            ld.rectangle(box, outline=(230, 40, 40, 255), width=4)
        layer.putalpha(layer.getchannel("A").point(lambda v: int(v * a_alpha)))
        img.paste(layer, (0, 0), layer)
    if sw > 0:
        a = ease_out(sw)
        rect(img, (96, 400, W - 96, 1050), SURFACE, a)
        draw_text(img, (150, 470), "Quiet", serif(150, 400), LIGHT, a, rise=20 * (1 - a))
        draw_text(img, (150, 650), "luxury", serif_it(150, 400), GOLD, ease_out(sw - 0.15), rise=20 * (1 - a))
        draw_text(img, (150, 880), "Один засечный. Один гротеск.", text_font(36), STONE, ease_out(sw - 0.3))
        hline(img, 150, 150 + int(300 * ease_out(sw - 0.2)), 850, GOLD, 0.8, 2)
    marker(img, t, "01", "Типографика")
    caption(img, ["Не больше", "двух шрифтов"], t, 1230, size=80, f=serif(80, 500), accent={"двух"}, delay=0.5)
    zoom_a = ease_out((t - 1.7) / 0.6)
    draw_text(img, (W / 2, 1650), "Playfair Display  +  Georgia", text_font(34), STONE, zoom_a, anchor="mm", rise=12 * (1 - zoom_a))


def scene_space(img, t, dur):
    marker(img, t, "02", "Воздух и сетка")
    cols = 12
    gx0, gx1 = 96, W - 96
    cw = (gx1 - gx0) / cols
    g = ease_out(t / 1.3)
    for i in range(cols + 1):
        x = gx0 + i * cw
        yy = 300 + int(1000 * clamp(g * 1.25 - i * 0.03))
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).line([(x, 300), (x, yy)], fill=with_alpha(GOLD, 0.22), width=1)
        img.paste(layer, (0, 0), layer)
    # элементы встают по сетке (асимметрия: заголовок смещён на 2 колонки)
    a1 = ease_out((t - 0.6) / 0.8)
    a2 = ease_out((t - 0.9) / 0.8)
    a3 = ease_out((t - 1.2) / 0.8)
    x_h = gx0 + cw * 2
    draw_text(img, (x_h, 560), "Тишина", serif(130, 400), LIGHT, a1, rise=24 * (1 - a1))
    draw_text(img, (x_h, 720), "в интерфейсе", serif_it(94, 400), GOLD, a2, rise=24 * (1 - a2))
    hline(img, int(x_h), int(x_h + cw * 3 * ease_out((t - 1.0) / 0.8)), 880, LIGHT, 0.6, 1)
    draw_text(img, (gx0 + cw * 7, 940), "поля ≥ 8 vw\n12 колонок", text_font(32), STONE, a3, rise=14 * (1 - a3))
    caption(img, ["Пустота — это", "не потери"], t, 1380, size=76, f=serif(76, 500), accent={"не", "потери"}, delay=1.1)


def scene_motion(img, t, dur):
    marker(img, t, "03", "Движение")
    sw = ease_io((t - 0.4) / 0.5)
    # A: резкий прыжок кнопки; B: линия растёт под ссылкой за 0.6 с
    rect(img, (96, 400, W - 96, 1020), SURFACE, 1.0)
    jump = int(40 * abs(math.sin(t * 14)) * (1 - sw))
    bcol = mix((255, 140, 0), (30, 100, 220), 0 if int(t * 10) % 2 == 0 else 1)
    if sw < 1:
        rect(img, (150, 560 - jump, 650, 680 - jump), bcol, 1 - sw, radius=8)
        draw_text(img, (400, 620 - jump), "КУПИТЬ СЕЙЧАС", font(ARIAL_BD, 40), (255, 255, 255), 1 - sw, anchor="mm")
    if sw > 0:
        a = sw
        draw_text(img, (150, 540), "Обсудить проект", serif(86, 400), LIGHT, a, rise=16 * (1 - a))
        tt = clamp((t - 1.0) / 0.6)  # ровно 0.6 с
        xw = text_size(ImageDraw.Draw(img), "Обсудить проект", serif(86, 400))[0]
        hline(img, 150, 150 + int(xw * ease_out(tt)), 660, GOLD, 0.95, 3)
        draw_text(img, (150, 740), "0.6 с  ·  cubic-bezier(0.22, 1, 0.36, 1)", text_font(30), STONE, ease_out((t - 1.2) / 0.5))
    caption(img, ["Не быстро. Не медленно.", "Ровно дорого."], t, 1230, size=70, f=serif(70, 500), accent={"дорого."}, delay=1.3)


def scene_cta(img, t, dur):
    a = ease_out(t / 0.6)
    draw_text(img, (W / 2, 330), "R", serif(150, 400), GOLD, a, anchor="mm", rise=16 * (1 - a))
    hline(img, W // 2 - int(110 * ease_out(t / 0.9)), W // 2 + int(110 * ease_out(t / 0.9)), 430, LIGHT, 0.5, 1)
    caption(img, ["Аудит вашего сайта", "или новый проект?"], t, 560, size=84, f=serif(84, 500), accent={"проект?"}, delay=0.3)
    # «карточка» бота
    ca = ease_out((t - 1.6) / 0.7)
    rect(img, (140, 1080, W - 140, 1340), SURFACE, ca, outline=GOLD, radius=36, width=2)
    draw_text(img, (W / 2, 1160), "Telegram-бот в шапке профиля", text_font(40), STONE, ca, anchor="mm", rise=14 * (1 - ca))
    draw_text(img, (W / 2, 1255), "@r_design_space_bot", serif(66, 500), GOLD, ca, anchor="mm", rise=14 * (1 - ca))
    draw_text(img, (W / 2, 1450), "оценит задачу и бюджет сразу", text_font(38), LIGHT, ease_out((t - 2.2) / 0.7), anchor="mm")
    # стрелка вверх к шапке профиля
    pulse = 0.5 + 0.5 * math.sin(t * 4)
    ar = ease_out((t - 2.6) / 0.6)
    ay = 1640 - int(18 * pulse)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).line([(W / 2, ay + 60), (W / 2, ay - 60), (W / 2 - 28, ay - 32), (W / 2, ay - 60), (W / 2 + 28, ay - 32)],
                               fill=with_alpha(GOLD, ar), width=4, joint="curve")
    img.paste(layer, (0, 0), layer)


SCENES_DEF = [
    ("hook", 3.8, scene_hook),
    ("type", 4.0, scene_type),
    ("space", 4.0, scene_space),
    ("motion", 4.0, scene_motion),
    ("cta", 5.5, scene_cta),
]


# ----------------------------------------------------------------------------- voiceover


def read_voiceover(path: Path) -> dict[str, str]:
    src = path.read_text(encoding="utf-8")
    m = re.search(r"```voiceover\s*\n(.*?)```", src, re.S)
    if not m:
        raise SystemExit("В script.md нет блока ```voiceover```")
    out = {}
    for line in m.group(1).strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def load_openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    envf = ROOT / "agent" / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def tts_openai(text: str, out: Path, voice: str, key: str) -> None:
    import requests

    r = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": "tts-1-hd", "voice": voice, "input": text, "response_format": "mp3", "speed": 0.95},
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI TTS HTTP {r.status_code}")
    out.write_bytes(r.content)


def tts_edge(text: str, out: Path, voice: str) -> None:
    import edge_tts

    async def run():
        male = any(n in voice for n in ("Dmitry", "Guy", "Alvaro"))  # мужским голосам чуть ниже тон, женским - без сдвига
        await edge_tts.Communicate(text, voice, pitch="-4Hz" if male else "+0Hz").save(str(out))

    last = None
    for _ in range(4):  # edge-tts иногда отвечает NoAudioReceived: повторяем
        try:
            asyncio.run(run())
            if out.exists() and out.stat().st_size > 1000:
                return
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"edge-tts: {last}")


def tts_sapi(text: str, out: Path, voice: str) -> None:
    wav = out.with_suffix(".wav")
    ps = (
        "Add-Type -AssemblyName System.Speech;$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v=$s.GetInstalledVoices()|Where-Object{$_.VoiceInfo.Culture.Name -like 'ru*'}|Select-Object -First 1;"
        "if($v){$s.SelectVoice($v.VoiceInfo.Name)};$s.Rate=-1;"
        f"$s.SetOutputToWaveFile('{wav}');$s.Speak([Console]::In.ReadToEnd());$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], input=text.encode("utf-8"), check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), str(out)], check=True)


def synth_one(text: str, eng: str, key: str, voice: str, lang: str = "ru") -> Path:
    edge_default = {"en": "en-US-GuyNeural", "es": "es-ES-ElviraNeural"}.get(lang, "ru-RU-DmitryNeural")
    v = voice or {"openai": "onyx", "edge": edge_default, "sapi": ""}[eng]
    tag = hashlib.sha1(f"{eng}|{v}|{text}".encode()).hexdigest()[:12]
    out = BUILD / "audio" / f"{tag}.mp3"
    if out.exists() and out.stat().st_size > 1000:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        if eng == "openai":
            if not key:
                raise RuntimeError("нет OPENAI_API_KEY")
            tts_openai(text, out, v, key)
        elif eng == "edge":
            tts_edge(text, out, v)
        else:
            tts_sapi(text, out, v)
        if not (out.exists() and out.stat().st_size > 1000):
            raise RuntimeError("пустой файл")
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return out


def synthesize_all(texts: dict[str, str], key: str, engine: str, voice: str, lang: str = "ru") -> tuple[dict[str, Path], str]:
    """Один и тот же движок для ВСЕХ сцен (иначе голос меняется посреди ролика). Кеш по тексту, движку и голосу."""
    order = {"auto": ["openai", "edge", "sapi"], "openai": ["openai"], "edge": ["edge"], "sapi": ["sapi"]}[engine]
    errors = []
    for eng in order:
        try:
            return {k: synth_one(t, eng, key, voice, lang) for k, t in texts.items()}, eng
        except Exception as e:  # noqa: BLE001
            errors.append(f"{eng}: {e}")
    raise SystemExit("Озвучка не получилась: " + "; ".join(errors))


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


# ----------------------------------------------------------------------------- render


def render_frame(bg: Image.Image, scene: Scene, t: float, frame_no: int) -> np.ndarray:
    img = scene.bgfn(t, scene.duration).copy() if scene.bgfn else bg.copy()
    scene.fn(img, t, scene.duration)
    arr = np.asarray(img, dtype=np.float32)
    # лёгкое зерно и fade in/out сцены (dissolve 0.25 с)
    arr = arr + GRAIN[frame_no % len(GRAIN)]
    fade = min(clamp(t / 0.25), clamp((scene.duration - t) / 0.25)) if scene.key != "hook" else clamp((scene.duration - t) / 0.25)
    arr = arr * fade
    # медленный zoom 100 -> 104 % на сцену
    return np.clip(arr, 0, 255).astype(np.uint8)


def zoom_crop(frame: np.ndarray, k: float) -> np.ndarray:
    if k <= 0.0001:
        return frame
    img = Image.fromarray(frame)
    nw, nh = int(W * (1 + k)), int(H * (1 + k))
    img = img.resize((nw, nh), Image.BILINEAR)
    left, top = (nw - W) // 2, (nh - H) // 2
    return np.asarray(img.crop((left, top, left + W, top + H)))


# ----------------------------------------------------------------------------- generic template for JSON reels


def fit_lines(text: str, sizes=(124, 108, 94, 82, 70), max_w=W - 192, max_lines=5):
    d = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    for size in sizes:
        f = serif(size, 500)
        lines = wrap(d, text, f, max_w)
        if len(lines) <= max_lines:
            return lines, size
    return wrap(d, text, serif(sizes[-1], 500), max_w), sizes[-1]


def last_word(lines: list[str]) -> set[str]:
    return {lines[-1].split(" ")[-1].strip(".,—«»?!")} if lines else set()


def faint_grid(img, t, cols=6, alpha=0.14, top=260, height=1300):
    g = ease_out(t / 1.2)
    cw = (W - 192) / cols
    for i in range(cols + 1):
        x = 96 + i * cw
        yy = top + int(height * clamp(g * 1.2 - i * 0.04))
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).line([(x, top), (x, yy)], fill=with_alpha(GOLD, alpha), width=1)
        img.paste(layer, (0, 0), layer)


def fit_one(text: str, size: int, max_w: int = W - 160):
    """Один сегмент в одну строку: уменьшаем кегль, пока не влезет в max_w."""
    d = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    while size > 30 and text_size(d, text, serif(size, 500))[0] > max_w:
        size = int(size * 0.9)
    return serif(size, 500)


def j_hook(img, t, dur, hook):
    faint_grid(img, t)
    a = ease_out(t / 0.5)
    draw_text(img, (W / 2, 190), "R DESIGN SPACE", text_font(30), STONE, a, anchor="mm", rise=10 * (1 - a))
    hline(img, W // 2 - int(90 * ease_out(t / 0.9)), W // 2 + int(90 * ease_out(t / 0.9)), 240, GOLD, 0.7, 1)
    parts = re.split(r"\s+vs\.?\s+", hook, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        a1 = ease_out((t - 0.2) / 0.6)
        a2 = ease_out((t - 0.9) / 0.7)
        ax = ease_out((t - 0.6) / 0.5)
        draw_text(img, (W / 2, 720), parts[0], fit_one(parts[0], 150), STONE, a1, anchor="mm", rise=22 * (1 - a1))
        draw_text(img, (W / 2, 930), "vs", serif_it(70, 400), STONE, ax, anchor="mm")
        w = int(160 * ease_out((t - 0.7) / 0.6))
        hline(img, W // 2 - w, W // 2 + w, 1010, GOLD, 0.6, 1)
        draw_text(img, (W / 2, 1180), parts[1], fit_one(parts[1], 190), GOLD, a2, anchor="mm", rise=22 * (1 - a2))
    else:
        lines, size = fit_lines(hook)
        caption(img, lines, t, 760, size=size, f=serif(size, 500), accent=last_word(lines), delay=0.2)


def j_point(img, t, dur, text, idx):
    faint_grid(img, t)
    marker(img, t, f"{idx:02d}", "R Design Space")
    lines, size = fit_lines(text)
    y = 760 - int((len(lines) - 1) * size * 0.64 / 2)
    caption(img, lines, t, y, size=size, f=serif(size, 500), accent=last_word(lines), delay=0.3, center=False)
    yy = y + int(len(lines) * size * 1.28) + 50
    hline(img, 96, 96 + int(300 * ease_out((t - 0.9) / 0.8)), yy, GOLD, 0.85, 3)


def j_cta(img, t, dur, text, handle, ru):
    a = ease_out(t / 0.6)
    draw_text(img, (W / 2, 330), "R", serif(150, 400), GOLD, a, anchor="mm", rise=16 * (1 - a))
    hline(img, W // 2 - int(110 * ease_out(t / 0.9)), W // 2 + int(110 * ease_out(t / 0.9)), 430, LIGHT, 0.5, 1)
    parts = [p.strip() for p in re.split(r"\s*[·|]\s*", text) if p.strip()] or [text]
    lines = []
    for part in parts:
        lines += fit_lines(part, sizes=(92, 80, 70), max_lines=2)[0]
    caption(img, lines, t, 540, size=84, f=serif(84, 500), accent=last_word(lines), delay=0.3)
    ca = ease_out((t - 1.4) / 0.7)
    rect(img, (140, 1080, W - 140, 1340), SURFACE, ca, outline=GOLD, radius=36, width=2)
    draw_text(img, (W / 2, 1160), "Telegram-бот в шапке профиля" if ru else "Telegram bot · link in bio",
              text_font(40), STONE, ca, anchor="mm", rise=14 * (1 - ca))
    draw_text(img, (W / 2, 1255), handle, serif(66, 500), GOLD, ca, anchor="mm", rise=14 * (1 - ca))
    pulse = 0.5 + 0.5 * math.sin(t * 4)
    ar = ease_out((t - 2.2) / 0.6)
    ay = 1620 - int(18 * pulse)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).line([(W / 2, ay + 60), (W / 2, ay - 60), (W / 2 - 28, ay - 32), (W / 2, ay - 60), (W / 2 + 28, ay - 32)],
                               fill=with_alpha(GOLD, ar), width=4, joint="curve")
    img.paste(layer, (0, 0), layer)


CTA_HINT = re.compile(r"telegram|\bbot\b|link in bio|in bio|бот|шапк|ссылк", re.I)


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"([.!?…])(?=[A-ZА-Я])", r" ", text)  # «help.Keep» -> «help. Keep»
    return [x.strip() for x in re.split(r"(?<=[.!?…])\s+", text.strip()) if x.strip()]


def distribute(sentences: list[str], k: int) -> list[str]:
    """Режет предложения на k подряд идущих групп так, чтобы самая длинная группа была как можно короче."""
    from itertools import combinations

    if k <= 0:
        return []
    n = len(sentences)
    if n <= k:
        return sentences + [""] * (k - n)
    best, best_cost = [], 10**9
    for cuts in combinations(range(1, n), k - 1):
        bounds = (0, *cuts, n)
        groups = [" ".join(sentences[bounds[i]:bounds[i + 1]]) for i in range(k)]
        cost = max(len(g) for g in groups)
        if cost < best_cost:
            best, best_cost = groups, cost
    return best


def spec_from_json(item: dict) -> dict:
    rid = str(item.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", rid):
        raise SystemExit(f"Некорректный id ролика: {rid!r}")
    voice = str(item.get("voiceover_text") or "").strip()
    lines = [str(x).strip() for x in (item.get("screen_text") or []) if str(x).strip()]
    hook = str(item.get("hook_screen") or (lines[0] if lines else "")).strip()
    cta = str(item.get("cta") or (lines[-1] if len(lines) > 1 else "")).strip()
    if not voice or not hook:
        raise SystemExit(f"{rid}: нужны hook_screen (или screen_text) и voiceover_text")
    middle = lines[1:-1] if len(lines) >= 3 else []
    sents = split_sentences(voice)
    cut = next((i for i, x in enumerate(sents) if i >= 1 and CTA_HINT.search(x)), None)
    cta_sents = sents[cut:] if cut is not None else []
    body = sents[:cut] if cut is not None else sents
    groups = distribute(body, 1 + len(middle))

    m = re.search(r"(\d+)", rid)
    num = int(m.group(1)) if m else 0
    lang = "ru" if re.search(r"[А-Яа-я]", voice) else "en"
    ru = lang == "ru"
    handle = "@r_design_space_bot"
    scenes = [Scene("hook", 3.2, lambda img, t, d, h=hook: j_hook(img, t, d, h), text=groups[0])]
    for i, line in enumerate(middle):
        scenes.append(Scene(f"p{i + 1}", 2.8, lambda img, t, d, x=line, n=i + 1: j_point(img, t, d, x, n), text=groups[i + 1]))
    scenes.append(Scene("cta", 4.0, lambda img, t, d, x=cta or "Free audit", hd=handle, r=ru: j_cta(img, t, d, x, hd, r), text=" ".join(cta_sents)))
    links = {"instagram": f"https://t.me/r_design_space_bot?start=ig_reel_{num}",
             "tiktok": f"https://t.me/r_design_space_bot?start=tt_reel_{num}"} if num else {}
    return {"id": rid, "title": item.get("title", rid), "scenes": scenes, "lang": lang, "deeplinks": links}


# ----------------------------------------------------------------------------- batch format: 4 layouts (HERO / BENTO / METRIC / QUOTE)
# Палитра и тема приходят из visual_style; сцены с «fondo marfil» рисуются на светлом фоне.

SANS = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
SANS_BD = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeuib.ttf"
SANS_FALLBACK = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"
SAFE_X = 96  # поля области безопасности
HANDLE = "@r_design_space_bot"


def sans_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = SANS_BD if bold and SANS_BD.exists() else SANS if SANS.exists() else SANS_FALLBACK
    return font(path, size)


def hex_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def make_palettes(vs: dict) -> dict:
    pal = (vs or {}).get("palette", {})
    bg = hex_rgb(pal.get("background", "#141414"))
    surf = hex_rgb(pal.get("surface", "#232323"))
    tx = hex_rgb(pal.get("text", "#F2EFE8"))
    ac = hex_rgb(pal.get("accent", "#B89B67"))
    dark = {"bg": bg, "surf": surf, "text": tx, "acc": ac, "stone": mix(tx, bg, 0.42), "light": False}
    ivory = {"bg": tx, "surf": mix(tx, (0, 0, 0), 0.06), "text": bg, "acc": mix(ac, (0, 0, 0), 0.2), "stone": mix(bg, tx, 0.45), "light": True}
    return {"dark": dark, "ivory": ivory}


def put_text(img, xy, s, f, fill, alpha=1.0, anchor="la", rise=0.0):
    """Текст через маленький слой (быстрее draw_text): прозрачность + подъём rise px."""
    if alpha <= 0.01 or not s:
        return
    x, y = xy[0], xy[1] + rise
    l, t_, r, b = ImageDraw.Draw(img).textbbox((x, y), s, font=f, anchor=anchor)
    box = (int(l) - 6, int(t_) - 6, int(r) + 6, int(b) + 6)
    layer = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((x - box[0], y - box[1]), s, font=f, fill=with_alpha(fill, alpha), anchor=anchor)
    img.paste(layer, (box[0], box[1]), layer)


def spaced(img, cx, y, s, f, color, a, track=7):
    d = ImageDraw.Draw(img)
    ws = [d.textlength(ch, font=f) for ch in s]
    x = cx - (sum(ws) + track * (len(s) - 1)) / 2
    for ch, w in zip(s, ws):
        put_text(img, (x, y), ch, f, color, a, anchor="lm")
        x += w + track


def tag(img, t, P, y=170, cx=W // 2):
    a = ease_out(t / 0.7)
    spaced(img, cx, y, "R DESIGN SPACE", sans_font(25), P["stone"], a)


def block(img, lines, f, x, y, lh, color, t, delay=0.0, stagger=0.18, align="left", acc=None, acc_words=(), ul_words=()):
    """Строки с поочерёдным проявлением (fade + подъём); align=center: x — центр; акцентные слова и подчёркивание."""
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        a = ease_out((t - delay - i * stagger) / 0.65)
        if a <= 0:
            continue
        cx = x - d.textlength(line, font=f) / 2 if align == "center" else x
        for w_ in line.split(" "):
            core = w_.strip(".,—«»?!¿¡:;“”")
            col = acc if (acc is not None and core in acc_words) else color
            put_text(img, (cx, y + i * lh), w_, f, col, a, rise=18 * (1 - a))
            if core in ul_words:
                ww = d.textlength(w_, font=f)
                hline(img, int(cx), int(cx + ww * ease_out((t - delay - 0.5) / 0.7)), int(y + i * lh + f.size * 1.14), acc or color, 0.95 * a, 3)
            cx += d.textlength(w_ + " ", font=f)


def pill(img, cx, y, label, P, t, delay=0.0, w_pad=64, h=104, size=44, filled=None):
    a = ease_out((t - delay) / 0.6)
    if a <= 0:
        return
    f = sans_font(size, True)
    w = ImageDraw.Draw(img).textlength(label, font=f) + 2 * w_pad
    box = (int(cx - w / 2), int(y), int(cx + w / 2), int(y + h))
    filled = P["light"] if filled is None else filled  # на светлом фоне CTA — тёмная кнопка
    if filled:
        rect(img, box, P["text"], a, radius=h // 2)
        put_text(img, (cx, y + h / 2), label, f, P["bg"], a, anchor="mm")
    else:
        rect(img, box, None, a, outline=P["acc"], radius=h // 2, width=3)
        put_text(img, (cx, y + h / 2), label, f, P["acc"], a, anchor="mm")


def sec_block(img, sec, t, y, P, delay=1.0, align="center", x=None, big_handle=False, size=40, maxw=None):
    """Вторичный текст: строки sans, строка с @handle — кнопка (или крупный sans). Возвращает нижнюю границу."""
    if not sec:
        return y
    x = (W // 2 if align == "center" else SAFE_X) if x is None else x
    maxw = maxw or (W - 2 * SAFE_X - 40)
    d = ImageDraw.Draw(img)
    f = sans_font(size)
    for line in sec.split("\n"):
        line = line.strip()
        if not line:
            continue
        if HANDLE in line:
            if big_handle:
                a = ease_out((t - delay) / 0.6)
                put_text(img, (x, y + 20), line, sans_font(58, True), P["acc"], a, anchor="lm" if align == "left" else "mm", rise=14 * (1 - a))
                y += 110
            else:
                label = line if line.startswith("@") else HANDLE
                psize, pad = 44, 64
                while psize > 26 and d.textlength(label, font=sans_font(psize, True)) + 2 * pad > maxw:
                    psize -= 2
                    pad = max(28, pad - 3)
                lw = d.textlength(label, font=sans_font(psize, True)) + 2 * pad
                pill(img, x if align == "center" else x + lw / 2, y, label, P, t, delay, w_pad=pad, size=psize)
                y += 140
            delay += 0.25
            continue
        for sub in wrap(d, line, f, maxw):
            a = ease_out((t - delay) / 0.6)
            put_text(img, (x, y), sub, f, P["stone"], a, anchor="ma" if align == "center" else "la", rise=12 * (1 - a))
            y += int(size * 1.5)
        delay += 0.2
    return y


def emph_words(vd: str) -> tuple:
    m = re.search(r"bajo la palabra (\w+)", vd, re.I)
    return (m.group(1),) if m else ()


# ---- LAYOUT_HERO: центрированный заголовок + золотая линия


def L_hero(img, t, dur, sc, P):
    tag(img, t, P)
    vd = sc["vd"].lower()
    lines, size = fit_lines(sc["text"], sizes=(128, 112, 98, 86, 76), max_w=W - 2 * SAFE_X - 40, max_lines=4)
    lh = int(size * 1.22)
    h = len(lines) * lh
    cy = 760 if sc["sec"] else 860
    top = cy - h // 2
    ul = emph_words(sc["vd"])
    acc_words = set(ul) or {lines[-1].split(" ")[-1].strip(".,—«»?!¿¡")}
    block(img, lines, serif(size, 500), W // 2, top, lh, P["text"], t, delay=0.25, align="center", acc=P["acc"], acc_words=acc_words, ul_words=set(ul))
    ly = top + h + 56
    half = 70 if "corta" in vd else 150
    w = int(half * ease_out((t - 0.8) / (1.8 if "lentamente" in vd else 0.9)))
    hline(img, W // 2 - w, W // 2 + w, ly, P["acc"], 0.95, 3)
    sec_block(img, sc["sec"], t, ly + 90, P, delay=1.1)


# ---- LAYOUT_QUOTE: асимметричная цитата, золотые кавычки, широкое негативное пространство


def L_quote(img, t, dur, sc, P):
    tag(img, t, P)
    vd = sc["vd"].lower()
    top_aligned = "arriba" in vd or "mitad superior" in vd
    qsize = 440 if "gran tamaño" in vd else 200 if "pequeñas" in vd else 300
    lines, size = fit_lines(sc["text"], sizes=(92, 82, 74, 66), max_w=700, max_lines=6)
    lh = int(size * 1.26)
    h = len(lines) * lh
    top = 520 if top_aligned else 860 - h // 2
    qa = ease_out(t / 0.8)
    put_text(img, (SAFE_X - 6, top - qsize * 0.62), "\u201c", serif(qsize, 500), P["acc"], qa, rise=24 * (1 - qa))
    x = SAFE_X + 36
    vh = int(h * ease_out((t - 0.2) / 1.0))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).line([(SAFE_X, top + 6), (SAFE_X, top + 6 + vh)], fill=with_alpha(P["acc"], 0.55), width=2)
    img.paste(layer, (0, 0), layer)
    block(img, lines, serif(size, 400), x, top, lh, P["text"], t, delay=0.35, acc=P["acc"])
    if sc["sec"]:
        sec_block(img, sc["sec"], t, top + h + (170 if top_aligned else 150), P, delay=1.3, align="left", big_handle="sans serif grande" in vd)


# ---- LAYOUT_METRIC: доминантная цифра + подзаголовок


def parse_metric(text: str):
    m = re.match(r"\s*(\d+)\s+(.*)", text)
    return (m.group(1), m.group(2).upper()) if m else (text, "")


def L_metric(img, t, dur, sc, P):
    tag(img, t, P)
    vd = sc["vd"].lower()
    num, word = parse_metric(sc["text"])
    na = ease_out(t / 0.8)
    big = serif(620, 400)
    d = ImageDraw.Draw(img)
    items = [s.strip() for s in re.split(r"\s+·\s+", sc["sec"])] if "·" in sc["sec"] else \
            [s.strip() for s in re.split(r"(?<=\.)\s+", sc["sec"]) if s.strip()] if "una por una" in vd else [sc["sec"]]
    if "columna" in vd:  # цифра справа, пункты колонкой слева
        put_text(img, (W - SAFE_X, 820), num, big, P["acc"], na, anchor="rm", rise=30 * (1 - na))
        put_text(img, (W - SAFE_X - 10, 1300), word, sans_font(40, True), P["stone"], ease_out((t - 0.5) / 0.6), anchor="rm")
        for i, it in enumerate(items):
            a = ease_out((t - 0.9 - i * 0.55) / 0.6)
            hline(img, SAFE_X, SAFE_X + int(70 * a), 640 + i * 190, P["acc"], 0.9 * a, 3)
            put_text(img, (SAFE_X, 668 + i * 190), it, serif(64, 500), P["text"], a, rise=14 * (1 - a))
    elif "izquierda" in vd:  # цифра слева, вопрос справа
        put_text(img, (SAFE_X - 20, 830), num, big, P["acc"], na, anchor="lm", rise=30 * (1 - na))
        put_text(img, (SAFE_X, 1220), word, sans_font(46, True), P["text"], ease_out((t - 0.5) / 0.6))
        lines, size = fit_lines(sc["sec"], sizes=(70, 62, 54), max_w=330, max_lines=5)
        block(img, lines, serif(size, 400), 640, 640, int(size * 1.3), P["stone"], t, delay=0.9)
    elif "teléfono" in vd:  # цифра рядом с контуром телефона
        put_text(img, (SAFE_X - 20, 800), num, big, P["acc"], na, anchor="lm", rise=30 * (1 - na))
        put_text(img, (SAFE_X, 1190), word, sans_font(46, True), P["text"], ease_out((t - 0.5) / 0.6))
        pa = ease_out((t - 0.3) / 0.8)
        x0, y0, pw, ph = W - SAFE_X - 300, 470, 300, 620
        rect(img, (x0, y0, x0 + pw, y0 + ph), None, pa, outline=P["stone"], radius=52, width=2)
        hline(img, x0 + 100, x0 + 200, y0 + 30, P["stone"], 0.6 * pa, 3)
        rect(img, (x0 + 40, y0 + ph - 150, x0 + pw - 40, y0 + ph - 90), P["acc"], 0.9 * pa, radius=30)
        lines, size = fit_lines(sc["sec"], sizes=(60, 54, 48), max_w=W - 2 * SAFE_X, max_lines=3)
        block(img, lines, serif(size, 400), SAFE_X, 1330, int(size * 1.3), P["text"], t, delay=1.0, acc=P["acc"])
    elif "una por una" in vd:  # цифра сверху по центру, ответы по очереди
        put_text(img, (W // 2, 560), num, big, P["acc"], na, anchor="mm", rise=30 * (1 - na))
        put_text(img, (W // 2, 930), word, sans_font(42, True), P["text"], ease_out((t - 0.5) / 0.6), anchor="mm")
        for i, it in enumerate(items):
            a = ease_out((t - 1.0 - i * 0.7) / 0.6)
            hline(img, W // 2 - 40, W // 2 + 40, 1060 + i * 150, P["acc"], 0.8 * a, 2)
            put_text(img, (W // 2, 1100 + i * 150), it, serif(62, 500), P["text"], a, anchor="mm", rise=14 * (1 - a))
    else:  # доминирующая цифра, подпись и мелкие метки
        put_text(img, (W // 2, 720), num, big, P["acc"], na, anchor="mm", rise=30 * (1 - na))
        put_text(img, (W // 2, 1090), word, sans_font(46, True), P["text"], ease_out((t - 0.5) / 0.6), anchor="mm")
        lines, size = fit_lines(sc["sec"], sizes=(66, 58, 50), max_w=W - 2 * SAFE_X, max_lines=2)
        block(img, lines, serif(size, 400), W // 2, 1190, int(size * 1.3), P["stone"], t, delay=0.9, align="center")
        m = re.search(r"etiquetas[^:]*:\s*(.+?)\.", sc["vd"], re.I)
        chips = [c.strip() for c in re.split(r",\s*|\s+y\s+", m.group(1)) if c.strip()] if m else []
        cw = [d.textlength(c.upper(), font=sans_font(26, True)) + 56 for c in chips]
        x = W // 2 - (sum(cw) + 20 * (len(cw) - 1)) / 2 if cw else 0
        for i, (c, w_) in enumerate(zip(chips, cw)):
            a = ease_out((t - 1.4 - i * 0.2) / 0.5)
            rect(img, (int(x), 1420, int(x + w_), 1488), None, a, outline=P["stone"], radius=34, width=2)
            put_text(img, (x + w_ / 2, 1454), c.upper(), sans_font(26, True), P["stone"], a, anchor="mm")
            x += w_ + 20


# ---- LAYOUT_BENTO: UI-карточки (surface) с тонкой золотой обводкой/акцентом


def card(img, box, P, a, gold=False, radius=36):
    rect(img, box, P["surf"], a, radius=radius)
    rect(img, box, None, a * (0.95 if gold else 0.5), outline=P["acc"] if gold else mix(P["text"], P["bg"], 0.82), radius=radius, width=3 if gold else 2)


CARD_PAD = 56  # одинаковый внутренний отступ во всех карточках


def common_tile_size(items, widths, sizes, max_lines=2):
    """Один кегль на все карточки: самый крупный, при котором текст каждой влезает в max_lines без сжатия."""
    d = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    for size in sizes:
        f = serif(size, 500)
        ok = True
        for it, w in zip(items, widths):
            lines = wrap(d, it, f, w - 2 * CARD_PAD)
            if len(lines) > max_lines or any(d.textlength(x, font=f) > w - 2 * CARD_PAD for x in lines):
                ok = False
                break
        if ok:
            return size
    return sizes[-1]


def tile(img, box, P, a, idx, label, size, gold=False):
    """Карточка: номер слева сверху, текст внизу слева; отступ CARD_PAD со всех сторон."""
    x0, y0, x1, y1 = box
    card(img, box, P, a, gold=gold)
    put_text(img, (x0 + CARD_PAD, y0 + CARD_PAD), f"{idx:02d}", sans_font(26, True), P["acc"], a)
    f = serif(size, 500)
    lines = wrap(ImageDraw.Draw(img), label, f, x1 - x0 - 2 * CARD_PAD)
    lh = int(size * 1.18)
    ytop = y1 - CARD_PAD - len(lines) * lh
    col = P["acc"] if gold else P["text"]
    for j, line in enumerate(lines):
        put_text(img, (x0 + CARD_PAD, ytop + j * lh), line, f, col, a, rise=12 * (1 - a))


def L_bento(img, t, dur, sc, P):
    vd = sc["vd"].lower()
    text, sec, role = sc["text"], sc["sec"], sc["role"]
    d = ImageDraw.Draw(img)
    items = [s.strip() for s in re.split(r"\s+·\s+", sec)] if (role != "cta" and "·" in sec and "\n" not in sec) else []
    if items:  # заголовок + сетка карточек: единые поля, одинаковые отступы и один кегль
        tag(img, t, P)
        full, gap = W - 2 * SAFE_X, 28
        lines, size = fit_lines(text, sizes=(96, 84, 74), max_w=full, max_lines=3)
        lh = int(size * 1.22)
        three = len(items) > 2
        half = (full - gap) // 2
        widths = [full] + ([half, half] if three else [full] * (len(items) - 1))
        rows_h = 300
        n_rows = 2 if three else len(items)
        grid_h = n_rows * rows_h + (n_rows - 1) * gap
        total_h = len(lines) * lh + 80 + grid_h
        ty = max(290, (H - total_h) // 2 - 20)
        block(img, lines, serif(size, 500), SAFE_X, ty, lh, P["text"], t, delay=0.2, acc=P["acc"], acc_words={lines[-1].split(" ")[-1].strip(".,")})
        y0 = ty + len(lines) * lh + 80
        m = re.search(r"(\w+) recibe el acento dorado", sc["vd"], re.I)
        gold_word = m.group(1).lower() if m else None
        fsz = common_tile_size(items, widths, (58, 54, 50, 46, 42))
        for i, it in enumerate(items):
            a = ease_out((t - 0.9 - i * 0.45) / 0.65)
            if i == 0 or not three:
                x0, row = SAFE_X, i
            else:
                x0, row = SAFE_X + (i - 1) * (half + gap), 1
            ybox = y0 + row * (rows_h + gap) + int(24 * (1 - a))
            gold = bool(gold_word and gold_word in it.lower()) or (gold_word is None and i == 0)
            tile(img, (x0, ybox, x0 + widths[i], ybox + rows_h), P, a, i + 1, it, fsz, gold)
        return
    if role == "hook" and sec:  # два блока друг над другом, второй с золотой обводкой
        tag(img, t, P)
        full, h_b, gap = W - 2 * SAFE_X, 330, 50
        y0 = (H - (2 * h_b + gap)) // 2 - 10
        fsz = common_tile_size([text, sec], [full, full], (112, 100, 90, 80, 72), max_lines=1)
        for i, (label, gold) in enumerate(((text, False), (sec, True))):
            a = ease_out((t - 0.3 - i * 0.6) / 0.7)
            yb = y0 + i * (h_b + gap) + int(20 * (1 - a))
            tile(img, (SAFE_X, yb, SAFE_X + full, yb + h_b), P, a, i + 1, label, fsz, gold)
        return
    if "formulario" in vd:  # карточка формы с абстрактными полями
        tag(img, t, P)
        a = ease_out(t / 0.7)
        card(img, (SAFE_X, 330, W - SAFE_X, 1560), P, a, radius=44)
        lines, size = fit_lines(text, sizes=(84, 74, 66), max_w=W - 2 * SAFE_X - 2 * CARD_PAD, max_lines=2)
        block(img, lines, serif(size, 500), SAFE_X + CARD_PAD, 400, int(size * 1.22), P["text"], t, delay=0.3, acc=P["acc"], acc_words={lines[-1].split(" ")[-1].strip(".,?¿")})
        for i in range(4):
            fa = ease_out((t - 0.7 - i * 0.18) / 0.5)
            y = 700 + i * 190
            put_text(img, (SAFE_X + CARD_PAD, y), "—" * (3 + i % 3), sans_font(26), P["stone"], fa)
            rect(img, (SAFE_X + CARD_PAD, y + 50, W - SAFE_X - CARD_PAD, y + 140), None, fa, outline=mix(P["text"], P["bg"], 0.7), radius=20, width=2)
        rect(img, (SAFE_X + CARD_PAD, 1470, SAFE_X + 400, 1540), P["stone"], 0.35 * ease_out((t - 1.5) / 0.5), radius=35)
        return
    if role == "cta" and ("móvil" in vd or "interfaz" in vd):  # карточка в виде мобильного интерфейса
        a = ease_out(t / 0.7)
        x0, x1, y0, y1 = (W - 700) // 2, (W + 700) // 2, 330, 1590
        card(img, (x0, y0, x1, y1), P, a, gold=True, radius=80)
        hline(img, W // 2 - 60, W // 2 + 60, y0 + 44, P["stone"], 0.6 * a, 4)
        spaced(img, W // 2, y0 + 120, "R DESIGN SPACE", sans_font(24), P["stone"], ease_out((t - 0.3) / 0.6), track=6)
        lines, size = fit_lines(text, sizes=(76, 68, 60), max_w=560, max_lines=4)
        block(img, lines, serif(size, 500), W // 2, y0 + 260, int(size * 1.24), P["text"], t, delay=0.5, align="center")
        yb = y0 + 260 + len(lines) * int(size * 1.24) + 70
        parts = sec.split("\n")
        put_text(img, (W // 2, yb), parts[0], sans_font(34), P["stone"], ease_out((t - 1.0) / 0.6), anchor="ma")
        ba = ease_out((t - 1.3) / 0.6)
        rect(img, (x0 + CARD_PAD, y1 - 330, x1 - CARD_PAD, y1 - 190), P["acc"], ba, radius=70)
        put_text(img, (W // 2, y1 - 260), HANDLE, sans_font(42, True), P["bg"], ba, anchor="mm")
        return
    if "botón" in vd:  # минималистичный интерфейс: золотая кнопка и тихие вторичные ссылки
        tag(img, t, P)
        a = ease_out(t / 0.7)
        card(img, (SAFE_X, 520, W - SAFE_X, 1330), P, a, radius=44)
        lines, size = fit_lines(text, sizes=(96, 84, 74), max_w=W - 2 * SAFE_X - 2 * CARD_PAD, max_lines=2)
        block(img, lines, serif(size, 500), SAFE_X + CARD_PAD, 600, int(size * 1.22), P["text"], t, delay=0.3)
        sec_block(img, sec, t, 600 + len(lines) * int(size * 1.22) + 30, P, delay=0.8, align="left", x=SAFE_X + CARD_PAD, size=36)
        ba = ease_out((t - 1.2) / 0.6)
        rect(img, (SAFE_X + CARD_PAD, 1020, SAFE_X + 560, 1120), P["acc"], ba, radius=50)
        for i in range(3):
            la = ease_out((t - 1.6 - i * 0.2) / 0.5)
            hline(img, SAFE_X + CARD_PAD + i * 190, SAFE_X + CARD_PAD + i * 190 + 120, 1220, P["stone"], 0.55 * la, 3)
        return
    # CTA и прочие: карточка с брендом сверху; геометрия по visual_direction
    if "vertical" in vd and "derecha" in vd:
        box = (W - SAFE_X - 600, 420, W - SAFE_X, 1500)
        wmax, lab_x = 600 - 2 * CARD_PAD, box[0] + CARD_PAD
    elif "central" in vd:
        box = (SAFE_X, 620, W - SAFE_X, 1300)
        wmax, lab_x = W - 2 * SAFE_X - 2 * CARD_PAD, SAFE_X + CARD_PAD
    else:  # горизонтальная карточка в нижней трети
        box = (SAFE_X, 1010, W - SAFE_X, 1560)
        wmax, lab_x = W - 2 * SAFE_X - 2 * CARD_PAD, SAFE_X + CARD_PAD
        tag(img, t, P)
    a = ease_out(t / 0.7)
    card(img, box, P, a, gold=("central" in vd))
    if wmax < 600:  # узкая карточка: подпись бренда слева сверху
        put_text(img, (lab_x, box[1] + 56), "R DESIGN SPACE", sans_font(24, True), P["acc"], ease_out((t - 0.3) / 0.6))
    elif "central" in vd:
        spaced(img, W // 2, box[1] + 56, "R DESIGN SPACE", sans_font(24), P["acc"], ease_out((t - 0.3) / 0.6), track=6)
    lines, size = fit_lines(text, sizes=(76, 66, 58, 52), max_w=wmax, max_lines=5)
    lh = int(size * 1.22)
    ty = box[1] + 130
    block(img, lines, serif(size, 500), lab_x, ty, lh, P["text"], t, delay=0.5, acc=P["acc"], acc_words={lines[-1].split(" ")[-1].strip(".,")})
    hline(img, lab_x, lab_x + int(110 * ease_out((t - 1.0) / 0.8)), ty + len(lines) * lh + 30, P["acc"], 0.9, 3)
    sec_block(img, sec, t, ty + len(lines) * lh + 70, P, delay=1.2, align="left", x=lab_x, size=34, maxw=wmax)


LAYOUTS = {"LAYOUT_HERO": L_hero, "LAYOUT_BENTO": L_bento, "LAYOUT_METRIC": L_metric, "LAYOUT_QUOTE": L_quote}

_bg_cache: dict = {}


def themed_bg(P, key):
    """Фон на 8 % больше кадра: медленный параллакс по сцене (микро-зум/дрейф)."""
    if key not in _bg_cache:
        bw, bh = int(W * 1.08), int(H * 1.08)
        y, x = np.mgrid[0:bh, 0:bw].astype(np.float32)
        glow = np.exp(-(((x - bw * 0.22) / (bw * 0.9)) ** 2 + ((y - bh * 0.18) / (bh * 0.45)) ** 2))
        vig = 1 - (0.14 if P["light"] else 0.5) * (((x - bw / 2) / (bw / 2)) ** 2 * 0.5 + ((y - bh / 2) / (bh / 2)) ** 2 * 0.5)
        base = np.array(P["bg"], dtype=np.float32)[None, None, :]
        k = (0.97 + 0.05 * glow) if P["light"] else (0.85 + 0.55 * glow)
        arr = base * k[..., None] * vig[..., None]
        _bg_cache[key] = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    return _bg_cache[key]


def spec_from_batch(script: dict, meta: dict, palettes: dict, idx: int) -> dict:
    sid = str(script["id"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", sid):
        raise SystemExit(f"Некорректный id ролика: {sid!r}")
    scenes = []
    for i, s in enumerate(script["scenes"]):
        layout = s.get("layout", "LAYOUT_HERO")
        if layout not in LAYOUTS:
            raise SystemExit(f"{sid}: неизвестный layout {layout}")
        vd = s.get("visual_direction", "")
        theme = "ivory" if "marfil" in vd.lower() else "dark"
        P = palettes[theme]
        sc = {"text": s.get("text", ""), "sec": s.get("secondary_text", ""), "role": s.get("role", ""), "vd": vd}
        bg_big = themed_bg(P, theme)

        def draw(img, t, d, fn=LAYOUTS[layout], sc=sc, P=P):
            fn(img, t, d, sc, P)

        def bgfn(t, d, big=bg_big):
            p = clamp(t / max(d, 0.1))
            bw, bh = big.size
            dx, dy = int(p * (bw - W)), int((1 - p) * (bh - H) * 0.6)
            return big.crop((dx, dy, dx + W, dy + H))

        nominal = max(2.0, float(s.get("end_seconds", 0)) - float(s.get("start_seconds", 0)))
        key = "hook" if i == 0 else f"{s.get('role', 's')}_{i}"
        scenes.append(Scene(key, nominal, draw, text=s.get("voiceover", ""), bgfn=bgfn))
    m = re.fullmatch(r"(\d+)_(\d+)", sid)
    num = int(m.group(1)) * 100 + int(m.group(2)) if m else 0  # 02_01 -> 201: метки ig_reel_201 / tt_reel_201
    links = {"instagram": f"https://t.me/r_design_space_bot?start=ig_reel_{num}",
             "tiktok": f"https://t.me/r_design_space_bot?start=tt_reel_{num}"} if num else {}
    return {"id": sid, "title": script.get("title", sid), "scenes": scenes, "lang": meta.get("language", "es"), "deeplinks": links}


def spec_default() -> dict:
    script = read_voiceover(HERE / "script.md")
    scenes = [Scene(k, nom, fn, text=script.get(k, "")) for (k, nom, fn) in SCENES_DEF]
    missing = [sc.key for sc in scenes if not sc.text]
    if missing:
        raise SystemExit(f"В блоке voiceover нет строк: {', '.join(missing)}")
    return {"id": "reel_01", "title": "Before / After", "scenes": scenes, "lang": "ru", "out": OUT,
            "deeplinks": {"instagram": "https://t.me/r_design_space_bot?start=ig_reel_1",
                          "tiktok": "https://t.me/r_design_space_bot?start=tt_reel_1"}}


# ----------------------------------------------------------------------------- build one reel


def build_reel(spec: dict, args, key: str) -> dict:
    rid, scenes = spec["id"], spec["scenes"]
    work = BUILD / rid
    work.mkdir(parents=True, exist_ok=True)
    out = spec.get("out") or (BUILD / f"{rid}.mp4")
    print(f"\n=== {rid}: {spec['title']} ===")

    texts = {sc.key: sc.text for sc in scenes if sc.text}
    audio, eng = synthesize_all(texts, key, args.tts, args.voice, lang=spec["lang"])
    for sc in scenes:
        sc.audio = audio.get(sc.key)
        spoken = probe_duration(sc.audio) if sc.audio else 0.0
        sc.duration = max(sc.nominal, spoken + 0.45)
        print(f"  {sc.key:7s} {eng:7s} голос {spoken:4.1f} с -> сцена {sc.duration:4.1f} с")
    if eng != "openai":
        print(f"ВНИМАНИЕ: голос сгенерирован не через OpenAI tts-1-hd ({eng}). Задайте OPENAI_API_KEY и перезапустите.")
    t0 = 0.0
    for sc in scenes:
        sc.start = t0
        t0 += sc.duration
    total = t0
    print(f"Итого {total:.1f} с")

    bg = make_background()

    if args.preview:
        pv = BUILD / "preview"
        pv.mkdir(exist_ok=True)
        for sc in scenes:
            for frac in (0.25, 0.9):
                Image.fromarray(render_frame(bg, sc, sc.duration * frac, 0)).save(pv / f"{rid}_{sc.key}_{int(frac * 100)}.png")
        print("Стоп-кадры:", pv)
        return {"id": rid, "preview": True}

    silent = work / "video_silent.mp4"
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-maxrate", "9M", "-bufsize", "18M", "-pix_fmt", "yuv420p",
         "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", str(silent)],
        stdin=subprocess.PIPE)
    n = 0
    print("Рендер кадров…")
    for sc in scenes:
        frames = int(round(sc.duration * FPS))
        for i in range(frames):
            fr = render_frame(bg, sc, i / FPS, n)
            enc.stdin.write(zoom_crop(fr, 0.04 * (i / frames)).tobytes())
            n += 1
        print(f"  {sc.key} ({frames} кадров)")
    enc.stdin.close()
    if enc.wait() != 0:
        raise SystemExit("ffmpeg (видео) завершился с ошибкой")

    # аудио: голос каждой сцены со сдвигом, плюс тихий ambient-фон
    inputs, filters, labels = [], [], []
    for sc in scenes:
        if not sc.audio:
            continue
        i = len(labels)
        inputs += ["-i", str(sc.audio)]
        ms = int((sc.start + 0.15) * 1000)
        filters.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms},volume=1.6[v{i}]")
        labels.append(f"[v{i}]")
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=longest[voice]")
    # ambient: мягкий аккорд (A2 + E3 + A3 + C#4), медленная пульсация, фейды
    pad = "+".join(f"sin(2*PI*{f}*t)*{a}" for f, a in [(110, 0.5), (164.81, 0.35), (220, 0.3), (277.18, 0.18)])
    filters.append(f"aevalsrc='({pad})*(0.75+0.25*sin(2*PI*0.18*t))':s=48000:d={total:.2f},lowpass=f=900,volume=0.05,"
                   f"afade=t=in:d=1.5,afade=t=out:st={total - 2:.2f}:d=2[pad]")
    filters.append("[voice][pad]amix=inputs=2:normalize=0,alimiter=limit=0.95,loudnorm=I=-16:TP=-1.5:LRA=7[a]")
    mixed = work / "mix.m4a"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[a]",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", f"{total:.2f}", str(mixed)]
    if subprocess.run(cmd).returncode != 0:
        raise SystemExit("ffmpeg (аудио) завершился с ошибкой")

    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-i", str(mixed), "-c:v", "copy", "-c:a", "copy",
                    "-shortest", "-movflags", "+faststart", str(out)], check=True)
    dur = probe_duration(out)
    print(f"Готово: {out}  ({out.stat().st_size / 1e6:.1f} МБ, {dur:.1f} с)")
    return {"id": rid, "title": spec["title"], "file": out.name, "duration_s": round(dur, 1), "voice_engine": eng,
            "language": spec["lang"], "deeplinks": spec["deeplinks"]}


def build(args) -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} не найден в PATH")
    global BUILD
    if args.build_dir:
        BUILD = Path(args.build_dir).resolve()
    BUILD.mkdir(parents=True, exist_ok=True)
    key = load_openai_key()

    if args.json:
        data = json.loads(Path(args.json).read_text(encoding="utf-8-sig"))
        batch = isinstance(data, dict) and "scripts" in data
        items = data["scripts"] if batch else data if isinstance(data, list) else [data]
        if args.only:
            wanted = {x.strip() for x in args.only.split(",")}
            items = [it for it in items if it.get("id") in wanted]
        if not items:
            raise SystemExit("В JSON нет роликов (проверьте --only)")
        ids = [it.get("id") for it in items]
        if len(set(ids)) != len(ids):
            raise SystemExit("В JSON повторяются id роликов")
        if batch:
            palettes = make_palettes(data.get("visual_style", {}))
            specs = [spec_from_batch(it, data, palettes, i) for i, it in enumerate(items)]
        else:
            specs = [spec_from_json(it) for it in items]
    else:
        specs = [spec_default()]

    results = []
    for spec in specs:
        try:
            results.append(build_reel(spec, args, key))
        except SystemExit as e:  # один сбойный ролик не должен останавливать пакет
            print(f"ОШИБКА {spec['id']}: {e}")
            results.append({"id": spec["id"], "error": str(e)})
    if args.json and not args.preview:
        manifest = BUILD / "manifest.json"
        manifest.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        ok = sum(1 for r in results if "file" in r)
        print(f"\nПакет: {ok}/{len(results)} видео, манифест {manifest}")
        if ok != len(results):
            raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tts", choices=["auto", "openai", "edge", "sapi"], default="auto")
    ap.add_argument("--voice", default="", help="openai: onyx|alloy; edge: ru-RU-DmitryNeural|en-US-GuyNeural")
    ap.add_argument("--preview", action="store_true", help="только стоп-кадры")
    ap.add_argument("--json", default="", help="JSON-файл (массив или один объект) с роликами -> пакет в build/")
    ap.add_argument("--build-dir", default="", help="папка результата (по умолчанию reels_01/build)")
    ap.add_argument("--only", default="", help="id роликов через запятую (только с --json)")
    build(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
