"""Telegram cards, written for quick reading on a phone.

Every approved lead produces two messages:
  1. the analysis card (Russian): what the project is, why it fits, what to offer, status, link buttons;
  2. the email draft (English, original text) so it can be copied or judged for style straight away.
A short run report is sent at the end.
"""
from __future__ import annotations

import os

import requests

from qualifier import CATEGORY_RU

STATUS_LABEL = {
    "sent": "✅ Письмо отправлено",
    "dry_run": "🧪 Письмо НЕ отправлено (тестовый режим)",
    "alert_only": "✋ Email не найден - напишите вручную",
    "email_failed": "⚠️ Не удалось отправить письмо (см. логи Actions)",
    "domain_already_contacted": "⏭ Этому домену уже писали",
    "email_cap_reached": "⏳ Лимит писем за запуск исчерпан",
}
SOURCE_LABEL = {
    "producthunt": "Product Hunt",
    "hackernews": "Hacker News · Show HN",
    "hackernews_ask": "Hacker News · Ask HN",
    "feed": "RSS-лента",
}
LINE = "━━━━━━━━━━━━━━"


def mode_label(dry_run: bool) -> str:
    return "🧪 Режим: ТЕСТ (письма не уходят)" if dry_run else "🚀 Режим: БОЕВОЙ (письма уходят)"


def build_card(lead: dict, status: str, dry_run: bool) -> str:
    a = lead.get("analysis") or {}
    why = [f"• {x}" for x in a.get("why_ru", [])] or ["• -"]
    lines = [
        f"🎯 {lead['name']}",
        LINE,
        f"📍 {SOURCE_LABEL.get(lead['source'], lead['source'])}",
        f"⭐ Оценка: {a.get('score', '?')}/10 · {CATEGORY_RU.get(a.get('category'), 'проект')}",
        mode_label(dry_run),
        STATUS_LABEL.get(status, status),
        "",
        "📝 О проекте",
        a.get("summary_ru") or "-",
        "",
        "✅ Почему подходит",
        *why,
        "",
        "🛠 Что предложить",
        a.get("need_ru") or "-",
        "",
        f"📧 Контакт: {lead.get('email') or 'не найден'}",
    ]
    if lead.get("author"):
        lines.append(f"👤 Автор на HN: {lead['author']}")
    return "\n".join(lines)


def build_draft(pitch: dict) -> str:
    return f"✉️ Черновик письма (EN)\n{LINE}\nSubject: {pitch['subject']}\n\n{pitch['body']}"


def _buttons(lead: dict) -> list[list[dict]]:
    row = []
    for text, url in (("🔗 Пост", lead.get("url")), ("🌐 Сайт", lead.get("website"))):
        if url and url.startswith(("http://", "https://")):
            row.append({"text": text, "url": url})
    return [row] if row else []


def _post(method: str, payload: dict, timeout: int) -> bool:
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - message skipped")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          json={"chat_id": chat_id, "disable_web_page_preview": True, **payload}, timeout=timeout)
        r.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"[telegram] send failed: {type(exc).__name__}")  # the URL contains the bot token - never print it
        return False


def send_alert(lead: dict, pitch: dict, status: str, dry_run: bool = True, timeout: int = 15) -> bool:
    card = {"text": build_card(lead, status, dry_run)[:4000]}
    buttons = _buttons(lead)
    if buttons:
        card["reply_markup"] = {"inline_keyboard": buttons}
    ok = _post("sendMessage", card, timeout)
    return _post("sendMessage", {"text": build_draft(pitch)[:4000]}, timeout) and ok


def send_report(stats: dict, dry_run: bool, timeout: int = 15) -> bool:
    text = "\n".join([
        "📊 Отчёт запуска",
        LINE,
        mode_label(dry_run),
        f"Новых кандидатов: {stats['candidates']}",
        f"Проверено ИИ/правилами: {stats['checked']}",
        f"Отсеяно как нерелевантные: {stats['rejected']}",
        f"Карточек отправлено: {stats['approved']}",
        f"Писем отправлено: {stats['emails']}",
    ])
    return _post("sendMessage", {"text": text}, timeout)
