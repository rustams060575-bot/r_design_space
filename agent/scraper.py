"""Lead hunter and pipeline entry point.

Actions (CLI flag --action, or the ACTION environment variable set by GitHub Actions):

  search  Collect fresh startups / SaaS launches worldwide (Product Hunt, Hacker News, regional news in many
          languages via Google News RSS, startup media feeds), qualify them strictly, write a pitch in the
          founder's language and send Telegram cards. Emails wait for a confirmation tap by default.
  send    Send the email for one confirmed lead (--lead-id / LEAD_ID) from agent/pending.json.

Optional --query / SEARCH_QUERY narrows a search to the user's own request (Telegram: /search <query>).
Emails are really sent only when DRY_RUN is exactly "false".
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from email_sender import EMAIL_RE, send_email  # noqa: E402
from pitch_writer import write_pitch  # noqa: E402
from qualifier import is_noise, qualify  # noqa: E402
from telegram_bot import send_alert, send_digest, send_report, send_text  # noqa: E402

CONFIG_PATH = ROOT / "config.json"
TRACKER_PATH = ROOT / "tracker.json"
PENDING_PATH = ROOT / "pending.json"
PENDING_TTL_DAYS = 14
UA = "Mozilla/5.0 (compatible; RSpaceOutreachBot/1.0; +https://t.me/r_design_space)"
AGGREGATOR_HOSTS = ("producthunt.com", "ycombinator.com", "news.ycombinator.com", "indiehackers.com",
                    "twitter.com", "x.com", "linkedin.com", "facebook.com", "github.com", "apple.com",
                    "google.com", "youtube.com", "instagram.com")
PREFERRED_LOCALS = ("hello", "contact", "founders", "founder", "team", "hi", "info", "support", "sales")
JUNK_EMAIL = re.compile(r"(example\.|sentry|wixpress|yourdomain|domain\.com|email\.com|\.(png|jpe?g|gif|svg|webp|js|css)$)", re.I)
FOUND_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LEAD_ID_RE = re.compile(r"^[a-z_]+:[A-Za-z0-9]{1,40}$")
JOB_SOURCES = ("jobboard",)  # real job / contract postings: relevant only with a design or front-end signal
NO_KEYWORD_GATE = ("news", "feed")  # regional sources are already topic-searched; the qualifier decides

_session = requests.Session()
_session.headers["User-Agent"] = UA
_robots_cache: dict[str, RobotFileParser | None] = {}


# ---------------------------------------------------------------- storage

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_tracker() -> dict:
    tracker = load_json(TRACKER_PATH, {})
    tracker.setdefault("processed", {})
    tracker.setdefault("contacted_domains", [])
    return tracker


def save_tracker(tracker: dict) -> None:
    save_json(TRACKER_PATH, tracker)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").strip().lower() != "false"


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


def query_terms(query: str | None) -> list[str]:
    if not query:
        return []
    terms = [t for t in re.split(r"[,\s]+", query) if len(t) >= 3]
    return [query, *terms] if len(terms) > 1 else terms or [query]


# ---------------------------------------------------------------- sources

def parse_feed(xml_text: str) -> list[dict]:
    """Minimal RSS/Atom parser (namespace agnostic)."""
    local = lambda tag: tag.rsplit("}", 1)[-1]
    items = []
    for el in ET.fromstring(xml_text).iter():
        if local(el.tag) not in ("entry", "item"):
            continue
        d = {"title": "", "link": "", "summary": "", "id": "", "date": ""}
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
            elif name in ("pubDate", "published", "updated") and not d["date"]:
                d["date"] = (ch.text or "").strip()
        items.append(d)
    return items


def fetch_feed(url: str, source: str, cfg: dict, region: str | None = None, lang: str | None = None,
               limit: int | None = None) -> list[dict]:
    r = get(url, cfg["limits"]["request_timeout"])
    if r is None:
        print(f"[scraper] {source}: feed unavailable ({host_of(url)})")
        return []
    try:
        entries = parse_feed(r.content)  # bytes: the XML declaration decides the encoding
    except ET.ParseError:
        print(f"[scraper] {source}: feed is not valid XML ({host_of(url)})")
        return []
    leads = []
    for e in entries[:limit]:
        if not e["link"]:
            continue
        title = e["title"]
        if source == "producthunt":
            name = title.split(" - ")[0].split(" – ")[0]
        elif source == "news":
            name = title.rsplit(" - ", 1)[0]  # Google News appends " - Publisher"
        else:
            name = title
        leads.append({
            "id": make_id(source, e["id"] or e["link"]),
            "source": source,
            "name": name.strip() or e["link"],
            "summary": e["summary"][:400],
            "url": e["link"],
            "website": None,
            "region": region,
            "lang_hint": lang,
        })
    return leads


def fetch_hackernews(cfg: dict, query: str | None = None) -> list[dict]:
    """Targeted Hacker News scan through the public Algolia API (search_by_date).

    Daily run: the last 24 hours (+ a small overlap so a delayed cron never leaves a gap; the tracker removes
    repeats) for Show HN and Ask HN, one search per keyword. With a custom query: that query over max_age_days.
    Show HN items carry the product's own website; Ask HN items are people asking for help (Telegram-only leads).
    """
    hn = cfg.get("hackernews", {})
    if query:
        hours = cfg["limits"]["max_age_days"] * 24
        searches = [("show_hn", query), ("ask_hn", query)]
    else:
        hours = hn.get("max_age_hours", cfg["limits"]["max_age_days"] * 24) + hn.get("overlap_hours", 0)
        terms = hn.get("keywords", [])
        show = list(dict.fromkeys(cfg["keywords"] + terms)) if cfg["sources"].get("hackernews_show") else list(terms)
        ask = list(dict.fromkeys(hn.get("ask_queries", []) + terms))
        searches = [("show_hn", q) for q in show] + [("ask_hn", q) for q in ask]
    cutoff = int(time.time() - hours * 3600)

    seen, leads = set(), []
    for tag, q in searches:
        r = get("https://hn.algolia.com/api/v1/search_by_date", cfg["limits"]["request_timeout"],
                params={"tags": tag, "query": q, "hitsPerPage": 20, "numericFilters": f"created_at_i>{cutoff}"})
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
                "region": "Hacker News",
                "lang_hint": "en",
            })
    return leads


def parse_date(value: str | None) -> datetime | None:
    """RFC 822 (RSS) or ISO 8601 (APIs) -> aware datetime; None when unknown."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fetch_job_boards(cfg: dict) -> list[dict]:
    """Fresh design / front-end postings from open job boards (public RSS and JSON APIs, no keys, no logins).

    Platforms without a public feed (Upwork, Toptal, LinkedIn, Wellfound, RocketPunch, TokyoDev) block automated
    access or forbid scraping in their terms, so they are not queried; see agent/README.md for the alternatives.
    Remotive and Jobicy require a visible link back to the source: every card links to the original posting.
    """
    jb = cfg.get("job_boards", {})
    if not jb.get("enabled"):
        return []
    timeout = cfg["limits"]["request_timeout"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["limits"]["max_age_days"])
    leads: list[dict] = []

    def add(platform: str, region: str, title: str, company: str, url: str, kind: str, place: str, text: str, date: str | None):
        posted = parse_date(date)
        if not url or (posted and posted < cutoff):
            return
        facts = " · ".join(x for x in (kind, place) if x)
        leads.append({
            "id": make_id("job", url.split("?")[0]),
            "source": "jobboard",
            "name": f"{title} - {company}" if company else title,
            "summary": strip_html(f"[{facts}] {text}" if facts else text)[:500],
            "url": url,
            "website": None,
            "region": f"{platform} · {region}" if region else platform,
            "lang_hint": "en",
        })

    for feed in jb.get("feeds", []):  # RSS: We Work Remotely, Dribbble Jobs, NoDesk
        r = get(feed["url"], timeout)
        if r is None:
            print(f"[jobs] {feed['name']}: unavailable")
            continue
        try:
            entries = parse_feed(r.content)
        except ET.ParseError:
            print(f"[jobs] {feed['name']}: not valid XML")
            continue
        for e in entries[: jb.get("per_source", 25)]:
            title, company = e["title"], ""
            if ":" in title and feed.get("company_prefix"):  # We Work Remotely: "Company: Role"
                company, title = [x.strip() for x in title.split(":", 1)]
            add(feed["name"], feed.get("region", "Remote"), title, company, e["link"], "", "", e["summary"], e["date"])

    for cat in jb.get("remotive_categories", []):  # Remotive JSON API
        r = get("https://remotive.com/api/remote-jobs", timeout, params={"category": cat, "limit": jb.get("per_source", 25)})
        for j in (r.json().get("jobs", []) if r is not None else []):
            add("Remotive", "Remote", j.get("title", ""), j.get("company_name", ""), j.get("url", ""),
                (j.get("job_type") or "").replace("_", " "), j.get("candidate_required_location", ""),
                j.get("description", ""), j.get("publication_date"))

    for q in jb.get("jobicy", []):  # Jobicy JSON API, per tag and region (APAC covers Japan, Korea, China-facing brands)
        r = get("https://jobicy.com/api/v2/remote-jobs", timeout,
                params={"count": jb.get("per_source", 25), "tag": q["tag"], "geo": q["geo"]})
        for j in (r.json().get("jobs", []) if r is not None else []):
            add("Jobicy", q.get("label", q["geo"].upper()), j.get("jobTitle", ""), j.get("companyName", ""), j.get("url", ""),
                "/".join(j.get("jobType") or []), j.get("jobGeo", ""), j.get("jobExcerpt", ""), j.get("pubDate"))
        time.sleep(0.3)
    print(f"[scraper] job boards: {len(leads)} postings")
    return leads


def google_news_url(query: str, region: dict, days: int) -> str:
    q = quote_plus(f"{query} when:{days}d", safe=":")
    return (f"https://news.google.com/rss/search?q={q}&hl={region['hl']}&gl={region['gl']}"
            f"&ceid={quote_plus(region['ceid'], safe=':')}")


def fetch_global_news(cfg: dict, query: str | None = None) -> list[dict]:
    """Regional startup news in many languages through Google News RSS (no key needed)."""
    gs = cfg.get("global_search", {})
    if not gs.get("enabled"):
        return []
    leads = []
    for region in gs.get("regions", []):
        for q in ([query] if query else region.get("queries", [])):
            leads += fetch_feed(google_news_url(q, region, gs.get("max_age_days", 30)), "news", cfg,
                                region=region["name"], lang=region["lang"], limit=gs.get("max_items_per_query", 6))
            time.sleep(0.3)
    return leads


def interleave(leads: list[dict]) -> list[dict]:
    """Strongest first inside each source/region group, then round-robin across groups.

    The qualification budget per run is limited; round-robin keeps every region and source in play.
    """
    groups: dict[tuple, list[dict]] = {}
    for lead in sorted(leads, key=lambda l: -(len(l["keywords"]) * 2 + len(l["signals"]))):
        groups.setdefault((lead["source"], lead.get("region")), []).append(lead)
    ordered, queues = [], list(groups.values())
    while queues:
        queues = [q for q in queues if q]
        for q in queues:
            ordered.append(q.pop(0))
    return ordered


def collect_leads(cfg: dict, query: str | None = None) -> list[dict]:
    src = cfg["sources"]
    leads: list[dict] = []
    if src.get("producthunt_feed"):
        leads += fetch_feed(src["producthunt_feed"], "producthunt", cfg, region="Product Hunt", lang="en")
    if src.get("hackernews_show") or cfg.get("hackernews", {}).get("ask_queries") or query:
        leads += fetch_hackernews(cfg, query)
    leads += fetch_global_news(cfg, query)
    if not query:  # job boards are scanned in the daily run; a custom /search stays on its own query
        leads += fetch_job_boards(cfg)
    if not query:  # general startup media feeds are only useful for the broad daily scan
        for feed in src.get("extra_feeds", []):
            feed = {"url": feed} if isinstance(feed, str) else feed
            leads += fetch_feed(feed["url"], "feed", cfg, region=feed.get("region"), lang=feed.get("lang"),
                                limit=src.get("feed_limit", 20))

    keywords = cfg["keywords"] + cfg.get("hackernews", {}).get("keywords", []) + query_terms(query)
    signals = cfg.get("design_signals", [])
    unique, noise = {}, 0
    for lead in leads:
        if is_noise(lead):  # funding, investment, rounds, startup news: never a lead
            noise += 1
            continue
        text = f"{lead['name']} {lead['summary']}"
        hits, sig = keyword_hits(text, keywords), keyword_hits(text, signals)
        relevant = (bool(hits)
                    or lead["source"] in NO_KEYWORD_GATE
                    or (lead["source"] in ("hackernews", "hackernews_ask") + JOB_SOURCES and bool(sig)))
        if relevant and lead["id"] not in unique:
            lead["keywords"], lead["signals"] = hits, sig
            unique[lead["id"]] = lead
    print(f"[scraper] dropped {noise} funding / investment / news posts before qualification")
    return interleave(list(unique.values()))


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


# ---------------------------------------------------------------- pipeline: search

def run_search(cfg: dict, query: str | None = None) -> dict:
    tracker = load_tracker()
    pending = load_json(PENDING_PATH, {})
    dry_run = is_dry_run()
    timeout = cfg["limits"]["request_timeout"]
    qcfg = cfg.get("qualification", {})
    confirm = cfg.get("outreach", {}).get("require_confirmation", True)
    print(f"[agent] mode: {'DRY RUN' if dry_run else 'LIVE'}; confirmation: {confirm}; query: {query!r}")

    # drop stale pending drafts
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PENDING_TTL_DAYS)).isoformat()
    pending = {k: v for k, v in pending.items() if v.get("created", "") >= cutoff}

    fresh = [l for l in collect_leads(cfg, query) if l["id"] not in tracker["processed"]]
    print(f"[agent] {len(fresh)} new candidates")
    if not query and os.getenv("GITHUB_EVENT_NAME", "") == "schedule":
        send_digest(fresh, dry_run)  # morning header: what was scanned today

    stats = {"candidates": len(fresh), "checked": 0, "rejected": 0, "approved": 0, "emails": 0}
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
            tracker["processed"][lead["id"]] = {"name": lead["name"], "status": "rejected",
                                                "score": lead["analysis"]["score"], "at": now_iso()}
            save_tracker(tracker)
            print(f"[agent] {lead['name']}: rejected ({lead['analysis']['score']}/10)")
            continue

        # 2. Approved: find a public contact, write the pitch in the founder's language.
        lead["email"] = find_contact_email(lead["website"], timeout) if lead["website"] else None
        pitch = write_pitch(lead, cfg)

        status = "alert_only"
        if lead["email"]:
            domain = lead["email"].split("@", 1)[1]
            if domain in tracker["contacted_domains"]:
                status = "domain_already_contacted"
            elif confirm:
                status = "pending"  # waits for the "Send" button in Telegram
                pending[lead["id"]] = {"name": lead["name"], "email": lead["email"], "domain": domain,
                                       "pitch": pitch, "created": now_iso()}
                save_json(PENDING_PATH, pending)
            elif stats["emails"] >= cfg["limits"]["max_emails_per_run"]:
                status = "email_cap_reached"
            elif dry_run:
                status = "dry_run"
            else:
                try:
                    send_email(lead["email"], pitch["subject"], pitch["body"], cfg, pitch["language"])
                    status = "sent"
                    stats["emails"] += 1
                    tracker["contacted_domains"].append(domain)
                except Exception as exc:  # noqa: BLE001 - record and continue with other leads
                    print(f"[email] failed for {lead['name']}: {type(exc).__name__}")
                    status = "email_failed"

        send_alert(lead, pitch, status, dry_run)
        stats["approved"] += 1
        # Failed emails stay unprocessed so the next run retries them; everything else is final.
        if status != "email_failed":
            tracker["processed"][lead["id"]] = {"name": lead["name"], "status": status,
                                                "score": lead["analysis"]["score"], "at": now_iso()}
            save_tracker(tracker)
        print(f"[agent] {lead['name']}: {status} ({pitch['language']})")

    save_json(PENDING_PATH, pending)
    # Scheduled runs stay quiet when there is nothing to say; manual runs always answer.
    if stats["checked"] or os.getenv("GITHUB_EVENT_NAME", "") != "schedule":
        send_report(stats, dry_run, query)
    print(f"[agent] done: {stats}")
    return stats


# ---------------------------------------------------------------- pipeline: confirmed send

def send_pending(cfg: dict, lead_id: str) -> bool:
    """Sends the stored draft for one lead after the user tapped the button in Telegram."""
    if not LEAD_ID_RE.match(lead_id or ""):
        print("[agent] invalid lead id")
        return False
    pending, tracker = load_json(PENDING_PATH, {}), load_tracker()
    item = pending.get(lead_id)
    if not item:
        send_text("ℹ️ Это письмо уже отправлено, пропущено или устарело.")
        return False
    if item["domain"] in tracker["contacted_domains"]:
        pending.pop(lead_id)
        save_json(PENDING_PATH, pending)
        send_text(f"⏭ Домену {item['domain']} уже писали - повторно не отправляю.")
        return False
    if is_dry_run():
        send_text(f"🧪 DRY_RUN включён: письмо для {item['name']} ({item['email']}) НЕ отправлено.\n"
                  "Чтобы отправлять по-настоящему, задайте в репозитории переменную DRY_RUN=false "
                  "(Settings → Secrets and variables → Actions → Variables). Черновик остаётся в очереди.")
        return True

    pitch = item["pitch"]
    try:
        send_email(item["email"], pitch["subject"], pitch["body"], cfg, pitch.get("language", "en"))
    except Exception as exc:  # noqa: BLE001
        print(f"[email] send failed: {type(exc).__name__}")
        send_text(f"⚠️ Не удалось отправить письмо для {item['name']} ({type(exc).__name__}). Черновик сохранён, "
                  "можно нажать кнопку ещё раз.")
        return False

    tracker["contacted_domains"].append(item["domain"])
    tracker["processed"][lead_id] = {**tracker["processed"].get(lead_id, {"name": item["name"]}),
                                     "status": "sent", "at": now_iso()}
    pending.pop(lead_id)
    save_tracker(tracker)
    save_json(PENDING_PATH, pending)
    send_text(f"✅ Письмо отправлено: {item['name']} → {item['email']} ({pitch.get('language', 'en').upper()})")
    return True


# ---------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--action", choices=["search", "send"])
    ap.add_argument("--query")
    ap.add_argument("--lead-id")
    args = ap.parse_args(argv)

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("config.json is missing or invalid")
        return 1

    action = args.action or os.getenv("ACTION") or "search"
    if action == "send":
        return 0 if send_pending(cfg, (args.lead_id or os.getenv("LEAD_ID", "")).strip()) else 1

    raw_query = args.query if args.query is not None else os.getenv("SEARCH_QUERY", "")
    query = re.sub(r"[\x00-\x1f]", " ", raw_query).strip()[:100] or None
    run_search(cfg, query)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # product names may contain non-cp1251 characters on Windows
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
