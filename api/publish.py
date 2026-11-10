"""Vercel serverless function (Python): publish to Instagram and the Facebook Page through the Meta Graph API.

    GET  /api/publish?action=discover          -> which Page / Instagram business account the token sees, missing permissions
    POST /api/publish                          -> publish now: {"kind": "reel|image|carousel|post", "media_url": "...",
                                                  "media_urls": [...], "caption": "...", "platforms": ["instagram","facebook"],
                                                  "confirm": true}
    POST /api/publish {"action": "finish", "container": "<id>"}   -> finish an Instagram container that was still processing

Auth: header X-Publish-Key (or ?key=) = PUBLISH_KEY (preferred) or the key derived from TELEGRAM_BOT_TOKEN.
Without META_PUBLISH_LIVE=1 every publish is a dry run. Two-step Instagram publishing (container -> status -> media_publish)
lives in agent/meta_publisher.py. Environment: ACCESS_TOKEN (or IG_ACCESS_TOKEN / FB_PAGE_TOKEN), IG_USER_ID, FB_PAGE_ID,
META_PUBLISH_LIVE, TELEGRAM_BOT_TOKEN. Tokens are never logged or returned.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
import meta_publisher as mp  # noqa: E402

MAX_BODY = 64 * 1024
POLL_BUDGET = {"META_POLL_TRIES": "7", "META_POLL_SECONDS": "5"}  # ~35 s, inside the 60 s function limit


def authorised(key: str) -> bool:
    """PUBLISH_KEY (explicit secret) wins; otherwise the key derived from TELEGRAM_BOT_TOKEN."""
    expected = os.environ.get("PUBLISH_KEY", "") or (mp.derived_key(os.environ["TELEGRAM_BOT_TOKEN"]) if os.environ.get("TELEGRAM_BOT_TOKEN") else "")
    return bool(expected and key) and hmac.compare_digest(expected, key)


def graph() -> "mp.Graph":
    return mp.Graph({**os.environ, **{k: v for k, v in POLL_BUDGET.items() if k not in os.environ}})


def handle_publish(body: dict) -> tuple[int, dict]:
    g = graph()
    if body.get("action") == "finish":
        if not g.has("instagram") or not g.live:
            return 200, {"ok": True, "dry_run": True}
        try:
            return 200, {"ok": True, "instagram": f"published:{g.finish_instagram(str(body.get('container', '')))}"}
        except RuntimeError as exc:
            return 202 if "still processing" in str(exc) else 502, {"ok": False, "error": str(exc)}

    if body.get("confirm") is not True:
        return 400, {"ok": False, "error": "set confirm=true: publishing is outward-facing"}
    item = {
        "kind": body.get("kind", "reel"),
        "platforms": body.get("platforms") or ["instagram", "facebook"],
        "media_url": body.get("media_url", ""),
        "media_urls": body.get("media_urls") or [],
        "caption": str(body.get("caption", "")),
        "publish_at": datetime.now(timezone.utc).isoformat(),
    }
    problems = mp.validate_item(item)
    if problems:
        return 400, {"ok": False, "error": "; ".join(problems)}
    missing = [p for p in item["platforms"] if not g.has(p)]
    if missing:
        return 400, {"ok": False, "error": f"credentials not set for: {', '.join(missing)}"}
    if not g.live:
        return 200, {"ok": True, "dry_run": True, "would_publish": {"kind": item["kind"], "platforms": item["platforms"]}}

    result: dict = {"ok": True}
    status = 200
    try:
        if "instagram" in item["platforms"]:
            container = g.create_instagram_container(item)
            if g.wait_ready(container):
                result["instagram"] = f"published:{g._call('POST', f'{g.ig_id}/media_publish', g.ig_token, creation_id=container)['id']}"
            else:
                result["instagram"] = {"status": "processing", "container": container}  # finish later with action=finish
                status = 202
        if "facebook" in item["platforms"]:
            result["facebook"] = f"published:{g.publish_facebook(item)}"
    except RuntimeError as exc:
        return 502, {**result, "ok": False, "error": str(exc)}
    return status, result


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _key(self) -> str:
        return self.headers.get("X-Publish-Key") or (parse_qs(urlparse(self.path).query).get("key") or [""])[0]

    def do_GET(self):  # noqa: N802
        if not authorised(self._key()):
            return self._send(401, {"ok": False})
        if (parse_qs(urlparse(self.path).query).get("action") or [""])[0] != "discover":
            return self._send(400, {"ok": False, "error": "use ?action=discover"})
        try:
            return self._send(200, {"ok": True, **graph().discover()})
        except RuntimeError as exc:
            return self._send(502, {"ok": False, "error": str(exc)})

    def do_POST(self):  # noqa: N802
        if not authorised(self._key()):
            return self._send(401, {"ok": False})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"ok": False})
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"ok": False, "error": "invalid JSON"})
        code, payload = handle_publish(body)
        return self._send(code, payload)
