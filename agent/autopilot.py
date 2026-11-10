"""james · autopilot: generate, render, host and publish posts (carousels) and stories to Instagram and the Facebook Page.

    python agent/scraper.py --action autopilot        # what GitHub Actions runs (twice a day)

One run: pick today's content -> render JPEG cards (cards.py) -> commit them to the public repo so Meta can fetch them by
https URL -> publish to @ar_design_agency and the "AR Design Studio" Page in sync -> log -> telegram report.

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
MAX_POSTS_PER_DAY, MAX_STORIES_PER_DAY, MAX_REELS_PER_DAY = 1, 2, 1
REEL_WEEKDAYS = (0, 2, 4)  # mon, wed, fri: three reels a week
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
        {"h": "just *flagship* work.", "b": "ar design studio. web engineering with taste."}],
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
    {"id": "s-flagship", "h": "flagship web engineering.", "b": "ar design studio"},
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


MODEL_SYSTEM = """You write instagram carousel copy for ar design studio (always lowercase), a studio for flagship web engineering. Voice: quiet luxury,
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

def hashtags(cfg: dict, seed: int) -> str:
    extra = ["#creativecoding", "#webdesign", "#uxdesign", "#frontend", "#brandidentity", "#gsap", "#threejs", "#scrollanimation"]
    pick = [extra[(seed + i) % len(extra)] for i in range(3)]
    return " ".join(CORE_TAGS + pick)


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
    graph = mp.Graph()
    live = graph.live
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    jobs = []
    forced = os.getenv("SEARCH_QUERY", "").strip().lower()  # manual run: query "reel" forces a reel today
    if (now.weekday() in REEL_WEEKDAYS and now.hour < 16 or forced == "reel") and done_today(state, "reel", today) < MAX_REELS_PER_DAY:
        jobs.append("reel")
    if done_today(state, "post", today) < MAX_POSTS_PER_DAY:
        jobs.append("post")
    if done_today(state, "story", today) < MAX_STORIES_PER_DAY:
        jobs.append("story")
    if not jobs:
        print("[autopilot] daily quota reached")
        return 0
    print(f"[autopilot] {today} live={live} jobs={jobs}")
    platforms = [p for p in mp.PLATFORMS if graph.has(p)] or list(mp.PLATFORMS)
    report = []

    for job in jobs:
        used = [e["id"] for e in state["log"] if e["kind"] == job]
        if job == "reel":
            c, source = CONCEPT_BANK[state["reel_i"] % len(CONCEPT_BANK)], "bank"
            stem = f"{c['id']}-{now.strftime('%H%M')}"
            try:
                paths = [reels.render_reel(c, MEDIA_DIR / today / f"{stem}.mp4")]
            except (RuntimeError, OSError) as exc:
                print(f"[autopilot] reel render failed: {exc}")
                report.append(f"⚠️ reel {c['id']}: render failed, skipped")
                continue
            tags = " ".join(dict.fromkeys(CORE_TAGS + c["hashtags"]))
            item = {"kind": "reel", "platforms": platforms, "media_url": "", "caption": f"{c['caption'].strip()}\n\n{c['cta']}\n\n{tags}", "publish_at": now.isoformat()}
        elif job == "post":
            c = model_carousel(cfg, used) if llm.available(cfg) and cfg["james"].get("autopilot", {}).get("model_generation", True) else None
            source = "model"
            if not c:
                c, source = CAROUSELS[state["carousel_i"] % len(CAROUSELS)], "bank"
            stem = f"{c['id']}-{now.strftime('%H%M')}"
            paths = cards.render_carousel(c["slides"], MEDIA_DIR / today, stem)
            item = {"kind": "carousel", "platforms": platforms, "media_urls": [], "caption": f"{c['caption'].strip()}\n\n{CTA}\n\n{hashtags(cfg, state['carousel_i'])}", "publish_at": now.isoformat()}
        else:
            s = STORIES[state["story_i"] % len(STORIES)]
            c, source = s, "bank"
            stem = f"{s['id']}-{now.strftime('%H%M')}"
            paths = [cards.render_story(s, MEDIA_DIR / today, stem)]
            item = {"kind": "story", "platforms": platforms, "media_url": "", "caption": "", "publish_at": now.isoformat()}

        entry = {"date": today, "time": now.strftime("%H:%M"), "kind": job, "id": c["id"], "source": source, "live": live, "ok": False}
        if not live:
            entry["result"] = {"dry_run": [str(p.name) for p in paths]}
            print(f"[autopilot] DRY RUN {job} {c['id']} ({source}): rendered {len(paths)} card(s), nothing pushed or published")
            report.append(f"dry run · {job}: {c['id']}")
            continue
        if not host(paths, today):
            entry["result"] = {"error": "could not host media"}
            state["log"].append(entry)
            report.append(f"⚠️ {job} {c['id']}: media hosting failed, nothing published")
            continue
        urls = [raw_url(p) for p in paths]
        if job == "post":
            item["media_urls"] = urls
        else:
            item["media_url"] = urls[0]
        problems = mp.validate_item(item)
        if problems:
            entry["result"] = {"error": "; ".join(problems)}
            state["log"].append(entry)
            report.append(f"⚠️ {job} {c['id']}: invalid ({'; '.join(problems)})")
            continue
        result = publish(graph, item)
        entry["result"], entry["ok"] = result, any(v.startswith("published") for v in result.values())
        state["log"].append(entry)
        if entry["ok"]:
            state[{"post": "carousel_i", "story": "story_i", "reel": "reel_i"}[job]] += 1
        report.append(f"{'✅' if entry['ok'] else '⚠️'} {job} · {c['id']} · " + ", ".join(f"{k}: {v.split(':')[0]}" for k, v in result.items()))
        print(f"[autopilot] {job} {c['id']}: {result}")

    if live:
        save_state(state)
    send_report("james autopilot · " + today + "\n" + "\n".join(report))
    return 0
