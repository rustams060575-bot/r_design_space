"""james · ar02 autopilot: publishes the finished AR_Content_02 queue (Reels R09–R18, carousels C05–C10) at two fixed US-Central (Chicago) slots: carousels in the morning, reels in the evening.

    python agent/scraper.py --action autopilot        # started by the external trigger (cron-job.org -> /api/cron?action=autopilot)

Slots (America/Chicago, daylight saving handled by the zone): morning 09:00 = carousel, evening 21:00 = reel. The run takes the NEXT unpublished
item OF THAT KIND from content-hub/ar02/queue.json (queue order inside each kind; a manual run takes the next item of any kind), publishes it to Instagram and the Facebook Page, logs it and reports to Telegram.

Guardrails:
  * old templates / concept bank / model-generated cards are NOT used in this mode;
  * at most one item per slot per Chicago day (a double trigger or a re-run does nothing);
  * kill switch: META_PUBLISH_LIVE != "1" is a dry run (nothing is published, state is not advanced);
  * a failed publish is reported and not retried in a loop; the item stays at the head of the queue for the next slot.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import meta_publisher as mp
from autopilot import load_state, raw_url, save_state, send_report

REPO = Path(__file__).resolve().parent.parent
QUEUE = REPO / "content-hub" / "ar02" / "queue.json"
TZ = "America/Chicago"
SLOT_KIND = {"morning": "carousel", "evening": "reel"}


WINDOWS = {"morning": ((9, 0), (10, 30)), "evening": ((21, 0), (22, 30))}  # America/Chicago; GitHub cron may start late, never early


def slot_of(et: datetime) -> str | None:
    """Slot whose window contains this Chicago time, else None (the run then does nothing)."""
    t = (et.hour, et.minute)
    for name, (a, b) in WINDOWS.items():
        if a <= t < b:
            return name
    return None


def next_item(queue: list, state: dict, slot: str):
    """Next unpublished queue item for the slot: carousel in the morning, reel in the evening, any kind for a manual run."""
    done = {e.get("id") for e in state.get("log", []) if e.get("source") == "ar02" and e.get("ok")}
    want = SLOT_KIND.get(slot)
    for i, q in enumerate(queue):
        if q["id"] not in done and (want is None or q["kind"] == want):
            return i, q
    return None, None


def media_ready(paths: list[str], wait: int = 90) -> bool:
    urls = [raw_url(REPO / p) for p in paths]
    deadline = time.time() + wait
    while urls and time.time() < deadline:
        urls = [u for u in urls if requests.head(u, timeout=15, allow_redirects=True).status_code != 200]
        if urls:
            time.sleep(5)
    return not urls


def make_item(q: dict) -> dict:
    ig = [raw_url(REPO / p) for p in q["files_ig"]]
    fb = [raw_url(REPO / p) for p in q["files_fb"]]
    item = {"kind": q["kind"], "platforms": list(mp.PLATFORMS), "caption": q["caption_ig"], "caption_fb": q["caption_fb"],
            "publish_at": datetime.now(timezone.utc).isoformat()}
    if q["kind"] == "carousel":
        item["media_urls"], item["media_urls_fb"] = ig, fb
    else:
        item["media_url"], item["media_url_fb"] = ig[0], fb[0]
    return item


def run(cfg: dict, now: datetime | None = None, force: bool | None = None) -> int:
    graph = mp.Graph()
    live = graph.live
    et = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(TZ))
    force = (os.getenv("SEARCH_QUERY", "").strip().lower() == "now") if force is None else force  # manual: "now" publishes the next item
    slot = slot_of(et) or ("manual" if force else None)
    today = et.strftime("%Y-%m-%d")
    if slot is None:
        print(f"[ar02] {et.strftime('%Y-%m-%d %H:%M %Z')}: outside the 09:00 / 21:00 Chicago windows, nothing to do")
        return 0
    state = load_state()
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    if any(e.get("date") == today and e.get("slot") == slot and e.get("ok") and e.get("source") == "ar02" for e in state["log"]):
        print(f"[ar02] {today} {slot}: already published, nothing to do")
        return 0
    i, q = next_item(queue, state, slot)
    if q is None:
        kind = SLOT_KIND.get(slot, "item")
        print(f"[ar02] no unpublished {kind} left in the queue: nothing published (old templates are disabled)")
        send_report(f"james ar02 · {today} {slot}: no unpublished {kind} left, nothing published. Add new content to content-hub/ar02.")
        return 0
    item = make_item(q)
    print(f"[ar02] {today} {et.strftime('%H:%M %Z')} slot={slot} live={live} next={q['id']} ({q['kind']}, #{q['order']}/{len(queue)})")
    entry = {"date": today, "time": et.strftime("%H:%M"), "slot": slot, "kind": "post" if q["kind"] == "carousel" else "reel",
             "id": q["id"], "source": "ar02", "live": live, "ok": False}
    if not live:
        print(f"[ar02] DRY RUN: would publish {q['id']} to {', '.join(item['platforms'])}")
        send_report(f"james ar02 · dry run · {today} {slot}: next would be {q['id']}")
        return 0
    if not media_ready(sorted(set(q["files_ig"] + q["files_fb"]))):
        entry["result"] = {"error": "media not reachable by https"}
        state["log"].append(entry)
        save_state(state)
        send_report(f"⚠️ james ar02 · {q['id']}: media is not reachable, nothing published")
        return 0
    problems = mp.validate_item(item)
    if problems:
        entry["result"] = {"error": "; ".join(problems)}
        state["log"].append(entry)
        save_state(state)
        send_report(f"⚠️ james ar02 · {q['id']}: invalid ({'; '.join(problems)})")
        return 0
    results = {}
    for platform in item["platforms"]:
        if not graph.has(platform):
            results[platform] = "skipped: credentials not set"
            continue
        try:
            results[platform] = "published:" + (graph.publish_instagram(item) if platform == "instagram" else graph.publish_facebook(item))
        except (RuntimeError, requests.RequestException) as exc:
            results[platform] = f"failed: {exc}"
    entry["result"], entry["ok"] = results, any(v.startswith("published") for v in results.values())
    state["log"].append(entry)
    if entry["ok"]:
        state["ar02_i"] = max(state.get("ar02_i", 0), i + 1)
    save_state(state)
    send_report(f"{'✅' if entry['ok'] else '⚠️'} james ar02 · {today} {slot} · {q['id']} · " + ", ".join(f"{k}: {v.split(':')[0]}" for k, v in results.items()))
    print(f"[ar02] {q['id']}: {results}")
    return 0
