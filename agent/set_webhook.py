"""One-off setup: registers the Vercel webhook and the command menu in Telegram.

    python agent/set_webhook.py https://<your-project>.vercel.app/api/telegram

Reads TELEGRAM_BOT_TOKEN from the environment (or agent/.env). The webhook secret is derived from the token
(same formula as api/_lib/env.js), so there is no second value to keep in sync with Vercel.
Use `python agent/set_webhook.py --delete` to go back to polling (agent/bot_poll.py).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from telegram_bot import call  # noqa: E402

COMMANDS = [
    {"command": "start", "description": "О боте и команды"},
    {"command": "search", "description": "Поиск лидов прямо сейчас (можно с запросом)"},
    {"command": "memory", "description": "Что я помню: проекты и договорённости"},
    {"command": "history", "description": "Хронология памяти"},
    {"command": "remember", "description": "Запомнить заметку"},
    {"command": "forget", "description": "Забыть запись по id"},
    {"command": "help", "description": "Справка"},
]


def derived_secret(token: str) -> str:
    digest = hmac.new(token.encode(), b"telegram-webhook", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:43]


def main(argv: list[str]) -> int:
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        print("TELEGRAM_BOT_TOKEN is not set")
        return 1
    if argv[:1] == ["--delete"]:
        print("deleteWebhook:", call("deleteWebhook", {"drop_pending_updates": False}))
        return 0
    if len(argv) != 1 or not argv[0].startswith("https://"):
        print(__doc__)
        return 1
    secret = derived_secret(os.environ["TELEGRAM_BOT_TOKEN"].strip())

    print("setWebhook:", call("setWebhook", {
        "url": argv[0], "secret_token": secret, "allowed_updates": ["message", "callback_query"],
    }))
    print("setMyCommands:", call("setMyCommands", {"commands": COMMANDS}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
