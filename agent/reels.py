"""james · reels engine: renders a 9:16 kinetic-typography reel (MP4, H.264 + AAC) from a concept in growth.CONCEPT_BANK.

    python agent/reels.py <concept-id> [out.mp4]        # render one reel for review

Frames are drawn with Pillow (same quiet-luxury style as cards.py) and piped straight into ffmpeg: no MoviePy/OpenCV needed.
Structure: 1.5-2 s hook (motion in the very first frame, <=5 words), one scene per on-screen line, end card with the cta.
Video: 1080x1920 (9:16), 24 fps, yuv420p, faststart. Audio: original ambient bed from audio.py (hits on cuts, fade in/out, -15 LUFS); a silent result is rejected.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import audio
import cards

W, H, FPS = 1080, 1920, 24
HOOK_SECONDS, SCENE_SECONDS, END_SECONDS = 2.0, 2.6, 2.6


def ease(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def mix(fg, bg, a: float):
    return tuple(int(bg[i] + (fg[i] - bg[i]) * a) for i in range(3))


def accent_last(text: str) -> str:
    words = text.split()
    if not words or "*" in text:
        return text
    stripped = words[-1].rstrip(".,!?")
    words[-1] = f"*{stripped}*{words[-1][len(stripped):]}"
    return " ".join(words)


def scenes_from_concept(concept: dict) -> list[dict]:
    """hook + each on-screen line (skipping the studio sign-off, which becomes the end card)."""
    lines = [concept["hook"]["on_screen_text"]]
    for shot in concept["shots"][1:]:
        text = shot.get("on_screen", "").strip()
        if text and "ar design" not in text and "link in bio" not in text and text not in lines:
            lines.append(text)
    out = [{"text": accent_last(t), "seconds": HOOK_SECONDS if i == 0 else SCENE_SECONDS, "hook": i == 0} for i, t in enumerate(lines)]
    out.append({"text": "ar design studio", "sub": concept["cta"], "seconds": END_SECONDS, "end": True})
    return out


def layout_scene(scene: dict):
    big = scene.get("hook") or scene.get("end")
    fnt, lines = cards.fit_headline(scene["text"], W - 2 * 96, max_lines=5, start=150 if big else 118, floor=72)
    flat = [(word, acc, li) for li, line in enumerate(lines) for word, acc in line]
    sub_font = cards.font("sans", 40)
    return fnt, lines, flat, sub_font


def background(t: float) -> Image.Image:
    img = Image.new("RGB", (W, H), cards.BG)
    d = ImageDraw.Draw(img)
    for i in range(1, 6):  # five hairlines drift very slowly
        x = (i * W // 6 + int(t * 6)) % W
        d.line([x, 0, x, H], fill=(24, 24, 25), width=1)
    sweep = int((t * 140) % (H + 400)) - 200  # a single champagne scan hairline
    d.line([0, sweep, W, sweep], fill=mix(cards.ACCENT, cards.BG, 0.22), width=2)
    d.rectangle([40, 40, W - 40, H - 40], outline=cards.LINE, width=2)
    return img


def draw_scene(img: Image.Image, scene: dict, prepared, local_t: float, total_t: float, total: float, footer: str = cards.HANDLE) -> None:
    fnt, lines, flat, sub_font = prepared
    d = ImageDraw.Draw(img)
    lead = int(fnt.size * 1.12)
    block_h = len(lines) * lead
    y0 = (H - block_h) // 2 - (40 if scene.get("end") else 0)
    stagger = 0.10 if scene.get("hook") else 0.17
    fade_out = 1.0 - ease((local_t - (scene["seconds"] - 0.3)) / 0.3) if local_t > scene["seconds"] - 0.3 else 1.0
    pos = 0
    for li, line in enumerate(lines):
        cx = 96
        for word, acc in line:
            start = pos * stagger
            a = ease((local_t - start) / 0.38)
            if scene.get("hook") and pos == 0:
                a = max(a, 0.35 + 0.65 * ease(local_t / 0.2))  # motion and contrast already in frame 1
            a *= fade_out
            if a > 0.01:
                rise = int((1 - ease((local_t - start) / 0.38)) * 34)
                color = mix(cards.ACCENT if acc else cards.INK, cards.BG, a)
                d.text((cx, y0 + li * lead + rise), word, font=fnt, fill=color)
            cx += fnt.getlength(word + " ")
            pos += 1
    if scene.get("sub"):
        a = ease((local_t - 0.9) / 0.5) * fade_out
        d.text((96, y0 + block_h + 36), scene["sub"], font=sub_font, fill=mix(cards.STONE, cards.BG, a))
    d.text((96, 92), cards.LABEL, font=cards.font("mono", 30), fill=cards.STONE)
    d.text((96, H - 250), footer, font=cards.font("mono", 32), fill=cards.STONE)
    d.rectangle([96, H - 190, 96 + int((W - 192) * (total_t / total)), H - 187], fill=cards.ACCENT)  # progress hairline


def render_reel(concept: dict, out_path: Path, footer: str = cards.HANDLE) -> Path:
    try:
        ffmpeg = audio.ffmpeg_exe()  # system ffmpeg, else the static binary from the imageio-ffmpeg wheel
    except Exception as exc:
        raise RuntimeError(f"ffmpeg not found ({type(exc).__name__})") from None
    scenes = scenes_from_concept(concept)
    prepared = [layout_scene(s) for s in scenes]
    total = sum(s["seconds"] for s in scenes)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cuts, acc = [], 0.0
    for sc in scenes:  # a low hit lands on every cut (and on frame 1: the hook)
        cuts.append(round(acc, 3))
        acc += sc["seconds"]
    seed = sum(ord(ch) for ch in concept["id"])
    music = audio.make_track(total, seed, cuts, out_path.with_suffix(".wav"))
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(music), "-t", f"{total:.2f}",
           "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "160k", "-ar", "44100", *audio.mix_args(total), "-shortest", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    frames = int(math.ceil(total * FPS))
    try:
        for n in range(frames):
            t = n / FPS
            acc, idx = 0.0, 0
            while idx < len(scenes) - 1 and t >= acc + scenes[idx]["seconds"]:
                acc += scenes[idx]["seconds"]
                idx += 1
            img = background(t)
            draw_scene(img, scenes[idx], prepared[idx], t - acc, t, total, footer)
            proc.stdin.write(img.tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    music.unlink(missing_ok=True)
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError("ffmpeg failed")
    level = mean_volume_db(ffmpeg, out_path)
    if level is None or level < -45:  # never ship a silent reel
        raise RuntimeError(f"reel audio is silent (mean {level} dB)")
    return out_path


def mean_volume_db(ffmpeg: str, path: Path) -> float | None:
    done = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    for line in done.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    return None


if __name__ == "__main__":
    from growth import CONCEPT_BANK
    wanted = sys.argv[1] if len(sys.argv) > 1 else CONCEPT_BANK[0]["id"]
    concept = next(c for c in CONCEPT_BANK if c["id"] == wanted)
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(f"{wanted}.mp4")
    print(render_reel(concept, target))
