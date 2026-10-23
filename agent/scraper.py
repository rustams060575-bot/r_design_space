"""Lead hunter and pipeline entry point.

Collects fresh SaaS/CRM launches from Product Hunt (Atom feed), Hacker News "Show HN" (Algolia API) and any
extra RSS/Atom feeds from config.json, filters them by keywords, finds a public contact address on the
product's own website, writes a pitch, alerts Telegram and (unless DRY_RUN) sends the email.

Run:  python agent/scraper.py
Emails are really sent only when the environment variable DRY_RUN is exactly "false".
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from email_sender import EMAIL_RE, send_email  # noqa: E402
from pitch_writer import write_pitch  # noqa: E402
from qualifier import qualify  # noqa: E402
from telegram_bot import send_alert, send_report  # noqa: E402

CONFIG_PATH = ROOT / "config.json"
TRACKER_PATH = ROOT / "tracker.json"
UA = "Mozilla/5.0 (compatible; RSpaceOutreachBot/1.0; +https://t.me/r_design_space)"
AGGREGATOR_HOSTS = ("producthunt.com", "ycombinator.com", "news.ycombinator.com", "indiehackers.com",
                    "twitter.com", "x.com", "linkedin.com", "facebook.com", "github.com", "apple.com",
                    "google.com", "youtube.com", "instagram.com")
PREFERRED_LOCALS = ("hello", "contact", "founders", "founder", "team", "hi", "info", "support", "sales")
JUNK_EMAIL = re.compile(r"(example\.|sentry|wixpress|yourdomain|domain\.com|email\.com|\.(png|jpe?g|gif|svg|webp|js|css)$)", re.I)
FOUND_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_session = requests.Session()
_session.headers["User-Agent"] = UA
_robots_cache: dict[str, RobotFileParser | None] = {}


# ---------------------------------------------------------------- storage

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_tracker(tracker: dict) -> None:
    TRACKER_PATH.write_text(json.dumps(tracker, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- helpers

def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(text or "")))).strip()


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    return [k for k in keywords if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text, re.I)]


def make_id(prefix: str, raw: str) -> str:
    return f"{prefix}:{hashlib.sha1(raw.encode()).hexdigest()[:16]}"


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_product_site(url: str | None) -> bool:
    h = host_of(url or "")
    return bool(h) and not any(h == a or h.endswith("." + a) for a in AGGREGATOR_HOSTS)


def get(url: str, timeout: int, **kw) -> requests.Response | None:
    try:
        r = _session.get(url, timeout=timeout, **kw)
        return r if r.status_code < 400 else None
    except requests.RequestException:
        return None


def allowed_by_robots(url: str, timeout: int) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots_cache:
        rp = RobotFileParser()
        r = get(f"{base}/robots.txt", timeout)
        if r is None:
            _robots_cache[base] = None  # missing/unreachable robots.txt = no restrictions
        else:
            rp.parse(r.text.splitlines())
            _robots_cache[base] = rp
    rp = _robots_cache[base]
    return rp is None or rp.can_fetch(UA, url)


# ---------------------------------------------------------------- sources

def parse_feed(xml_text: str) -> list[dict]:
    """Minimal RSS/Atom parser (namespace agnostic)."""
    local = lambda tag: tag.rsplit("}", 1)[-1]
    items = []
    for el in ET.fromstring(xml_text).iter():
        if local(el.tag) not in ("entry", "item"):
            continue
        d = {"title": "", "link": "", "summary": "", "id": ""}
        for ch in el:
            name = local(ch.tag)
            if name == "title":
                d["title"] = strip_html("".join(ch.itertext()))
            elif name == "link":
                if ch.attrib.get("href"):
                    if ch.attrib.get("rel", "alternate") == "alternate":
                        d["link"] = ch.attrib["href"]
                elif ch.text and not d["link"]:
                    d["link"] = ch.text.strip()
            elif name in ("content", "summary", "description") and not d["summary"]:
                d["summary"] = strip_html("".join(ch.itertext()))
            elif name in ("id", "guid"):
                d["id"] = (ch.text or "").strip()
        items.append(d)
    return items


def fetch_feed(url: str, source: str, cfg: dict) -> list[dict]:
    r = get(url, cfg["limits"]["request_timeout"])
    if r is None:
        print(f"[scraper] {source}: feed unavailable ({url})")
        return []
    try:
        entries = parse_feed(r.text)
    except ET.ParseError:
        print(f"[scraper] {source}: feed is not valid XML")
        return []
    leads = []
    for e in entries:
        if not e["link"]:
            continue
        leads.append({
            "id": make_id(source, e["id"] or e["link"]),
            "source": source,
            "name": e["title"].split(" - ")[0].split(" – ")[0].strip() or e["link"],
            "summary": e["summary"][:400],
            "url": e["link"],
            "website": None,
        })
    return leads


def fetch_hackernews(cfg: dict) -> list[dict]:
    """Targeted Hacker News scan through the public Algolia API.

    - Show HN: new products, one search per keyword; the item URL is the product's own website.
    - Ask HN: founders asking for design / UI / frontend help, one search per phrase in
      hackernews.ask_queries (no website, so these become Telegram-only leads).
    """
    hn = cfg.get("hackernews", {})
    cutoff = int(time.time() - cfg["limits"]["max_age_days"] * 86400)
    searches = []
    if cfg["sources"].get("hackernews_show"):
        searches += [("show_hn", kw) for kw in cfg["keywords"]]
    searches += [("ask_hn", q) for q in hn.get("ask_queries", [])]

    seen, leads = set(), []
    for tag, query in searches:
        r = get("https://hn.algolia.com/api/v1/search_by_date", cfg["limits"]["request_timeout"],
                params={"tags": tag, "query": query, "hitsPerPage": 20,
                        "numericFilters": f"created_at_i>{cutoff}"})
        if r is None:
            continue
        for h in r.json().get("hits", []):
            oid = h.get("objectID")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            title = re.sub(r"^(show|ask) hn:\s*", "", h.get("title") or "", flags=re.I)
            parts = re.split(r"\s[–—-]\s", title, maxsplit=1)
            name, tagline = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
            is_ask = tag == "ask_hn"
            leads.append({
                "id": f"hn:{oid}",
                "source": "hackernews_ask" if is_ask else "hackernews",
                "name": (title if is_ask else name) or title,
                "summary": strip_html(f"{tagline}. {h.get('story_text') or ''}")[:500].strip(". "),
                "url": f"https://news.ycombinator.com/item?id={oid}",
                "website": None if is_ask else (h.get("url") if is_product_site(h.get("url")) else None),
                "author": h.get("author"),
            })
    return leads


def collect_leads(cfg: dict) -> list[dict]:
    src = cfg["sources"]
    leads: list[dict] = []
    if src.get("producthunt_feed"):
        leads += fetch_feed(src["producthunt_feed"], "producthunt", cfg)
    if src.get("hackernews_show") or cfg.get("hackernews", {}).get("ask_queries"):
        leads += fetch_hackernews(cfg)
    for url in src.get("extra_feeds", []):
        leads += fetch_feed(url, "feed", cfg)

    signals = cfg.get("design_signals", [])
    unique = {}
    for lead in leads:
        text = f"{lead['name']} {lead['summary']}"
        hits, sig = keyword_hits(text, cfg["keywords"]), keyword_hits(text, signals)
        # Ask HN posts were already found by design-related phrases: a signal alone is enough there.
        relevant = bool(hits) or (lead["source"] == "hackernews_ask" and bool(sig))
        if relevant and lead["id"] not in unique:
            lead["keywords"], lead["signals"] = hits, sig
            unique[lead["id"]] = lead
    # Strongest matches first, so the per-run cap keeps the best leads.
    return sorted(unique.values(), key=lambda l: -(len(l["keywords"]) * 2 + len(l["signals"])))


# ---------------------------------------------------------------- contact discovery

def resolve_website(lead: dict, timeout: int) -> str | None:
    """Product Hunt posts only link to the product through a tracked outbound URL; try to find it."""
    if lead.get("website") or lead["source"] != "producthunt":
        return lead.get("website")
    if not allowed_by_robots(lead["url"], timeout):
        return None
    r = get(lead["url"], timeout)
    if r is None:
        return None
    for link in re.findall(r'href="(https?://[^"]+)"', r.text):
        if is_product_site(link) and re.search(r"(ref|utm_source)=producthunt", link):
            return link.split("?")[0]
    return None


def find_contact_email(website: str, timeout: int) -> str | None:
    """Looks for a public business address on the homepage and /contact (robots.txt respected)."""
    base = f"{urlparse(website).scheme}://{urlparse(website).netloc}"
    domain = host_of(website)
    candidates: set[str] = set()
    for path in ("/", "/contact", "/about"):
        url = urljoin(base, path)
        if not allowed_by_robots(url, timeout):
            continue
        r = get(url, timeout)
        if r is None:
            continue
        for m in FOUND_EMAIL_RE.findall(html.unescape(r.text)):
            m = m.lower().strip(".")
            if EMAIL_RE.match(m) and not JUNK_EMAIL.search(m):
                candidates.add(m)
        time.sleep(0.5)
    if not candidates:
        return None

    def rank(addr: str):
        local, _, dom = addr.partition("@")
        same = dom == domain or dom.endswith("." + domain) or domain.endswith("." + dom)
        pref = PREFERRED_LOCALS.index(local) if local in PREFERRED_LOCALS else len(PREFERRED_LOCALS)
        return (not same, pref, addr)

    best = min(candidates, key=rank)
    return best if rank(best)[0] is False else None  # only addresses on the product's own domain


def site_blurb(website: str, timeout: int) -> str:
    """Title, meta description and commercial hints from the homepage - extra context for the qualifier."""
    if not allowed_by_robots(website, timeout):
        return ""
    r = get(website, timeout)
    if r is None:
        return ""
    page = r.text[:200_000]
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    desc = (re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\'](.*?)["\']', page, re.I | re.S)
            or re.search(r'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:name|property)=["\'](?:og:)?description["\']', page, re.I | re.S))
    hints = sorted({h.lower() for h in re.findall(
        r"\b(pricing|sign ?up|log ?in|book a demo|free trial|get started|dashboard|subscribe)\b", strip_html(page), re.I)})
    parts = [strip_html(title.group(1)) if title else "", strip_html(desc.group(1)) if desc else "",
             f"page mentions: {', '.join(hints)}" if hints else ""]
    return " | ".join(p for p in parts if p)[:400]


# ---------------------------------------------------------------- pipeline

def run() -> int:
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("config.json is missing or invalid")
        return 1
    tracker = load_json(TRACKER_PATH, {"processed": {}, "contacted_domains": []})
    tracker.setdefault("processed", {})
    tracker.setdefault("contacted_domains", [])

    dry_run = os.getenv("DRY_RUN", "true").strip().lower() != "false"
    timeout = cfg["limits"]["request_timeout"]
    print(f"[agent] mode: {'DRY RUN (no emails will be sent)' if dry_run else 'LIVE'}")

    qcfg = cfg.get("qualification", {})
    fresh = [l for l in collect_leads(cfg) if l["id"] not in tracker["processed"]]
    print(f"[agent] {len(fresh)} new keyword-matching candidates")

    stats = {"candidates": len(fresh), "checked": 0, "rejected": 0, "approved": 0, "emails": 0}
    emails_sent = 0
    for lead in fresh[: qcfg.get("max_per_run", 25)]:
        if stats["approved"] >= cfg["limits"]["max_leads_per_run"]:
            break

        # 1. Strict qualification: only real startups / SaaS / products with a visible UI need pass.
        lead["website"] = resolve_website(lead, timeout)
        if lead["website"] and qcfg.get("enrich_site", True):
            lead["site_info"] = site_blurb(lead["website"], timeout)
        lead["analysis"] = qualify(lead, cfg)
        stats["checked"] += 1
        if not lead["analysis"]["relevant"]:
            stats["rejected"] += 1
            tracker["processed"][lead["id"]] = {
                "name": lead["name"],
                "status": "rejected",
                "score": lead["analysis"]["score"],
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            save_tracker(tracker)
            print(f"[agent] {lead['name']}: rejected ({lead['analysis']['score']}/10)")
            continue

        # 2. Approved: find a public contact, write the pitch, send / preview.
        lead["email"] = find_contact_email(lead["website"], timeout) if lead["website"] else None
        pitch = write_pitch(lead, cfg)

        status = "alert_only"
        if lead["email"]:
            domain = lead["email"].split("@", 1)[1]
            if domain in tracker["contacted_domains"]:
                status = "domain_already_contacted"
            elif emails_sent >= cfg["limits"]["max_emails_per_run"]:
                status = "email_cap_reached"
            elif dry_run:
                status = "dry_run"
            else:
                try:
                    send_email(lead["email"], pitch["subject"], pitch["body"], cfg)
                    status = "sent"
                    emails_sent += 1
                    tracker["contacted_domains"].append(domain)
                except Exception as exc:  # noqa: BLE001 - record and continue with other leads
                    print(f"[email] failed for {lead['name']}: {type(exc).__name__}")
                    status = "email_failed"

        send_alert(lead, pitch, status, dry_run)
        stats["approved"] += 1
        stats["emails"] = emails_sent
        # Failed emails stay unprocessed so the next run retries them; everything else is final.
        if status != "email_failed":
            tracker["processed"][lead["id"]] = {
                "name": lead["name"],
                "status": status,
                "score": lead["analysis"]["score"],
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            save_tracker(tracker)
        print(f"[agent] {lead['name']}: {status}")

    if stats["checked"]:
        send_report(stats, dry_run)
    print(f"[agent] done: {stats}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # product names may contain non-cp1251 characters on Windows
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(run())
