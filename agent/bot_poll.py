"""Local long-polling mode of the bot (alternative to the Vercel webhook, handy for testing on your PC).

    python agent/bot_poll.py

Telegram allows either a webhook or polling, so this refuses to run while a webhook is set
(run `python agent/set_webhook.py --delete` first). Commands run the pipeline in-process, using the local
tracker.json / pending.json. Stop with Ctrl+C - nothing needs to run between sessions.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import scraper  # noqa: E402
from telegram_bot import HELP_TEXT, START_TEXT, call, send_text  # noqa: E402

_busy = threading.Lock()


def parse_command(text: str) -> tuple[str, str] | None:
    import re
    m = re.match(r"^/([a-z_]+)(?:@\w+)?(?:\s+(.*))?$", (text or "").strip(), re.I | re.S)
    return (m.group(1).lower(), re.sub(r"[\x00-\x1f]", " ", m.group(2) or "").strip()[:100]) if m else None


def _in_background(fn, *args) -> None:
    def job():
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - keep the bot alive, report instead
            print(f"[bot] job failed: {type(exc).__name__}")
            send_text(f"⚠️ Ошибка выполнения: {type(exc).__name__}. Подробности в консоли.")
        finally:
            _busy.release()

    if not _busy.acquire(blocking=False):
        send_text("⏳ Уже выполняется другая задача - дождитесь результата.")
        return
    threading.Thread(target=job, daemon=True).start()


def handle_update(update: dict, cfg: dict) -> None:
    owner = str(os.getenv("TELEGRAM_CHAT_ID", ""))
    msg, cb = update.get("message"), update.get("callback_query")
    chat = (msg or {}).get("chat", {}).get("id") or ((cb or {}).get("message") or {}).get("chat", {}).get("id")
    if not owner or str(chat) != owner:
        return  # only the owner's chat is obeyed

    if msg and msg.get("text"):
        cmd = parse_command(msg["text"])
        if not cmd:
            send_text("Не понял. Напишите /help")
        elif cmd[0] == "start":
            send_text(START_TEXT)
        elif cmd[0] == "help":
            send_text(HELP_TEXT)
        elif cmd[0] == "search":
            send_text(f"🔎 Запускаю поиск{' по запросу «' + cmd[1] + '»' if cmd[1] else ''}. Карточки придут через пару минут.")
            _in_background(scraper.run_search, cfg, cmd[1] or None)
        else:
            send_text("Неизвестная команда. Напишите /help")
    elif cb:
        action, _, lead_id = (cb.get("data") or "").partition(":")
        if action == "skip":
            call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Пропущено"})
            call("editMessageReplyMarkup", {"chat_id": chat, "message_id": cb["message"]["message_id"],
                                            "reply_markup": {"inline_keyboard": []}})
        elif action == "send" and scraper.LEAD_ID_RE.match(lead_id):
            call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Отправляю письмо..."})
            call("editMessageReplyMarkup", {"chat_id": chat, "message_id": cb["message"]["message_id"],
                                            "reply_markup": {"inline_keyboard": []}})
            _in_background(scraper.send_pending, cfg, lead_id)
        else:
            call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Неизвестное действие"})


def main() -> int:
    cfg = scraper.load_json(scraper.CONFIG_PATH, None)
    if cfg is None or not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        print("Need config.json, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return 1
    info = call("getWebhookInfo", {})
    if info and info.get("result", {}).get("url"):
        print("A webhook is registered - polling would conflict. Run: python agent/set_webhook.py --delete")
        return 1

    print(f"[bot] polling started, mode: {'DRY RUN' if scraper.is_dry_run() else 'LIVE'}. Ctrl+C to stop.")
    offset = 0
    while True:
        resp = call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]}, timeout=40)
        if resp is None:
            time.sleep(5)
            continue
        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            handle_update(update, cfg)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[bot] stopped")
