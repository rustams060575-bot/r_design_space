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

OPENAI_API_KEY берётся из окружения или из agent/.env (файл в .gitignore).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
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
        await edge_tts.Communicate(text, voice, pitch="-4Hz").save(str(out))

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


def synth_one(text: str, eng: str, key: str, voice: str) -> Path:
    v = voice or {"openai": "onyx", "edge": "ru-RU-DmitryNeural", "sapi": ""}[eng]
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


def synthesize_all(texts: dict[str, str], key: str, engine: str, voice: str) -> tuple[dict[str, Path], str]:
    """Один и тот же движок для ВСЕХ сцен (иначе голос меняется посреди ролика). Кеш по тексту, движку и голосу."""
    order = {"auto": ["openai", "edge", "sapi"], "openai": ["openai"], "edge": ["edge"], "sapi": ["sapi"]}[engine]
    errors = []
    for eng in order:
        try:
            return {k: synth_one(t, eng, key, voice) for k, t in texts.items()}, eng
        except Exception as e:  # noqa: BLE001
            errors.append(f"{eng}: {e}")
    raise SystemExit("Озвучка не получилась: " + "; ".join(errors))


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


# ----------------------------------------------------------------------------- render


def render_frame(bg: Image.Image, scene: Scene, t: float, frame_no: int) -> np.ndarray:
    img = bg.copy()
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


def build(args) -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} не найден в PATH")
    BUILD.mkdir(exist_ok=True)
    script = read_voiceover(HERE / "script.md")
    key = load_openai_key()

    scenes = [Scene(k, nom, fn) for (k, nom, fn) in SCENES_DEF]
    missing = [sc.key for sc in scenes if not script.get(sc.key)]
    if missing:
        raise SystemExit(f"В блоке voiceover нет строк: {', '.join(missing)}")
    print("Озвучка…")
    audio, eng = synthesize_all({sc.key: script[sc.key] for sc in scenes}, key, args.tts, args.voice)
    engines = {eng}
    for sc in scenes:
        sc.audio = audio[sc.key]
        sc.duration = max(sc.nominal, probe_duration(sc.audio) + 0.45)
        print(f"  {sc.key:7s} {eng:7s} голос {probe_duration(sc.audio):4.1f} с -> сцена {sc.duration:4.1f} с")
    if "openai" not in engines:
        print("ВНИМАНИЕ: голос сгенерирован не через OpenAI tts-1-hd (%s). Задайте OPENAI_API_KEY и перезапустите." % ", ".join(sorted(engines)))
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
                t = sc.duration * frac
                Image.fromarray(render_frame(bg, sc, t, 0)).save(pv / f"{sc.key}_{int(frac * 100)}.png")
        print("Стоп-кадры:", pv)
        return

    silent = BUILD / "video_silent.mp4"
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-maxrate", "9M", "-bufsize", "18M", "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart", str(silent)],
        stdin=subprocess.PIPE)
    n = 0
    print("Рендер кадров…")
    for sc in scenes:
        frames = int(round(sc.duration * FPS))
        for i in range(frames):
            t = i / FPS
            fr = render_frame(bg, sc, t, n)
            fr = zoom_crop(fr, 0.04 * (i / frames))
            enc.stdin.write(fr.tobytes())
            n += 1
        print(f"  {sc.key} ({frames} кадров)")
    enc.stdin.close()
    if enc.wait() != 0:
        raise SystemExit("ffmpeg (видео) завершился с ошибкой")

    # аудио: голос каждой сцены со сдвигом, плюс тихий ambient-фон
    inputs, filters, labels = [], [], []
    for i, sc in enumerate(scenes):
        inputs += ["-i", str(sc.audio)]
        ms = int((sc.start + 0.15) * 1000)
        filters.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms},volume=1.6[v{i}]")
        labels.append(f"[v{i}]")
    n_in = len(scenes)
    filters.append("".join(labels) + f"amix=inputs={n_in}:normalize=0:duration=longest[voice]")
    # ambient: мягкий аккорд (A2 + E3 + A3 + C#4), медленная пульсация, фейды
    pad = "+".join(f"sin(2*PI*{f}*t)*{a}" for f, a in [(110, 0.5), (164.81, 0.35), (220, 0.3), (277.18, 0.18)])
    filters.append(f"aevalsrc='({pad})*(0.75+0.25*sin(2*PI*0.18*t))':s=48000:d={total:.2f},lowpass=f=900,volume=0.05,"
                   f"afade=t=in:d=1.5,afade=t=out:st={total - 2:.2f}:d=2[pad]")
    filters.append("[voice][pad]amix=inputs=2:normalize=0,alimiter=limit=0.95,loudnorm=I=-16:TP=-1.5:LRA=7[a]")
    mixed = BUILD / "mix.m4a"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[a]",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", f"{total:.2f}", str(mixed)]
    if subprocess.run(cmd).returncode != 0:
        raise SystemExit("ffmpeg (аудио) завершился с ошибкой")

    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-i", str(mixed), "-c:v", "copy", "-c:a", "copy",
                    "-shortest", "-movflags", "+faststart", str(OUT)], check=True)
    print(f"Готово: {OUT}  ({OUT.stat().st_size / 1e6:.1f} МБ, {probe_duration(OUT):.1f} с)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tts", choices=["auto", "openai", "edge", "sapi"], default="auto")
    ap.add_argument("--voice", default="", help="openai: onyx|alloy; edge: ru-RU-DmitryNeural|ru-RU-SvetlanaNeural")
    ap.add_argument("--preview", action="store_true", help="только стоп-кадры")
    build(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
