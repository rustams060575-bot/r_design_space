"""Prints the URL for an external scheduler (cron-job.org etc.) that starts the lead search exactly on time.

    python agent/cron_key.py https://r-design-space-agent-sigma.vercel.app

Reads TELEGRAM_BOT_TOKEN from the environment (or agent/.env). The key is derived from the token (same formula as
api/cron.js), so it changes when the token is rotated. Treat the URL as a secret.
Scheduler settings: timezone Europe/Madrid, 09:00 and 21:00 every day, method GET.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


def cron_key(token: str) -> str:
    digest = hmac.new(token.encode(), b"cron-trigger", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:32]


def main(argv: list[str]) -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or len(argv) != 1 or not argv[0].startswith("https://"):
        print(__doc__)
        return 1
    print(f"{argv[0].rstrip('/')}/api/cron?key={cron_key(token)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
