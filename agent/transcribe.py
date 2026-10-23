"""Speech-to-text for the Telegram bot, fully free: open-source Whisper (faster-whisper) runs on a GitHub Actions
runner. No API key and no paid service: the audio never leaves GitHub's machine.

Flow (started by .github/workflows/voice.yml, which api/telegram.js triggers when a voice message arrives):
    getFile -> download the voice note -> transcribe -> POST the transcript back to the Vercel webhook as a signed
    update, where the usual Claude conversation (with memory) answers and replies with text and a voice message.

Environment: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (the only chat served), WEBHOOK_URL (repo variable),
FILE_ID and CHAT_ID (workflow inputs), optional WHISPER_MODEL (default "small": good for Russian and English).
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from set_webhook import derived_secret  # noqa: E402
from telegram_bot import call, send_text  # noqa: E402

FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,250}$")
MAX_BYTES = 5 * 1024 * 1024


def download_voice(token: str, file_id: str, folder: str) -> Path:
    info = call("getFile", {"file_id": file_id})
    path = ((info or {}).get("result") or {}).get("file_path")
    if not path:
        raise RuntimeError("Telegram did not return a file path")
    r = requests.get(f"https://api.telegram.org/file/bot{token}/{path}", timeout=30)
    r.raise_for_status()
    if len(r.content) > MAX_BYTES:
        raise RuntimeError("voice file is too large")
    target = Path(folder) / "voice.ogg"
    target.write_bytes(r.content)
    return target


def transcribe(audio: Path, model_name: str) -> str:
    from faster_whisper import WhisperModel  # imported late: the package is only installed for the voice workflow

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(audio), vad_filter=True, beam_size=1)  # language is detected automatically
    return " ".join(s.text.strip() for s in segments).strip()


def post_to_webhook(url: str, token: str, chat_id: int, text: str) -> None:
    """Delivers the transcript to the webhook as a signed update (same secret Telegram uses)."""
    r = requests.post(
        url,
        json={"update_id": 0, "message": {"chat": {"id": chat_id}, "text": text, "rs_voice": True}},
        headers={"x-telegram-bot-api-secret-token": derived_secret(token)},
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"webhook answered HTTP {r.status_code} ({r.headers.get('x-auth-reason', 'no reason')})")


def main() -> int:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    owner = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    chat_id = (os.getenv("CHAT_ID") or "").strip()
    file_id = (os.getenv("FILE_ID") or "").strip()
    url = (os.getenv("WEBHOOK_URL") or "").strip()

    if not token or not owner or chat_id != owner:
        print("refusing: missing token or the chat is not the owner's chat")
        return 1
    if not FILE_ID_RE.match(file_id):
        print("refusing: invalid file id")
        return 1
    if not url.startswith("https://"):
        send_text("⚠️ Голос: не задана переменная репозитория WEBHOOK_URL (Settings → Secrets and variables → Actions → Variables).")
        return 1

    try:
        with tempfile.TemporaryDirectory() as folder:
            audio = download_voice(token, file_id, folder)
            text = transcribe(audio, os.getenv("WHISPER_MODEL", "small"))
        print(f"[voice] transcribed {len(text)} characters")
        post_to_webhook(url, token, int(chat_id), text)  # an empty text makes the bot say it did not understand
    except Exception as exc:  # noqa: BLE001 - tell the user instead of failing silently
        print(f"[voice] failed: {type(exc).__name__}: {exc}")
        send_text(f"⚠️ Не удалось обработать голосовое ({type(exc).__name__}). Попробуйте ещё раз или напишите текстом.")
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
