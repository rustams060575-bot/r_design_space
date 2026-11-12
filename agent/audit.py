"""james · reach audit: why do posts show no views? Read-only diagnostics against the Graph API (never publishes).

    python agent/scraper.py --action audit

Collects: account basics (followers, type), the last media with reach / views / likes, public visibility of every
permalink (anonymous request), the publishing limit, Facebook Page basics and recent post impressions. Prints one JSON
report (no tokens) and sends a short summary to Telegram.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from meta_publisher import Graph  # noqa: E402

PAGE_METRICS = "post_impressions,post_impressions_unique,post_reactions_like_total"


def safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as exc:  # noqa: BLE001 - the audit reports problems, it never crashes on one
        return {"error": str(exc)[:200]}


def public_status(url: str) -> str:
    """What an anonymous visitor gets for a permalink: ok / login wall / not found."""
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
    except requests.RequestException:
        return "unreachable"
    if r.status_code == 404:
        return "not found (404)"
    return "public (200)" if r.status_code == 200 and "login" not in r.url else f"http {r.status_code}"


IG_METRICS = ["reach", "views", "saved", "shares", "likes", "comments", "total_interactions"]


def media_insights(g: Graph, media_id: str) -> dict | str:
    """Every metric is asked on its own: one unsupported metric must not hide the others."""
    out: dict = {}
    first_error = None
    for metric in IG_METRICS:
        res = safe(g._call, "GET", f"{media_id}/insights", g.ig_token, metric=metric)
        if "data" in res and res["data"]:
            out[metric] = (res["data"][0].get("values") or [{}])[0].get("value")
        elif first_error is None:
            first_error = res.get("error")
    if not out and first_error and "(#10)" in first_error:
        return "нет права instagram_manage_insights: добавьте его в токен"
    return out or (first_error or "нет данных")


def _try(label, fn):
    """Full Meta error (code + message, never a token) or the result."""
    try:
        return {label: fn()}
    except Exception as exc:  # noqa: BLE001
        return {label: {"error": re.sub(r"\s+", " ", str(exc))[:600]}}


def diagnose(g: Graph) -> dict:
    """Read-only: which identities and permissions the CI secrets carry and which IG/FB account is the publishing target.
    Prints names, usernames, permission names and match flags only: no token and no secret value."""
    out: dict = {"secrets_present": {
        "IG_USER_ID": bool(g.ig_id), "FB_PAGE_ID": bool(g.fb_id), "ACCESS_TOKEN": bool(g.user_token),
        "IG_ACCESS_TOKEN": bool(os.getenv("IG_ACCESS_TOKEN")), "FB_PAGE_TOKEN": bool(g.fb_token),
        "META_PUBLISH_LIVE": os.getenv("META_PUBLISH_LIVE") == "1"}}
    tokens = {"ACCESS_TOKEN": g.user_token, "IG_ACCESS_TOKEN": os.getenv("IG_ACCESS_TOKEN", ""), "FB_PAGE_TOKEN": g.fb_token}
    for name, tok in tokens.items():
        if not tok:
            continue
        out.update(_try(f"{name}.me", lambda t=tok: {k: v for k, v in g._call("GET", "me", t, fields="id,name").items() if k == "name"}))
        out.update(_try(f"{name}.permissions", lambda t=tok: sorted(
            f"{p['permission']}:{p['status']}" for p in g._call("GET", "me/permissions", t).get("data", []))))
    # which Page / IG account sit behind the user token
    out.update(_try("discover", g.discover) if g.user_token or g.ig_token else {})
    # the target: Facebook Page behind FB_PAGE_ID and the Instagram account linked to it
    if g.fb_id and g.fb_token:
        def page():
            r = g._call("GET", g.fb_id, g.fb_token, fields="name,link,username,instagram_business_account{id,username}")
            ig = r.get("instagram_business_account") or {}
            return {"page_name": r.get("name"), "page_username": r.get("username"), "page_link": r.get("link"),
                    "linked_ig_username": ig.get("username"), "IG_USER_ID_matches_linked_ig": bool(ig.get("id")) and ig.get("id") == g.ig_id}
        out.update(_try("target_page", page))
    if g.ig_id:
        for name in ("IG_ACCESS_TOKEN", "FB_PAGE_TOKEN"):
            if tokens.get(name):
                out.update(_try(f"ig_account_via_{name}", lambda t=tokens[name]: {
                    k: v for k, v in g._call("GET", g.ig_id, t, fields="username,name,website").items() if k != "id"}))
    return out


def instagram(g: Graph) -> dict:
    out: dict = {}
    out["account"] = safe(g._call, "GET", g.ig_id, g.ig_token,
                          fields="username,name,followers_count,follows_count,media_count,biography,website")
    out["publishing_limit"] = safe(g._call, "GET", f"{g.ig_id}/content_publishing_limit", g.ig_token,
                                   fields="quota_usage,config")
    media = safe(g._call, "GET", f"{g.ig_id}/media", g.ig_token,
                 fields="id,media_type,media_product_type,timestamp,permalink,like_count,comments_count", limit=20)
    items = []
    for m in media.get("data", []) if isinstance(media, dict) else []:
        row = {k: m.get(k) for k in ("media_product_type", "media_type", "timestamp", "like_count", "comments_count", "permalink")}
        row["insights"] = media_insights(g, m["id"])
        row["public"] = public_status(m["permalink"]) if m.get("permalink") else "n/a"
        items.append(row)
    out["media"] = items
    # account-level reach for the last days (needs insights permission; the error text itself is a finding)
    out["account_reach_7d"] = safe(g._call, "GET", f"{g.ig_id}/insights", g.ig_token, metric="reach,views", period="day",
                                   metric_type="total_value")
    return out


def facebook(g: Graph) -> dict:
    out: dict = {}
    token = g.fb_token or g.user_token
    out["page"] = safe(g._call, "GET", g.fb_id, token, fields="name,username,link,website,fan_count,followers_count,is_published,verification_status")
    posts = safe(g._call, "GET", f"{g.fb_id}/posts", token, fields="id,created_time,permalink_url,is_published", limit=10)
    items = []
    for p in posts.get("data", []) if isinstance(posts, dict) else []:
        row = {"created": p.get("created_time"), "published": p.get("is_published"), "url": p.get("permalink_url")}
        row["reach"] = None
        for metric in ("post_total_media_view_unique", "post_media_view", "post_impressions_unique"):
            ins = safe(g._call, "GET", f"{p['id']}/insights", token, metric=metric)
            if "data" in ins and ins["data"]:
                row["reach"] = ((ins["data"][0].get("values") or [{}])[0].get("value"))
                break
            row["reach"] = ins.get("error")
        row["public"] = public_status(p["permalink_url"]) if p.get("permalink_url") else "n/a"
        items.append(row)
    out["posts"] = items
    return out


def summary(report: dict) -> str:
    ig, fb = report.get("instagram", {}), report.get("facebook", {})
    acc = ig.get("account", {})
    lines = ["Аудит охватов (read-only):",
             f"IG @{acc.get('username')}: подписчиков {acc.get('followers_count')}, публикаций {acc.get('media_count')}"]
    for m in ig.get("media", [])[:8]:
        i = m.get("insights")
        i = i if isinstance(i, dict) else {"err": str(i)[:60]}
        lines.append(f"- {m.get('media_product_type')} {str(m.get('timestamp'))[:16]}: reach {i.get('reach')}, views {i.get('views')}, saves {i.get('saved')}, shares {i.get('shares')}, likes {m.get('like_count')}")
    pg = fb.get("page", {})
    lines.append(f"FB {pg.get('name')}: подписчиков {pg.get('followers_count')}, опубликована {pg.get('is_published')}")
    for p in fb.get("posts", [])[:5]:
        lines.append(f"- {str(p.get('created'))[:16]}: reach {p.get('reach')}, {p.get('public')}")
    return "\n".join(lines)


def run_audit(cfg: dict | None = None) -> int:
    g = Graph()
    report = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "instagram": instagram(g) if g.has("instagram") else {"error": "no instagram credentials"},
              "facebook": facebook(g) if g.has("facebook") else {"error": "no facebook credentials"}}
    report["diagnose"] = diagnose(g)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(re.sub(r"access_token=[^&\"\s]+", "access_token=***", text))
    try:
        from telegram_bot import send_text
        send_text(summary(report))
    except Exception as exc:  # noqa: BLE001
        print("[audit] telegram failed:", type(exc).__name__)
    return 0
