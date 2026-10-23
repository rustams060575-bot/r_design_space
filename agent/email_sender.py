"""Gmail SMTP sender. Authenticates with the GMAIL_APP_PASSWORD environment variable."""
from __future__ import annotations

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def build_footer(cfg: dict) -> str:
    owner = cfg["owner"]
    parts = [
        "--",
        f"{owner['name']} | UI/UX & Interactive Web Developer",
        f"Telegram: {owner['telegram_handle']} ({owner['telegram_url']})",
    ]
    if cfg.get("vercel_url"):
        parts.append(f"Portfolio: {cfg['vercel_url']}")
    if owner.get("postal_address"):
        parts.append(owner["postal_address"])
    parts += [
        "",
        "I found your project through a public listing and am writing once. "
        "If this is not relevant, just reply \"unsubscribe\" and I will not contact you again.",
    ]
    return "\n".join(parts)


def send_email(to_addr: str, subject: str, body: str, cfg: dict) -> None:
    """Send one plain-text email. Raises on any failure so the caller can record it."""
    if not EMAIL_RE.match(to_addr):
        raise ValueError(f"invalid recipient address: {to_addr!r}")

    password = os.getenv("GMAIL_APP_PASSWORD")
    if not password:
        raise RuntimeError("GMAIL_APP_PASSWORD environment variable is not set")

    sender = cfg["owner"]["email"].lower()
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["owner"]["name"], sender))
    msg["To"] = to_addr
    msg["Reply-To"] = sender
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    msg.set_content(f"{body.rstrip()}\n\n{build_footer(cfg)}\n")

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(sender, password.replace(" ", ""))  # app passwords are often pasted with spaces
        smtp.send_message(msg)
