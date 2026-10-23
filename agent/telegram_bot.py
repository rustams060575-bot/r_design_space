"""Telegram alerts: tells Rustam about every processed lead together with the generated pitch."""
from __future__ import annotations

import os

import requests

STATUS_LABEL = {
    "sent": "EMAIL SENT",
    "dry_run": "DRY RUN (email not sent)",
    "alert_only": "NO CONTACT EMAIL FOUND - reach out manually",
    "email_failed": "EMAIL FAILED - check logs",
    "domain_already_contacted": "DOMAIN ALREADY CONTACTED",
    "email_cap_reached": "EMAIL CAP REACHED FOR THIS RUN",
}


def build_message(lead: dict, pitch: dict, status: str) -> str:
    lines = [
        f"New lead [{lead['source']}]: {lead['name']}",
        f"Status: {STATUS_LABEL.get(status, status)}",
        f"Keywords: {', '.join(lead.get('keywords', [])) or '-'}",
        f"Listing: {lead.get('url') or '-'}",
        f"Website: {lead.get('website') or '-'}",
        f"Contact: {lead.get('email') or '-'}",
    ]
    if lead.get("summary"):
        lines += ["", lead["summary"][:300]]
    lines += ["", f"Subject: {pitch['subject']}", "", pitch["body"]]
    return "\n".join(lines)


def send_alert(lead: dict, pitch: dict, status: str, timeout: int = 15) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - alert skipped")
        return False

    text = build_message(lead, pitch, status)[:4000]  # Telegram limit is 4096 chars
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=timeout,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as exc:
        # Never print the URL: it contains the bot token.
        print(f"[telegram] send failed: {type(exc).__name__}")
        return False
