"""james · original ambient soundtracks for reels, synthesised with ffmpeg (no downloads, no licensing risk).

Why synthesis: trending commercial tracks cannot be attached through the Instagram API and a brand account that uploads
copyrighted music gets muted or taken down. These beds are generated here, so ar design studio owns them outright.
Sound: deep ambient - detuned sine pads, a slow sub pulse, soft pink-noise air, reverb, and low "hits" that land on the
scene cuts of the reel (the cut timing comes from reels.py).

    make_track(duration, seed, hits, out_wav)   # -> wav
    mix_args(duration)                           # ffmpeg -af chain: fade in/out + loudness normalisation
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SR = 44100
# (root hz, name): deep, minor, cinematic. m3, P5, m7 ratios in equal temperament.
ROOTS = [(55.0, "A"), (43.65, "F"), (73.42, "D"), (49.0, "G"), (65.41, "C"), (61.74, "B")]
M3, P5, M7, M9 = 1.18921, 1.49831, 1.78180, 2.24492
CHORD = [1.0, P5, 2.0 * M3, 2.0 * P5, 2.0 * M7, 4.0 * M9 / 2]  # root, fifth, minor third (oct), fifth (oct), minor seventh (oct), 9th


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def hit_expr(hits: list[float]) -> str:
    """aevalsrc expression: a soft sub 'boom' (pitch-dropping sine with exponential decay) at each time in `hits`."""
    parts = [f"gte(t,{t:.3f})*exp(-4.5*(t-{t:.3f}))*sin(2*PI*(46+60*exp(-14*(t-{t:.3f})))*(t-{t:.3f}))" for t in hits]
    return "0.55*(" + "+".join(parts or ["0"]) + ")"


def make_track(duration: float, seed: int, hits: list[float], out_wav: Path) -> Path:
    length = duration + 1.0
    r1, _ = ROOTS[seed % len(ROOTS)]
    r2, _ = ROOTS[(seed + 2) % len(ROOTS)]
    half = length / 2
    inputs, chains, labels = [], [], []

    def sine(freq: float, start: float, span: float, level: float, tag: str) -> None:
        idx = len(inputs) // 6  # each lavfi input adds 6 argv items
        inputs.extend(["-f", "lavfi", "-t", f"{span:.2f}", "-i", f"sine=f={freq:.3f}:sample_rate={SR}"])
        fade = min(2.4, span / 3)
        chains.append(f"[{idx}:a]volume={level},afade=t=in:d={fade},afade=t=out:st={span - fade:.2f}:d={fade},"
                      f"adelay={int(start * 1000)}|{int(start * 1000)}[{tag}]")
        labels.append(f"[{tag}]")

    for ci, (root, start, span) in enumerate(((r1, 0.0, half + 2.0), (r2, half - 2.0, half + 2.0))):
        for ni, ratio in enumerate(CHORD):
            level = 0.20 / (1 + ni * 0.35)
            for side, detune in (("a", 0.997), ("b", 1.003)):  # two detuned voices per note: slow beating = width
                sine(root * ratio * detune, start, span, level, f"c{ci}n{ni}{side}")

    idx = len(inputs) // 6  # each lavfi input adds 6 argv items
    inputs.extend(["-f", "lavfi", "-t", f"{length:.2f}", "-i", f"anoisesrc=color=pink:amplitude=0.05:sample_rate={SR}"])
    chains.append(f"[{idx}:a]lowpass=f=900,highpass=f=120,volume=0.25,afade=t=in:d=3[air]")
    labels.append("[air]")
    idx = len(inputs) // 6  # each lavfi input adds 6 argv items
    inputs.extend(["-f", "lavfi", "-t", f"{length:.2f}", "-i", f"aevalsrc='{hit_expr(hits)}':s={SR}"])
    chains.append(f"[{idx}:a]volume=1.0[hits]")
    labels.append("[hits]")
    idx = len(inputs) // 6  # each lavfi input adds 6 argv items
    inputs.extend(["-f", "lavfi", "-t", f"{length:.2f}", "-i", f"sine=f={r1 / 2:.3f}:sample_rate={SR}"])
    chains.append(f"[{idx}:a]volume=0.22,tremolo=f=1.5:d=0.85,afade=t=in:d=2[sub]")  # ~90 bpm sub pulse
    labels.append("[sub]")

    graph = ";".join(chains) + ";" + "".join(labels) + f"amix=inputs={len(labels)}:normalize=0,lowpass=f=2400," \
        "aecho=0.8:0.85:70|140|230:0.38|0.28|0.18,aformat=channel_layouts=stereo[out]"
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, "-map", "[out]", "-ar", str(SR), "-t", f"{length:.2f}", str(out_wav)]
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if done.returncode != 0 or not out_wav.exists():
        raise RuntimeError("soundtrack synthesis failed: " + done.stderr[-300:])
    return out_wav


def mix_args(duration: float) -> list[str]:
    """ffmpeg output audio filters: 0.7 s fade in, 1.6 s fade out, loudness about -15 LUFS with a -1.5 dBTP ceiling."""
    fade_out = max(0.0, duration - 1.6)
    return ["-af", f"afade=t=in:st=0:d=0.7,afade=t=out:st={fade_out:.2f}:d=1.6,loudnorm=I=-15:TP=-1.5:LRA=7"]
