"""Gmail SMTP sender. Authenticates with the GMAIL_APP_PASSWORD environment variable."""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def load_credentials(cfg: dict) -> tuple[str, str]:
    """Reads the Gmail login from the GMAIL_APP_PASSWORD secret.

    Accepted formats: a bare app password, "user@gmail.com:app-password", or
    JSON {"email": "...", "app_password": "..."}. Values are never printed.
    """
    raw = (os.getenv("GMAIL_APP_PASSWORD") or "").strip()
    if not raw:
        raise RuntimeError("GMAIL_APP_PASSWORD environment variable is not set")

    user, password = cfg["owner"]["email"], raw
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("GMAIL_APP_PASSWORD looks like JSON but is not valid") from exc
        user = data.get("email") or data.get("user") or user
        password = data.get("app_password") or data.get("password") or ""
    elif "@" in raw.split(":", 1)[0] and ":" in raw:
        user, password = raw.split(":", 1)
    if not password:
        raise ValueError("GMAIL_APP_PASSWORD does not contain a password")
    return user.strip().lower(), password.replace(" ", "")  # app passwords are often pasted with spaces


OPT_OUT = {  # one-time-contact notice + opt-out line, localized like the email body
    "en": 'I found your project through a public listing and am writing once. If this is not relevant, just reply "unsubscribe" and I will not contact you again.',
    "ru": 'Я нашёл ваш проект в открытых источниках и пишу один раз. Если это неактуально, просто ответьте «отписаться», и я больше не побеспокою.',
    "es": 'Encontré su proyecto en un listado público y escribo una sola vez. Si no es relevante, responda «baja» y no volveré a contactarles.',
    "de": 'Ich habe Ihr Projekt in einem öffentlichen Verzeichnis gefunden und schreibe nur einmal. Wenn es nicht passt, antworten Sie einfach „abmelden“, dann melde ich mich nicht mehr.',
    "fr": 'J\'ai trouvé votre projet dans un annuaire public et je n\'écris qu\'une fois. Si cela ne vous concerne pas, répondez « désinscription » et je ne vous recontacterai pas.',
    "pt": 'Encontrei seu projeto em uma listagem pública e escrevo apenas uma vez. Se não for relevante, responda «cancelar» e não entrarei em contato novamente.',
    "it": 'Ho trovato il vostro progetto in un elenco pubblico e scrivo una sola volta. Se non è pertinente, rispondete «annulla» e non vi contatterò più.',
}


def build_footer(cfg: dict, lang: str = "en") -> str:
    owner = cfg["owner"]
    parts = [
        "--",
        f"{owner['name']} | {cfg.get('brand', 'R Design Space')} - UI/UX, dashboards, frontend & Tilda Zero Block",
        f"Telegram: {owner['telegram_handle']} ({owner['telegram_url']})",
    ]
    if cfg.get("vercel_url"):
        parts.append(f"Portfolio: {cfg['vercel_url']}")
    if owner.get("postal_address"):
        parts.append(owner["postal_address"])
    parts += [
        "",
        OPT_OUT.get(lang, OPT_OUT["en"]) + (f"\n{OPT_OUT['en']}" if lang not in OPT_OUT else ""),
    ]
    return "\n".join(parts)


def send_email(to_addr: str, subject: str, body: str, cfg: dict, lang: str = "en") -> None:
    """Send one plain-text email. Raises on any failure so the caller can record it."""
    if not EMAIL_RE.match(to_addr):
        raise ValueError(f"invalid recipient address: {to_addr!r}")

    sender, password = load_credentials(cfg)
    msg = EmailMessage()
    msg["From"] = formataddr((f"{cfg['owner']['name']} | {cfg.get('brand', 'R Design Space')}", sender))
    msg["To"] = to_addr
    msg["Reply-To"] = sender
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    msg.set_content(f"{body.rstrip()}\n\n{build_footer(cfg, lang)}\n")

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
