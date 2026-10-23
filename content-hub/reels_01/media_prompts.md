# Reels 01 — промпты для генерации, озвучка, музыка

Концепт: **Before / After — шаблон → Quiet Luxury**. Тайминги — по [script.md](script.md).

> Важно: AI-генераторы плохо рисуют читаемый текст и интерфейсы. Используйте их для **атмосферных кадров, фонов, переходов и эффектов света**, а сам сайт (экран A и B) записывайте реальной записью экрана с вашей Tilda/HTML-страницы, текст накладывайте в CapCut/DaVinci шрифтами Playfair Display и Georgia.

## Общие параметры

- Формат 9:16, 1080×1920, 24–30 fps при генерации (в монтаже — 60 fps для экрана)
- Палитра: графит `#0E0E0F`, поверхность `#17171A`, off-white `#EDE8E0`, шампань/латунь `#B79B6B`
- Свет: мягкий боковой, низкий ключ, лёгкая дымка, без пересвета
- Движение: медленное (камера 3–6 с на кадр), без резких зумов, без «кибер» эффектов

**Negative prompt (общий):** `text, letters, watermark, logo, people, faces, neon, glitch, cyberpunk, bright colors, clutter, stock photo look, oversaturated, lens flare, cartoon, 3d render look, low quality`

---

## A. Midjourney (статичные кадры/фоны/обложка)

Параметры в конце: `--ar 9:16 --style raw --v 7 --s 150 --no text, letters, people`

**A1 — Фон хука (правая, «дорогая» сторона):**
```
Minimalist dark luxury interface backdrop, deep graphite background, thin 1px champagne-gold grid lines forming a 12-column layout, vast negative space, soft directional light from the left, subtle film grain, editorial, quiet luxury aesthetic, cinematic, ultra clean --ar 9:16 --style raw --v 7 --s 150 --no text, letters, people
```

**A2 — Фон «дешёвой» стороны (контраст):**
```
Cluttered generic website template backdrop, flat bright blue and orange blocks, overcrowded rectangles, harsh even lighting, abstract UI collage without text, looks cheap and busy --ar 9:16 --style raw --v 7 --s 100 --no text, letters, people
```

**A3 — Макро типографики (фон под деталь 01):**
```
Macro photograph of fine serif letterforms engraved on dark matte stone, champagne-gold highlights on thin serifs, shallow depth of field, soft raking light, quiet luxury, editorial still life, no readable words --ar 9:16 --style raw --v 7 --s 200 --no text, watermark
```

**A4 — Воздух и сетка (деталь 02):**
```
Architectural interior of a dark minimalist gallery, vast empty space, thin brass lines on the floor forming a precise grid, one soft shaft of warm light, long shadows, serene, quiet luxury, symmetrical asymmetry --ar 9:16 --style raw --v 7 --s 150 --no text, people
```

**A5 — Движение света (деталь 03):**
```
Abstract slow light sweep across a dark matte surface, a thin champagne-gold line of light drawing from left to right, soft bokeh, subtle haze, minimal, premium, calm --ar 9:16 --style raw --v 7 --s 150 --no text, people
```

**A6 — Обложка Reels:**
```
Split composition: left side dull flat generic blue interface blocks, right side deep graphite luxury minimal composition with thin gold grid lines and negative space, strong contrast, no text --ar 9:16 --style raw --v 7 --s 150 --no text, letters, people
```
Поверх в монтаже: «$300 / $3 000» (Playfair Display, 120 px).

---

## B. LTX Studio / видео-генерация (image-to-video, 3–5 с)

Загрузите кадры A1, A3, A4, A5 как начальные изображения. Общие настройки: 9:16, длительность 4 с, motion strength низкий (2–3 из 10).

**B1 (A1 → хук, 3 с):**
```
Slow push-in on a dark luxury minimalist backdrop, thin gold grid lines gently drawing themselves across the frame one by one, soft light drifting from the left, subtle haze, camera very slow and steady, cinematic, no text
```

**B2 (A3 → деталь 01, 4 с):**
```
Macro slow dolly along fine serif letterforms on dark stone, champagne-gold glints appear on the thin serifs as the light moves, shallow depth of field, calm, premium, no readable text
```

**B3 (A4 → деталь 02, 4 с):**
```
Slow lateral camera glide through an empty dark gallery, thin brass grid lines on the floor catching soft warm light, long soft shadows, serene, architectural, no people
```

**B4 (A5 → деталь 03, 4 с):**
```
A single thin champagne-gold line of light slowly draws from left to right across a dark matte surface, soft haze and glow trail, elegant and unhurried, minimal
```

**B5 (A6/финал → CTA, 3 с):**
```
Dark graphite frame with very slow gold light gradient moving upward, subtle grain, calm, empty space in the center for text
```

---

## C. ComfyUI (если нужен локальный пайплайн)

- Модель: SDXL / Flux (Quiet Luxury checkpoint по вкусу), разрешение 832×1472, затем upscale ×1.3
- **Positive:** `dark minimal luxury interface backdrop, thin gold grid lines, vast negative space, soft side light, fine grain, editorial, quiet luxury`
- **Negative:** `text, letters, watermark, logo, people, neon, glitch, bright colors, clutter, low quality, jpeg artifacts`
- Steps 28–32, CFG 4–6 (Flux: guidance 3), sampler `dpmpp_2m` + `karras`
- Видео: AnimateDiff / Wan 2.x image-to-video, motion scale 0.6–0.8, 49–81 кадров, затем интерполяция RIFE до 30 fps
- Seed зафиксировать одинаковый для всех кадров серии — единая атмосфера

---

## D. CapCut — монтаж и текст

1. Таймлайн 1080×1920, 30 fps (экран — 60 fps, замедление не требуется).
2. Слои: экран A / экран B (реальные записи) → поверх AI-фоны с blend `Soft Light` 15–25% (для атмосферы и зерна).
3. Шрифты (установить локально): **Playfair Display** — заголовки; **Georgia** — подписи. Цвет `#EDE8E0`, акцент `#B79B6B`.
4. Анимация текста: `Fade In` 0.4 с + смещение вверх 16 px, выход `Fade Out` 0.2 с.
5. Переходы: cut / `Dissolve` 6–8 кадров. Без «Glitch», «Shake», «Zoom».
6. Эффекты: `Film Grain` 8–10%, виньетка 10%.
7. Субтитры: автоматическая расшифровка → стиль Georgia 40 px, нижняя граница выше 280 px от низа.
8. Звуковая дорожка: голос −6 dB, музыка −18 LUFS общего микса, ducking на голос.
9. Экспорт: 1080×1920, H.264, 20 Mbps, yuv420p, AAC 192 kbps.

---

## E. Закадровая озвучка (Voiceover, ~20 с, 50–55 слов)

Темп спокойный, на 5–10% медленнее обычной речи; интонация уверенная, без «продающего» нажима. Паузы указаны `/`.

```
[0–3]   Почему ваш сайт выглядит на триста долларов, / даже если вы заплатили три тысячи?

[3–7]   Первое — шрифты. / Не больше двух. / Контраст засечек и спокойного текста — это и есть голос бренда.

[7–11]  Второе — пустота. / Люксу нужно место. / Пустое пространство говорит: вам некуда спешить.

[11–15] Третье — движение. / Не быстро и не медленно — / ровно настолько, чтобы чувствовать качество. / Так выглядит доверие.

[15–20] Хотите аудит сайта или новый проект? / Переходите в шапку профиля / и жмите на Telegram-бота — / он сразу оценит задачу и бюджет.
```

**Варианты озвучки:**
- Живой голос (рекомендуется): низкий, спокойный, запись в тихой комнате, микрофон на расстоянии 15–20 см, без компрессии «радио».
- AI-голос: ElevenLabs — стабильность 55–65%, стиль 10–20%; голоса низкого регистра (напр. *Adam / Brian* или русскоязычный аналог), скорость 0.95.
- Встроенный TTS бота (`OPENAI_TTS_VOICE=onyx`) подходит только для черновика.

---

## F. Музыкальный референс

Цель: ambient / minimal, 70–90 BPM, мягкий пульс, без вокала и резких дропов.

| Вариант | Жанр | Что искать |
|---|---|---|
| 1 | Minimal piano + ambient pad | «Nils Frahm — Says» (настроение), «Ólafur Arnalds — Near Light» |
| 2 | Deep / organic house, тихий | «Jon Hopkins — Abandon Window», «Rival Consoles — Untravel» |
| 3 | Cinematic tension, низкий пульс | «Max Richter — Sunlight» (структура), «Hania Rani — Esja» |

- **Авторские права:** для монетизируемого/рекламного аккаунта используйте треки из **Instagram Sound Collection / TikTok Commercial Music Library** или лицензии Artlist / Epidemic Sound. Названия выше — только референсы по настроению, не гарантия лицензии.
- Структура: 0–3 с — тихий удар/пауза, 3–15 с — ровный пульс, 15–20 с — затухание под CTA.
- Громкость: музыка −18 LUFS, голос на 6–8 dB выше.

---

## G. Обложка и описание
- Обложка: кадр A6 + «$300 / $3 000», Playfair Display.
- Описание и закреплённый комментарий — по [script.md](script.md); ссылка: **https://t.me/r_design_space_bot?start=ig_reel_1**
