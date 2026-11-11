"""james · ar02 queue builder: copies the finished content into the repo (so Meta can fetch it by raw https URL)
and writes content-hub/ar02/queue.json (publication order, per-platform media and captions).

    python agent/ar02_build.py "C:/Users/ramaz/OneDrive/Desktop/matrix/AR_Content_02"

Sources: matrix/reels R01-R08 and matrix/carousels C01-C04 (first series, one master per reel), AR_Content_02/deliverables R09-R18 and C05-C10
(second series; only the voice+piano reel masters and the carousel PNGs are published, piano-only masters stay local).
Order: R01 C01 R02 C02 ... R10 C10 R11 ... R18 (morning slot = reel, evening slot = carousel, strictly by number).
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DEST = REPO / "content-hub" / "ar02"
# Day by day: reel in the morning slot, carousel in the evening slot, strictly by number; after C10 only reels remain (two a day).
REELS = [f"R{i:02d}" for i in range(1, 19)]
CAROUSELS = [f"C{i:02d}" for i in range(1, 11)]
ORDER = [x for pair in zip(REELS, CAROUSELS) for x in pair] + REELS[len(CAROUSELS):]
OLD = Path("C:/Users/ramaz/OneDrive/Desktop/matrix")  # R01-R08 / C01-C04 (first series), never published before
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


def old_reel_caption(n: int) -> str:
    """R01-R08 have no caption file: use the spoken text of the video itself (its on-screen subtitles), which ends with its CTA."""
    t = (OLD / "reels" / f"R{n:02d}" / "index.html").read_text(encoding="utf-8")
    vo = " ".join(html_lib.unescape(x) for x in re.findall(r'class="sub[^"]*"[^>]*><span>(.*?)</span>', t))
    return vo + "\n\nConcept demo."


def fb_cta(text: str) -> str:
    return re.sub(r"\bDM (?=[\u201c\"]?[A-Z]{3,})", "Message us: ", text)


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
        num = int(cid[1:])
        if cid.startswith("R") and num <= 8:  # first series: one master per reel (voice + piano baked in), same file for both platforms
            f = next((OLD / "reels").glob(f"{cid}_*.mp4"))
            shutil.copy2(f, d / f.name)
            item = {"kind": "reel", "files_ig": [rel(d / f.name)], "files_fb": [rel(d / f.name)]}
            cap = old_reel_caption(num)
            caps[(cid, "instagram")], caps[(cid, "facebook")] = cap, fb_cta(cap)
        elif cid.startswith("C") and num <= 4:
            cdir = next((OLD / "carousels").glob(f"{cid}_*"))
            files = []
            for k in range(1, 8):
                shutil.copy2(cdir / f"slide_{k:02d}.png", d / f"{cid}_{k:02d}.png")
                files.append(rel(d / f"{cid}_{k:02d}.png"))
            item = {"kind": "carousel", "files_ig": files, "files_fb": files}
            cap = (cdir / "caption.txt").read_text(encoding="utf-8").strip()
            caps[(cid, "instagram")], caps[(cid, "facebook")] = cap, fb_cta(cap)
        elif cid.startswith("R"):
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
