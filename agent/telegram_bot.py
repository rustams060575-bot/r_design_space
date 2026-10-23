"""Telegram cards: tell Rustam about every processed lead with a clear description and the pitch text."""
from __future__ import annotations

import os

import requests

STATUS_LABEL = {
    "sent": "✅ Письмо отправлено",
    "dry_run": "🧪 Тестовый режим: письмо НЕ отправлено (DRY_RUN)",
    "alert_only": "✋ Контактный email не найден - напишите вручную",
    "email_failed": "⚠️ Ошибка отправки письма - см. логи Actions",
    "domain_already_contacted": "⏭ Этот домен уже получал письмо",
    "email_cap_reached": "⏳ Лимит писем за запуск исчерпан",
}
SOURCE_LABEL = {
    "producthunt": "Product Hunt",
    "hackernews": "Hacker News · Show HN",
    "hackernews_ask": "Hacker News · Ask HN (человек ищет помощь)",
    "feed": "RSS-лента",
}


def build_message(lead: dict, pitch: dict, status: str) -> str:
    lines = [
        f"🎯 Новый лид: {lead['name']}",
        f"Источник: {SOURCE_LABEL.get(lead['source'], lead['source'])}",
        f"Статус: {STATUS_LABEL.get(status, status)}",
        "",
        "Почему подходит:",
        f"• Ключевые слова: {', '.join(lead.get('keywords', [])) or '-'}",
        f"• Сигналы: {', '.join(lead.get('signals', [])) or '-'}",
    ]
    if lead.get("summary"):
        lines += ["", "О проекте:", lead["summary"][:350]]
    lines += [
        "",
        f"Пост: {lead.get('url') or '-'}",
        f"Сайт: {lead.get('website') or '-'}",
        f"Контакт: {lead.get('email') or '-'}",
    ]
    if lead.get("author"):
        lines.append(f"Автор на HN: {lead['author']}")
    lines += ["", "✉️ Письмо (предпросмотр)", f"Тема: {pitch['subject']}", "", pitch["body"]]
    return "\n".join(lines)


def send_alert(lead: dict, pitch: dict, status: str, timeout: int = 15) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - card skipped")
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
