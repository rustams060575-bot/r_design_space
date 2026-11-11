"""james · ar02 queue builder: copies the finished AR_Content_02 deliverables into the repo (so Meta can fetch them by raw https URL)
and writes content-hub/ar02/queue.json (publication order, slots, per-platform media and captions).

    python agent/ar02_build.py "C:/Users/ramaz/OneDrive/Desktop/matrix/AR_Content_02"

Only the voice+piano reel masters and the carousel PNGs are published. Piano-only masters stay local.
Order = the Playbook 02 week plan (R12, C08, R18, R17, R09, C05, R10, C06, R11, C07, R16, R13, R14, C10, R15, C09).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DEST = REPO / "content-hub" / "ar02"
ORDER = ["R12", "C08", "R18", "R17", "R09", "C05", "R10", "C06", "R11", "C07", "R16", "R13", "R14", "C10", "R15", "C09"]
TAGS = "#webdesign #uxdesign #websitedesign #designagency #uidesign"


def captions(src: Path) -> dict:
    """captions.txt blocks: '[ID] [Platform] [...]' header line followed by the caption text."""
    out: dict = {}
    lines = (src / "captions.txt").read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        m = re.match(r"\[(R\d+|C\d+)\] \[(Instagram|Facebook)\]", ln)
        if m and i + 1 < len(lines):
            out[(m.group(1), m.group(2).lower())] = lines[i + 1].strip()
    return out


def sha(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


def build(src: Path) -> list[dict]:
    caps = captions(src)
    shutil.rmtree(DEST, ignore_errors=True)
    queue = []
    for n, cid in enumerate(ORDER, 1):
        d = DEST / cid
        d.mkdir(parents=True, exist_ok=True)
        if cid.startswith("R"):
            ig_src = src / "deliverables" / "reels" / cid / f"{cid}_IG_voice_piano.mp4"
            fb_src = src / "deliverables" / "reels" / cid / f"{cid}_FB_voice_piano.mp4"
            shutil.copy2(ig_src, d / ig_src.name)
            fb = d / ig_src.name
            if sha(fb_src) != sha(ig_src):
                shutil.copy2(fb_src, d / fb_src.name)
                fb = d / fb_src.name
            item = {"kind": "reel", "files_ig": [rel(d / ig_src.name)], "files_fb": [rel(fb)]}
        else:
            cdir = src / "deliverables" / "carousels" / cid
            ig_files = [cdir / f"{cid}_{k:02d}.png" for k in range(1, 8)]
            fb_files = ig_files[:6] + ([cdir / f"{cid}_07_FB.png"] if (cdir / f"{cid}_07_FB.png").exists() else [ig_files[6]])
            for f in ig_files + fb_files:
                if not (d / f.name).exists():
                    shutil.copy2(f, d / f.name)
            item = {"kind": "carousel", "files_ig": [rel(d / f.name) for f in ig_files], "files_fb": [rel(d / f.name) for f in fb_files]}
        item.update({"id": cid, "order": n,
                     "caption_ig": f"{caps[(cid, 'instagram')]}\n\n{TAGS}",
                     "caption_fb": caps[(cid, "facebook")]})
        queue.append(item)
    (DEST / "queue.json").write_text(json.dumps(queue, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return queue


if __name__ == "__main__":
    q = build(Path(sys.argv[1]))
    size = sum(f.stat().st_size for f in DEST.rglob("*") if f.is_file()) / 1e6
    print(f"{len(q)} items, {size:.1f} MB in {DEST}")
