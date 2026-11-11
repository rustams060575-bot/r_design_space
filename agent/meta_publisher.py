"""james · publishing to Instagram (@ar_design_agency) and the Facebook Page through the Meta Graph API.

Flow: concept (reels.json) -> queue item -> owner approval -> due time -> publish.

    python agent/meta_publisher.py add --concept <id> --video-url https://... --at 2026-11-12T16:00:00Z [--platforms instagram,facebook] [--kind reel|image|post]
    python agent/meta_publisher.py approve <queue-id>
    python agent/meta_publisher.py list
    python agent/meta_publisher.py run            # also: python agent/scraper.py --action publish (GitHub Actions / cron)

What the Meta APIs really allow (so the logic matches them):
  * Instagram Reels / images: POST /{ig-user-id}/media (container) -> poll status_code -> POST /{ig-user-id}/media_publish.
    The Instagram Content Publishing API has NO native scheduling, so AR Design schedules in this queue: the job runs
    periodically and publishes every approved item whose time has come.
  * Facebook Page videos / posts: native scheduling exists (published=false + scheduled_publish_time, 10 min to 30 days ahead),
    used when the item is approved ahead of its time.
  * The video must be a public https URL (Meta downloads it); a reel is not uploaded from a local file.

Safety: nothing publishes without status "approved" (set by the owner); with no tokens, or without META_PUBLISH_LIVE=1,
the run is a dry run that only prints what it would do. Tokens come from environment variables and are never printed.
Environment: IG_USER_ID, IG_ACCESS_TOKEN (scopes instagram_content_publish, instagram_basic, pages_show_list),
FB_PAGE_ID, FB_PAGE_TOKEN (pages_manage_posts, pages_read_engagement), IG_API_VERSION (default v21.0), META_PUBLISH_LIVE=1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
QUEUE_PATH = ROOT.parent / "content-hub" / "publish_queue.json"
GRAPH = "https://graph.facebook.com"
FB_MIN_AHEAD, FB_MAX_AHEAD = timedelta(minutes=10), timedelta(days=30)
PLATFORMS = ("instagram", "facebook")
KINDS = ("reel", "image", "post", "carousel", "story")
PUBLISH_SCOPES = ("instagram_basic", "instagram_content_publish", "pages_show_list", "pages_manage_posts", "pages_read_engagement")
INSIGHTS_SCOPES = ("instagram_manage_insights", "read_insights", "pages_read_engagement")  # reach / views / saves for the stats report
STATUS_POLL_SECONDS, STATUS_POLL_TRIES = 6, 30


# ---------------------------------------------------------------- queue storage

def load_queue(path: Path = QUEUE_PATH) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_queue(items: list[dict], path: Path = QUEUE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- caption and item validation

def build_caption(concept: dict) -> str:
    from growth import normalize_brand
    tags = " ".join(concept.get("hashtags", [])[:30])
    return normalize_brand(f"{concept['caption'].strip()}\n\n{tags}".strip())


def validate_item(item: dict) -> list[str]:
    """Returns a list of problems; an item with problems is never published."""
    problems = []
    if item.get("kind") not in KINDS:
        problems.append("kind must be reel, image or post")
    if not set(item.get("platforms", [])) <= set(PLATFORMS) or not item.get("platforms"):
        problems.append("platforms must be instagram and/or facebook")
    if item.get("kind") == "carousel":
        urls = item.get("media_urls") or []
        if not 2 <= len(urls) <= 10 or not all(str(u).startswith("https://") for u in urls):
            problems.append("carousel needs 2-10 public https image URLs in media_urls")
    elif item.get("kind") != "post" and not str(item.get("media_url", "")).startswith("https://"):
        problems.append("media_url must be a public https URL")
    caption = item.get("caption", "")
    if (not caption and item.get("kind") != "story") or len(caption) > 2200:
        problems.append("caption is empty or longer than 2200 characters")
    if len(re.findall(r"#\w+", caption)) > 30:
        problems.append("more than 30 hashtags")
    if re.search(r"\bAR[ _]Design[ _]Space\b", caption, re.I):
        problems.append("old brand name in caption")
    try:
        parse_time(item["publish_at"])
    except (KeyError, ValueError):
        problems.append("publish_at must be an ISO time")
    return problems


# ---------------------------------------------------------------- Graph API client

class Graph:
    def __init__(self, env=os.environ, session=None):
        self.v = env.get("IG_API_VERSION") or "v21.0"
        # ACCESS_TOKEN (a user token) is the generic fallback; a dedicated Page token is preferred for Facebook.
        generic = env.get("ACCESS_TOKEN", "")
        self.user_token = generic
        self.ig_id = env.get("IG_USER_ID", "")
        self.fb_id, self.fb_token = env.get("FB_PAGE_ID", ""), env.get("FB_PAGE_TOKEN", "")
        self.ig_token = env.get("IG_ACCESS_TOKEN") or self.fb_token or generic
        self.live = env.get("META_PUBLISH_LIVE") == "1"
        self.ig_location_id = env.get("IG_LOCATION_ID", "")
        self.poll_tries = int(env.get("META_POLL_TRIES") or STATUS_POLL_TRIES)
        self.poll_seconds = int(env.get("META_POLL_SECONDS") or STATUS_POLL_SECONDS)
        self.http = session or requests

    def has(self, platform: str) -> bool:
        if platform == "instagram":
            return bool(self.ig_id and self.ig_token)
        return bool(self.fb_id and (self.fb_token or self.user_token))

    def _call(self, method: str, path: str, token: str, **params) -> dict:
        try:
            res = self.http.request(method, f"{GRAPH}/{self.v}/{path}", params={**params, "access_token": token}, timeout=60)
        except requests.RequestException as exc:  # the exception text can embed the URL (and the token): never pass it on
            raise RuntimeError(f"meta network error ({type(exc).__name__})") from None
        try:
            data = res.json()
        except ValueError:
            data = {}
        if res.status_code >= 400 or "error" in data:
            err = data.get("error", {})  # never log the request (it carries the token), only Meta's error summary
            raise RuntimeError(f"meta {res.status_code}: {err.get('message', 'unknown error')} (code {err.get('code')})")
        return data

    # discovery: which Page and which Instagram business account are behind this token (ids only, never tokens)
    def discover(self) -> dict:
        token = self.user_token or self.ig_token
        perms = self._call("GET", "me/permissions", token).get("data", [])
        granted = sorted(p["permission"] for p in perms if p.get("status") == "granted")
        pages = self._call("GET", "me/accounts", token, fields="id,name,instagram_business_account{id,username}").get("data", [])
        return {
            "granted": granted,
            "missing_for_publishing": [s for s in PUBLISH_SCOPES if s not in granted],
            "missing_for_insights": [s for s in INSIGHTS_SCOPES if s not in granted],
            "pages": [{"page_id": pg["id"], "page_name": pg.get("name"),
                       "ig_user_id": (pg.get("instagram_business_account") or {}).get("id"),
                       "ig_username": (pg.get("instagram_business_account") or {}).get("username")} for pg in pages],
        }

    def exchange_long_lived(self, app_id: str, app_secret: str) -> dict:
        """Short-lived user token -> long-lived (about 60 days). Page tokens derived from a long-lived user token do not expire."""
        data = self._call("GET", "oauth/access_token", f"{app_id}|{app_secret}", grant_type="fb_exchange_token",
                          client_id=app_id, client_secret=app_secret, fb_exchange_token=self.user_token)
        return {"access_token": data["access_token"], "expires_in": data.get("expires_in")}

    def page_token(self) -> str:
        """Facebook posting needs a Page token: FB_PAGE_TOKEN, or derived from the user token through /{page}?fields=access_token."""
        if not self.fb_token and self.fb_id and self.user_token:
            self.fb_token = self._call("GET", self.fb_id, self.user_token, fields="access_token").get("access_token", "")
        return self.fb_token

    # instagram: step 1 create a container, step 2 wait until FINISHED, step 3 media_publish
    def _container(self, params: dict) -> str:
        if self.ig_location_id and "is_carousel_item" not in params and params.get("media_type") != "STORIES":
            params = {**params, "location_id": self.ig_location_id}
        return self._call("POST", f"{self.ig_id}/media", self.ig_token, **params)["id"]

    def wait_ready(self, container: str) -> bool:
        """True when FINISHED; False if still processing after the poll budget (a reel can take a minute or more)."""
        for _ in range(self.poll_tries):
            state = self._call("GET", container, self.ig_token, fields="status_code").get("status_code")
            if state == "FINISHED":
                return True
            if state in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"instagram container {state}")
            time.sleep(self.poll_seconds)
        return False

    def finish_instagram(self, container: str) -> str:
        if not self.wait_ready(container):
            raise RuntimeError("instagram container still processing")
        return self._call("POST", f"{self.ig_id}/media_publish", self.ig_token, creation_id=container)["id"]

    def create_instagram_container(self, item: dict) -> str:
        caption = item["caption"]
        if item["kind"] == "reel":
            return self._container({"caption": caption, "media_type": "REELS", "video_url": item["media_url"], "share_to_feed": "true"})
        if item["kind"] == "story":
            return self._container({"media_type": "STORIES", "image_url": item["media_url"]})
        if item["kind"] == "carousel":
            children = [self._container({"image_url": u, "is_carousel_item": "true"}) for u in item["media_urls"]]
            for child in children:
                if not self.wait_ready(child):
                    raise RuntimeError("carousel item still processing")
            return self._container({"caption": caption, "media_type": "CAROUSEL", "children": ",".join(children)})
        return self._container({"caption": caption, "image_url": item["media_url"]})

    def publish_instagram(self, item: dict) -> str:
        return self.finish_instagram(self.create_instagram_container(item))

    # facebook page (native scheduling when `when` is given)
    def _fb_photo(self, url: str) -> str:
        """Uploads a photo to the Page without publishing it (used for stories and multi-photo posts)."""
        return self._call("POST", f"{self.fb_id}/photos", self.fb_token, url=url, published="false")["id"]

    def publish_facebook(self, item: dict, when: datetime | None = None) -> str:
        self.page_token()
        item = {**item, "caption": item.get("caption_fb") or item["caption"]}  # ar02: Facebook gets its own caption / media
        if item.get("media_url_fb"):
            item["media_url"] = item["media_url_fb"]
        if item.get("media_urls_fb"):
            item["media_urls"] = item["media_urls_fb"]
        if item["kind"] == "story":
            return self._call("POST", f"{self.fb_id}/photo_stories", self.fb_token, photo_id=self._fb_photo(item["media_url"]))["post_id"]
        if item["kind"] == "carousel":
            attached = {f"attached_media[{i}]": json.dumps({"media_fbid": self._fb_photo(u)}) for i, u in enumerate(item["media_urls"])}
            return self._call("POST", f"{self.fb_id}/feed", self.fb_token, message=item["caption"], **attached)["id"]
        sched = {"published": "false", "scheduled_publish_time": int(when.timestamp())} if when else {}
        if item["kind"] == "reel":
            return self._call("POST", f"{self.fb_id}/videos", self.fb_token, file_url=item["media_url"], description=item["caption"], **sched)["id"]
        if item["kind"] == "image":
            return self._call("POST", f"{self.fb_id}/photos", self.fb_token, url=item["media_url"], caption=item["caption"], **sched)["id"]
        return self._call("POST", f"{self.fb_id}/feed", self.fb_token, message=item["caption"], **sched)["id"]


def derived_key(token: str, purpose: str = "publish-trigger") -> str:
    """Secret for the Vercel endpoint, derived from TELEGRAM_BOT_TOKEN (same idea as api/cron.js); rotates with the token."""
    import base64
    import hashlib
    import hmac
    return base64.urlsafe_b64encode(hmac.new(token.encode(), purpose.encode(), hashlib.sha256).digest()).decode().rstrip("=")[:32]


# ---------------------------------------------------------------- queue operations

def add_item(items: list[dict], concept: dict, media_url: str, publish_at: str, platforms=PLATFORMS, kind="reel") -> dict:
    item = {
        "id": f"{concept['id']}-{len(items) + 1}",
        "concept_id": concept["id"],
        "kind": kind,
        "platforms": list(platforms),
        "media_url": media_url,
        "caption": build_caption(concept),
        "publish_at": publish_at,
        "status": "draft",  # the owner must approve: draft -> approved
        "results": {},
    }
    problems = validate_item(item)
    if problems:
        raise ValueError("; ".join(problems))
    items.append(item)
    return item


def run_queue(items: list[dict], graph: Graph, now: datetime | None = None) -> list[str]:
    """Publishes due approved items, natively schedules approved Facebook items that are 10 min-30 days ahead. Mutates items."""
    now = now or datetime.now(timezone.utc)
    log = []
    for item in items:
        if item.get("status") != "approved":
            continue
        problems = validate_item(item)
        if problems:
            item["status"], item["error"] = "failed", "; ".join(problems)
            log.append(f"{item['id']}: invalid ({item['error']})")
            continue
        due = parse_time(item["publish_at"])
        for platform in item["platforms"]:
            if item["results"].get(platform):
                continue
            if not graph.has(platform):
                log.append(f"{item['id']}/{platform}: skipped, credentials not set")
                continue
            try:
                if platform == "facebook" and due > now:
                    if not FB_MIN_AHEAD <= due - now <= FB_MAX_AHEAD or not graph.live:
                        log.append(f"{item['id']}/facebook: {'dry run' if not graph.live else 'too close/far for native schedule, waits'}")
                        continue
                    item["results"][platform] = f"scheduled:{graph.publish_facebook(item, due)}"
                elif due <= now:
                    if not graph.live:
                        log.append(f"{item['id']}/{platform}: DRY RUN would publish now (set META_PUBLISH_LIVE=1)")
                        continue
                    pid = graph.publish_instagram(item) if platform == "instagram" else graph.publish_facebook(item)
                    item["results"][platform] = f"published:{pid}"
                else:
                    continue  # instagram has no native scheduling: wait for the due time
                log.append(f"{item['id']}/{platform}: {item['results'][platform]}")
            except (RuntimeError, requests.RequestException) as exc:
                item["error"] = f"{platform}: {exc}"
                log.append(f"{item['id']}/{platform}: FAILED {exc}")
        if all(item["results"].get(p, "").startswith("published") for p in item["platforms"]):
            item["status"] = "published"
        elif item.get("error") and not any(item["results"].values()):
            item["status"] = "failed"
    return log


def run_publish(cfg: dict | None = None) -> int:
    items = load_queue()
    log = run_queue(items, Graph())
    save_queue(items)
    print("\n".join(log) or "[publish] nothing due")
    return 0


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--concept", required=True)
    a.add_argument("--video-url", required=True)
    a.add_argument("--at", required=True)
    a.add_argument("--platforms", default="instagram,facebook")
    a.add_argument("--week", default=None)
    a.add_argument("--kind", choices=KINDS, default="reel")
    ap_ = sub.add_parser("approve")
    ap_.add_argument("id")
    sub.add_parser("list")
    sub.add_parser("run")
    sub.add_parser("key")
    args = ap.parse_args(argv)
    if args.cmd == "key":  # secret for /api/publish (header X-Publish-Key)
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        print(derived_key(token) if token else "TELEGRAM_BOT_TOKEN is not set")
        return 0 if token else 1
    items = load_queue()
    if args.cmd == "add":
        from growth import iso_week
        path = ROOT.parent / "content-hub" / "weekly" / (args.week or iso_week()) / "reels.json"
        concept = next((c for c in json.loads(path.read_text(encoding="utf-8")) if c["id"] == args.concept), None)
        if not concept:
            print(f"concept {args.concept} not found in {path}")
            return 1
        print("queued as draft:", add_item(items, concept, args.video_url, args.at, args.platforms.split(","), args.kind)["id"])
    elif args.cmd == "approve":
        item = next((i for i in items if i["id"] == args.id), None)
        if not item or item["status"] != "draft":
            print("no draft with that id")
            return 1
        item["status"] = "approved"
        print("approved:", item["id"])
    elif args.cmd == "list":
        for i in items:
            print(i["id"], i["status"], i["publish_at"], i["platforms"], i["results"])
        return 0
    else:
        return run_publish()
    save_queue(items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
