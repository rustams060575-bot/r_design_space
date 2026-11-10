"""james · piano soundtracks for reels: real recordings, all CC0 / public domain (see assets/music/LICENSES.md).

Tracks live in assets/music (AAC, first 80 s of each recording) with a list of verified "usable starts" in tracks.json:
windows where the piano plays steadily (no silence). A reel takes one 13 s window, picked by the reel's seed, then
mix_args() adds fade in/out and loudness normalisation. Nothing is downloaded at render time.

    make_track(duration, seed, hits, out_wav)   # -> wav (hits are ignored: piano needs no 'impacts')
    mix_args(duration)                           # ffmpeg -af chain: fade in/out + loudness normalisation
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

MUSIC_DIR = Path(__file__).resolve().parent.parent / "assets" / "music"
SR = 44100


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def library() -> list[dict]:
    tracks = json.loads((MUSIC_DIR / "tracks.json").read_text(encoding="utf-8"))
    return [t for t in tracks if t.get("starts") and (MUSIC_DIR / t["file"]).exists()]


def pick(seed: int) -> tuple[dict, int]:
    tracks = library()
    if not tracks:
        raise RuntimeError("no piano tracks in assets/music")
    track = tracks[seed % len(tracks)]
    start = track["starts"][(seed // len(tracks)) % len(track["starts"])]
    return track, start


def make_track(duration: float, seed: int, hits: list[float], out_wav: Path) -> Path:
    track, start = pick(seed)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", str(start), "-t", f"{duration + 1.0:.2f}", "-i", str(MUSIC_DIR / track["file"]),
           "-ac", "2", "-ar", str(SR), str(out_wav)]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if done.returncode != 0 or not out_wav.exists():
        raise RuntimeError("soundtrack cut failed: " + done.stderr[-300:])
    return out_wav


def mix_args(duration: float) -> list[str]:
    """Output audio filters: 0.8 s fade in, 2 s fade out, loudness about -15 LUFS with a -1.5 dBTP ceiling."""
    fade_out = max(0.0, duration - 2.0)
    return ["-af", f"afade=t=in:st=0:d=0.8,afade=t=out:st={fade_out:.2f}:d=2.0,loudnorm=I=-15:TP=-1.5:LRA=9"]
