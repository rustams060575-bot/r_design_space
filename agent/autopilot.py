"""james · autopilot: generate, render, host and publish posts (carousels) and stories to Instagram and the Facebook Page.

    python agent/scraper.py --action autopilot        # what GitHub Actions runs (twice a day)

One run: pick today's content -> render JPEG cards (cards.py) -> commit them to the public repo so Meta can fetch them by
https URL -> publish to @ar_design_agency and the "AR Design" Page in sync -> log -> telegram report.

Guardrails (autonomy is bounded, not unlimited):
  * at most 1 feed post and 2 stories per UTC day (state in content-hub/autopilot_state.json);
  * the model may write fresh copy, but every card passes validate_copy(); anything with metrics, money, awards, guarantees
    or client names is rejected and the vetted bank is used instead;
  * kill switch: META_PUBLISH_LIVE != "1" means dry run (cards are rendered, nothing is pushed or published);
  * nothing is published when a platform's credentials are missing; failures are reported in telegram and never retried in a loop.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import cards
import llm
import meta_publisher as mp
import reels
from growth import CONCEPT_BANK

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
STATE_PATH = REPO_ROOT / "content-hub" / "autopilot_state.json"
MEDIA_DIR = REPO_ROOT / "content-hub" / "published"
MAX_POSTS_PER_DAY, MAX_STORIES_PER_DAY, MAX_REELS_PER_DAY = 1, 0, 1  # strictly 1 reel + 1 carousel a day, no stories
REEL_WEEKDAYS = (0, 1, 2, 3, 4, 5, 6)  # a reel every day (prime slot)
CORE_TAGS = ["#uidesign", "#webdevelopment", "#webengineering", "#minimalistdesign", "#awwwards", "#digitalagency"]
CTA = "start a project: link in bio."

# ---------------------------------------------------------------- vetted content bank (quiet luxury, no claims we cannot prove)

CAROUSELS = [
    {"id": "template-vs-flagship", "slides": [
        {"h": "a template *loads*. a flagship *lands*.", "b": "what separates a site that exists from a site that sells."},
        {"h": "hierarchy before *animation*.", "b": "one message per screen. motion explains. it never decorates."},
        {"h": "performance is *design*.", "b": "a fast page feels expensive. weight, fonts and images are part of the craft."},
        {"h": "details people *feel*.", "b": "spacing, easing, hover states. nobody names them. everybody notices."},
        {"h": "built in *code*. finished to the pixel.", "b": "flagship web engineering for brands that care."}],
     "caption": "a template gets you online. a flagship build gets you remembered.\n\nflagship web engineering, built in code and finished to the pixel."},
    {"id": "scroll-is-the-story", "slides": [
        {"h": "the scroll is the *story*.", "b": "a page is not a list of sections. it is a sequence."},
        {"h": "one idea per *screen*.", "b": "give each moment room. let the next one earn its entrance."},
        {"h": "motion with a *reason*.", "b": "every transition answers a question: where am i, what changed, what next."},
        {"h": "type does the *heavy lifting*.", "b": "kinetic typography carries tone before a single image loads."},
        {"h": "your launch deserves a *scroll* like this.", "b": "scroll-driven sites, engineered end to end."}],
     "caption": "the page is the product demo. scroll-driven storytelling, engineered end to end."},
    {"id": "design-systems-quiet", "slides": [
        {"h": "a design system is *quiet* infrastructure.", "b": "you notice it only when it is missing."},
        {"h": "tokens first, *pixels* second.", "b": "color, type and spacing defined once, used everywhere."},
        {"h": "components that *refuse* to drift.", "b": "new pages stay on-brand without a designer in the loop."},
        {"h": "speed is the *dividend*.", "b": "teams ship faster when the rules are already written in code."},
        {"h": "systems that *scale* with you.", "b": "design systems for products that keep growing."}],
     "caption": "consistency is not a style guide. it is code that makes the right choice the easy one."},
    {"id": "dashboard-ux", "slides": [
        {"h": "dashboards should feel *calm*.", "b": "dense data does not have to feel dense."},
        {"h": "show the *decision*, not the data.", "b": "lead with what the user must act on."},
        {"h": "reduce *every* click.", "b": "complex flows, simplified until they feel obvious."},
        {"h": "empty states are *design* too.", "b": "the first minute of a new account sets the tone for the product."},
        {"h": "saas interfaces, *quietly* engineered.", "b": "ux for complex products."}],
     "caption": "complex product, calm interface. that is the whole job."},
    {"id": "launch-page-anatomy", "slides": [
        {"h": "anatomy of a *launch* page.", "b": "five decisions that shape how a product is perceived."},
        {"h": "the first *three seconds*.", "b": "a clear promise, a confident type system, no noise."},
        {"h": "proof before *persuasion*.", "b": "show the product working. let the interface argue the case."},
        {"h": "one *call to action*.", "b": "every section points the same way."},
        {"h": "then build it *properly*.", "b": "flagship launch sites, engineered for the moment."}],
     "caption": "a launch is a moment. the page should be built like one."},
    {"id": "what-we-dont-do", "slides": [
        {"h": "what we *don't* do.", "b": "a studio is defined by what it refuses."},
        {"h": "no *templates*.", "b": "every build starts from your story, not from a theme."},
        {"h": "no *stock* anything.", "b": "no filler imagery, no borrowed motion, no decorative noise."},
        {"h": "no *shortcuts* in code.", "b": "clean, readable, maintainable. you can hand it to any engineer."},
        {"h": "just *flagship* work.", "b": "AR Design. web engineering with taste."}],
     "caption": "restraint is a feature. fewer things, done properly."},
]

STORIES = [
    {"id": "s-clean-code", "h": "clean code is a *design* decision."},
    {"id": "s-space", "h": "space is the most *expensive* material on a page."},
    {"id": "s-motion", "h": "motion should *explain*, not entertain."},
    {"id": "s-type", "h": "good type needs *no* introduction."},
    {"id": "s-speed", "h": "a fast site feels *premium*."},
    {"id": "s-details", "h": "the *details* are the product.", "b": "spacing. easing. hover states."},
    {"id": "s-scroll", "h": "scroll. *don't* click.", "b": "scroll-driven stories, engineered."},
    {"id": "s-template", "h": "built, not *assembled*."},
    {"id": "s-restraint", "h": "restraint is a *feature*."},
    {"id": "s-system", "h": "design systems keep *brands* consistent.", "b": "components that refuse to drift."},
    {"id": "s-cta", "h": "have a launch *coming up*?", "b": "link in bio. tell us about the project."},
    {"id": "s-flagship", "h": "flagship web engineering.", "b": "AR Design"},
]

# ---------------------------------------------------------------- validation of any (model-written) copy

BANNED = re.compile(r"(\d+\s?%|[$€£]\s?\d|\d+\s?(k|m)\b|guarantee|award[- ]winning|best in|#1|no\.? ?1|clients? like|trusted by|\bfree\b|discount|limited (time|spots)|!)", re.I)


def validate_copy(*texts: str) -> bool:
    joined = " ".join(t for t in texts if t)
    return bool(joined.strip()) and not BANNED.search(joined)


def validate_carousel(c: dict) -> bool:
    slides = c.get("slides", [])
    return (4 <= len(slides) <= 6 and all(validate_copy(s.get("h", ""), s.get("b", "")) and len(s.get("h", "")) <= 80 and len(s.get("b", "")) <= 140 for s in slides)
            and validate_copy(c.get("caption", "")) and len(c.get("caption", "")) <= 600)


def validate_story(s: dict) -> bool:
    return validate_copy(s.get("h", ""), s.get("b", "")) and 0 < len(s.get("h", "")) <= 70 and len(s.get("b", "")) <= 90


MODEL_SYSTEM = """You write instagram carousel copy for AR Design (always written exactly as AR Design, never with Studio or Space), a studio for flagship web engineering. Voice: quiet luxury,
clean code, minimalism, kinetic typography, complex UX/UI. American english, all lowercase, no emojis, no exclamation marks, no hype.
Hard rules: no numbers with %, $, k or m; no prices, awards, guarantees, rankings, client names or statistics; make timeless craft statements only.
Mark the single accent word of a headline with *asterisks*. Reply with ONLY one JSON object:
{"id": "kebab-slug", "slides": [{"h": "headline (max 70 chars)", "b": "one short body sentence (max 120 chars)"}] (5 slides, the last is a soft call to action), "caption": "2 short lines"}
Topic must differ from the already used ids."""


def model_carousel(cfg: dict, used: list[str]) -> dict | None:
    text = llm.complete(MODEL_SYSTEM, f"already used ids: {', '.join(used[-12:]) or 'none'}\nWrite one new carousel.", cfg, max_tokens=900, model=cfg["llm"].get("anthropic_model"))
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        c = json.loads(match.group(0))
        c["id"] = re.sub(r"[^a-z0-9-]", "", str(c.get("id", "model")).lower())[:40] or "model"
    except json.JSONDecodeError:
        return None
    return c if validate_carousel(c) else None


# ---------------------------------------------------------------- state

def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("carousel_i", 0)
    state.setdefault("story_i", 0)
    state.setdefault("reel_i", 0)
    state.setdefault("log", [])
    return state


def save_state(state: dict) -> None:
    state["log"] = state["log"][-120:]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def done_today(state: dict, kind: str, today: str) -> int:
    return sum(1 for e in state["log"] if e["date"] == today and e["kind"] == kind and e.get("ok"))


# ---------------------------------------------------------------- hosting (public repo raw URLs)

def repo_slug() -> str:
    return mp.os.environ.get("GITHUB_REPOSITORY") or "rustams060575-bot/r_design_space"


def raw_url(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return f"https://raw.githubusercontent.com/{repo_slug()}/main/{rel}"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)


def host(paths: list[Path], today: str) -> bool:
    """Commits and pushes the rendered cards, then waits until each URL answers 200. Returns True when Meta can fetch them."""
    rels = [p.relative_to(REPO_ROOT).as_posix() for p in paths]
    git("config", "user.name", "james")
    git("config", "user.email", "james@users.noreply.github.com")
    git("add", *rels)
    if git("diff", "--cached", "--quiet").returncode != 0:
        git("commit", "-m", f"james: media for {today} [skip ci]")
        git("pull", "--rebase", "origin", "main")
        if git("push", "origin", "HEAD:main").returncode != 0:
            return False
    deadline = time.time() + 120
    pending = [raw_url(p) for p in paths]
    while pending and time.time() < deadline:
        pending = [u for u in pending if requests.head(u, timeout=15, allow_redirects=True).status_code != 200]
        if pending:
            time.sleep(5)
    return not pending


# ---------------------------------------------------------------- publish

def hashtags(cfg: dict, seed: int, geo_tags: list[str] | None = None) -> str:
    extra = ["#creativecoding", "#webdesign", "#uxdesign", "#frontend", "#brandidentity", "#gsap", "#threejs", "#scrollanimation"]
    pick = [extra[(seed + i) % len(extra)] for i in range(3)]
    return " ".join(CORE_TAGS + pick + (geo_tags or []))


def send_report(text: str) -> None:
    try:
        from telegram_bot import send_text
        send_text(text)
    except Exception as exc:  # reporting must never break publishing
        print(f"[autopilot] telegram skipped ({type(exc).__name__})")


def publish(graph: "mp.Graph", item: dict) -> dict:
    results = {}
    for platform in item["platforms"]:
        if not graph.has(platform):
            results[platform] = "skipped: credentials not set"
            continue
        try:
            results[platform] = "published:" + (graph.publish_instagram(item) if platform == "instagram" else graph.publish_facebook(item))
        except (RuntimeError, requests.RequestException) as exc:
            results[platform] = f"failed: {exc}"
    return results


def run_autopilot(cfg: dict) -> int:
    """The ONLY publishing path: the owner's finished queue (content-hub/ar02/queue.json) at 09:00 / 21:00 Miami.

    There is no generation here and none can be switched on by config: templates, the concept bank and model-written
    cards were removed on purpose (owner's decision, 2026-11-11).
    """
    import ar02
    import leads_report
    from datetime import datetime as _dt, timezone as _tz
    rc = ar02.run(cfg)  # the post first: a long lead search never delays it
    et = _dt.now(_tz.utc).astimezone(ar02.ZoneInfo(ar02.TZ))
    try:
        leads_report.run_if_due(cfg, ar02.slot_of(et))
    except Exception as exc:  # noqa: BLE001 - the lead report must never break the publishing run
        print(f"[leads] failed: {type(exc).__name__}: {str(exc)[:160]}")
    return rc


def run_reel_test() -> int:
    """CI smoke test: render a reel with sound, check the audio is not silent, print stats. Publishes nothing."""
    import tempfile
    import audio
    concept = CONCEPT_BANK[0]
    out = Path(tempfile.mkdtemp()) / "reeltest.mp4"
    reels.render_reel(concept, out)  # render_reel raises if the audio is missing, silent or truncated
    info = reels.audio_info(audio.ffmpeg_exe(), out)
    print(f"[reeltest] ok: {out.stat().st_size // 1024} KB, audio {info}")
    return 0
