"""james · lead hunt on the same clock as the posts: 09:00 and 21:00 Miami.

Runs right after the slot's post (same workflow run, same window gate as the publishing, once per slot per Miami day) and sends one
concrete report to Telegram: how much was scanned, approved leads (each also arrives as a card with an email draft and a conversation
draft), near misses with links, and the postings found on job boards. Outreach emails are never sent without the owner's confirmation.

    python agent/scraper.py --action autopilot            # scheduled: publishing + this report inside the 09:00 / 21:00 windows
    SEARCH_QUERY=leads  (workflow_dispatch, action=autopilot) forces a report now
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

STATE = Path(__file__).resolve().parent.parent / "content-hub" / "leads_state.json"
TZ = "America/New_York"
BOARDS = ("jobboard", "hackernews_ask", "signal")


def load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"runs": []}


def format_report(stats: dict, cfg: dict, label: str) -> list[str]:
    rc = cfg["james"].get("leads_report", {})
    found = stats.get("found", [])
    lines = [f"📊 Отчёт Джеймса по лидам · {label}",
             f"Новых кандидатов: {stats['candidates']} · проверено: {stats['checked']} · одобрено: {stats['approved']} · отсеяно: {stats['rejected']}"]
    approved = [f for f in found if f["approved"]]
    if approved:
        lines.append("\n✅ Одобрены (карточка с черновиком письма и переписки пришла отдельно):")
        for f in approved:
            lines.append(f"• {f['name']} · {f['score']}/10 · {f.get('need')} · {f.get('status', '')}\n  {f['url']}")
    near = sorted((f for f in found if not f["approved"] and f["score"] >= rc.get("near_miss_min_score", 6)), key=lambda f: -f["score"])
    if near:
        lines.append("\n🟡 Почти подошли (не прошли строгий US / high-end фильтр, ссылка для ручного взгляда):")
        for f in near[: rc.get("near_miss_max", 8)]:
            lines.append(f"• {f['name']} · {f['score']}/10 · {f['reason'] or 'ниже порога'}\n  {f['url']}")
    boards = [f for f in found if f["source"] in BOARDS and f["score"] >= 4]
    boards.sort(key=lambda f: -f["score"])
    if boards:
        lines.append("\n🧭 Найдено на биржах, вакансиях и сигналах:")
        for f in boards[: rc.get("board_postings_max", 12)]:
            lines.append(f"• [{f['source']}] {f['name']} · {f['score']}/10\n  {f['url']}")
    if not found:
        lines.append("\nНовых объявлений сегодня нет: все найденные уже проверены в прошлых запусках. Источники сканируются каждый слот.")
    elif not approved:
        lines.append("\nСтрогий фильтр (США, реальный бюджет, не шаблоны и не биржи микро-заказов) ничего не одобрил в этом слоте.")
    msgs, cur = [], ""
    for line in lines:  # Telegram limit is 4096 characters
        if len(cur) + len(line) > 3500:
            msgs.append(cur)
            cur = ""
        cur += line + "\n"
    return msgs + [cur]


def run_if_due(cfg: dict, slot: str | None) -> None:
    rc = cfg["james"].get("leads_report", {})
    if not rc.get("enabled"):
        return
    forced = os.getenv("SEARCH_QUERY", "").strip().lower() == "leads"
    if not (slot or forced):
        return
    et = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ))
    day = et.strftime("%Y-%m-%d")
    key = slot or "manual"
    state = load()
    if not forced and any(r["date"] == day and r["slot"] == key for r in state["runs"]):
        print(f"[leads] {day} {key}: report already sent")
        return
    import scraper
    from telegram_bot import send_text
    print(f"[leads] {day} {key}: searching")
    stats = scraper.run_search(cfg, None, quiet=True)
    for msg in format_report(stats, cfg, f"{et.strftime('%H:%M')} Miami"):
        send_text(msg)
    state["runs"].append({"date": day, "slot": key, "checked": stats["checked"], "approved": stats["approved"]})
    state["runs"] = state["runs"][-60:]
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
