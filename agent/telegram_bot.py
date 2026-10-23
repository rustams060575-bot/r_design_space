"""Telegram layer: lead cards with confirmation buttons, run reports and the bot's static texts.

Every approved lead produces two messages:
  1. the analysis card (Russian): what the project is, why it fits, what to offer, language, status and buttons;
  2. the email draft in the founder's language (original text, so the R Design Space style can be judged at once),
     with a one-line Russian gist when the draft is not in Russian.
Commands and button taps are received by api/telegram.js (webhook) or agent/bot_poll.py (local polling).
"""
from __future__ import annotations

import os

import requests

import langs
from qualifier import NEED_RU, URGENCY_RU

STATUS_LABEL = {
    "pending": "⏳ Ждёт вашего подтверждения - нажмите «Отправить письмо»",
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
    "news": "Новости",
    "feed": "Лента СМИ",
}
LINE = "━━━━━━━━━━━━━━"

START_TEXT = (
    "👋 R Design Space · персональный агент\n\n"
    "• Лидогенерация: ищу стартапы и SaaS по миру, пишу письма на языке фаундера. Письмо уходит только после вашей кнопки.\n"
    "• Диалог с памятью: пишите мне как ассистенту - я помню проекты, договорённости и интересы, даже спустя недели.\n\n"
    "Команды:\n/search - поиск лидов сейчас (можно с запросом)\n/memory - что я помню\n/history - хронология\n"
    "/remember <текст> - запомнить\n/forget <id> - забыть запись\n/help - справка"
)
HELP_TEXT = (
    "ℹ️ Как это работает\n\n"
    "🔎 /search запускает сканирование в GitHub Actions (сервер не нужен). Через 2-4 минуты придут карточки: анализ на русском и "
    "черновик письма. «✅ Отправить письмо» отправляет черновик с вашей почты, «⏭ Пропустить» убирает лид. Каждый день в 09:00 UTC поиск идёт сам.\n\n"
    "🧠 Память. Любое сообщение без «/» - вопрос ассистенту. Я учитываю сохранённые записи («что мы решили по документам клуба?») "
    "и сам сохраняю важное: проекты, договорённости со сроками, интересы. Пароли и ключи не сохраняются.\n"
    "/memory - активные проекты и договорённости; /memory клуб - поиск по памяти\n/history 15 - последние записи\n"
    "/remember <текст> - сохранить заметку\n/forget <id> - удалить запись (id в скобках)\n\n"
    "🧪 Пока в репозитории DRY_RUN=true, письма не отправляются."
)
POLL_ONLY_NOTE = (
    "🧠 Диалог и память работают в режиме webhook (Vercel). Локальный polling (bot_poll.py) умеет только "
    "/search и кнопки писем."
)


def mode_label(dry_run: bool) -> str:
    return "🧪 Режим: ТЕСТ (письма не уходят)" if dry_run else "🚀 Режим: БОЕВОЙ (письма уходят)"


def build_card(lead: dict, status: str, dry_run: bool, language: str = "en") -> str:
    a = lead.get("analysis") or {}
    why = [f"• {x}" for x in a.get("why_ru", [])] or ["• -"]
    lines = [
        f"🎯 {lead['name']}",
        LINE,
        f"📍 {SOURCE_LABEL.get(lead['source'], lead['source'])}" + (f" · {lead['region']}" if lead.get("region") else ""),
        f"⭐ Оценка: {a.get('score', '?')}/10 · 🧩 {NEED_RU.get(a.get('need_type'), 'запрос')} · ⏱ {URGENCY_RU.get(a.get('urgency'), 'срок не указан')}",
        f"🌐 Язык письма: {langs.LANG_RU.get(language, language)}",
        mode_label(dry_run),
        STATUS_LABEL.get(status, status),
        "",
        "📝 Кто и что просит",
        a.get("summary_ru") or "-",
        "",
        "😣 Боль клиента",
        a.get("pain_ru") or "-",
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
    code = pitch.get("language", "en")
    lines = [f"✉️ Черновик письма ({code.upper()})", LINE]
    if pitch.get("gist_ru") and code != "ru":
        lines += [f"О чём: {pitch['gist_ru']}", ""]
    lines += [f"Subject: {pitch['subject']}", "", pitch["body"]]
    return "\n".join(lines)


def card_buttons(lead: dict, status: str) -> list[list[dict]]:
    rows = []
    if status == "pending":
        rows.append([{"text": "✅ Отправить письмо", "callback_data": f"send:{lead['id']}"},
                     {"text": "⏭ Пропустить", "callback_data": f"skip:{lead['id']}"}])
    links = [{"text": text, "url": url} for text, url in (("🔗 Источник", lead.get("url")), ("🌐 Сайт", lead.get("website")))
             if url and url.startswith(("http://", "https://"))]
    if links:
        rows.append(links)
    return rows


def call(method: str, payload: dict, timeout: int = 15) -> dict | None:
    """Telegram Bot API call. Returns the decoded response or None on failure (the token is never printed)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("[telegram] TELEGRAM_BOT_TOKEN not set - call skipped")
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"[telegram] {method} failed: {type(exc).__name__}")
        return None


def send_text(text: str, reply_markup: dict | None = None, chat_id: str | None = None, timeout: int = 15) -> bool:
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        print("[telegram] TELEGRAM_CHAT_ID not set - message skipped")
        return False
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return call("sendMessage", payload, timeout) is not None


def send_alert(lead: dict, pitch: dict, status: str, dry_run: bool = True) -> bool:
    language = pitch.get("language", "en")
    buttons = card_buttons(lead, status)
    ok = send_text(build_card(lead, status, dry_run, language), {"inline_keyboard": buttons} if buttons else None)
    return send_text(build_draft(pitch)) and ok


def send_report(stats: dict, dry_run: bool, query: str | None = None) -> bool:
    lines = ["📊 Отчёт запуска", LINE, mode_label(dry_run)]
    if query:
        lines.append(f"🔎 Запрос: {query}")
    lines += [
        f"Новых кандидатов: {stats['candidates']}",
        f"Проверено ИИ/правилами: {stats['checked']}",
        f"Отсеяно как нерелевантные: {stats['rejected']}",
        f"Карточек отправлено: {stats['approved']}",
        f"Писем отправлено: {stats['emails']}",
    ]
    if not stats["approved"]:
        lines.append("\nПодходящих новых лидов не нашлось. Попробуйте другой запрос: /search <запрос>")
    return send_text("\n".join(lines))
