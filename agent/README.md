# R Design Space · агент лидогенерации

Бот ищет стартапы и SaaS по миру, отбирает их ИИ, пишет письмо на языке фаундера и присылает карточку в Telegram.
Письмо уходит **только после нажатия кнопки «Отправить письмо»**. Постоянный сервер не нужен.

```
Telegram ──► Vercel api/telegram.js ──► GitHub Actions (outreach.yml) ──► scraper.py ──► Telegram
 /search        webhook, мгновенно         workflow_dispatch               поиск, ИИ,       карточки
 кнопки         проверяет секрет и chat id  (search | send)                письмо, Gmail    и отчёт
```

## Файлы

| Файл | Назначение |
|---|---|
| `api/telegram.js` | webhook: `/start`, `/help`, `/search [запрос]`, кнопки; запускает Actions |
| `.github/workflows/outreach.yml` | ежедневно 09:00 UTC + запуск из Telegram; сохраняет `tracker.json` и `pending.json` |
| `agent/scraper.py` | точка входа: `--action search \| send`, `--query`, `--lead-id` |
| `agent/qualifier.py` | жёсткий отбор (порог 7/10), анализ на русском, определение языка письма |
| `agent/pitch_writer.py` | письмо на языке фаундера в стиле R Design Space |
| `agent/email_sender.py` | Gmail SMTP, подпись и отписка на языке письма |
| `agent/telegram_bot.py` | карточки, кнопки, отчёт, тексты `/start` и `/help` |
| `agent/bot_poll.py` | необязательный локальный режим polling (вместо webhook) |
| `agent/set_webhook.py` | разовая регистрация webhook и меню команд |
| `agent/llm.py`, `agent/langs.py` | клиент Claude/OpenAI, справочник языков |
| `agent/config.json` | ключевые слова, регионы, лимиты, `require_confirmation` |
| `agent/tracker.json`, `agent/pending.json` | обработанные лиды и черновики, ждущие кнопки |

## Источники (по всему миру)

Product Hunt, Hacker News (Show HN и Ask HN), **Google News RSS** на 15 регионов и языков (США, Великобритания,
Россия, Казахстан, Германия, Франция, Испания, Латинская Америка, Бразилия, Турция, Индия, Сингапур, Япония,
Корея, Тайвань), vc.ru и EU-Startups. Регионы и запросы правятся в `config.json → global_search`.
Для новостных лидов сайт неизвестен, поэтому по ним приходит карточка для ручного контакта; письма
автоматически готовятся там, где найден публичный email на сайте продукта.

## Настройка (один раз)

1. **GitHub → Settings → Secrets and variables → Actions → Secrets:** `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`, `GMAIL_APP_PASSWORD`. Variables: `DRY_RUN=true` (на `false` только после проверки карточек).
2. **Токен для запуска Actions из Vercel:** GitHub → Settings → Developer settings → Fine-grained tokens →
   только этот репозиторий, право **Actions: Read and write**.
3. **Vercel → Project → Settings → Environment Variables:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   `TELEGRAM_WEBHOOK_SECRET` (любая случайная строка), `GH_DISPATCH_TOKEN` (токен из п. 2). После этого Redeploy.
4. **Регистрация webhook** (локально, один раз):
   `python agent/set_webhook.py https://<проект>.vercel.app/api/telegram`
5. Напишите боту `/start`, затем `/search`.

Без Vercel: `python agent/bot_poll.py` слушает команды прямо на вашем компьютере (пока окно открыто).

## Безопасность

- Бот подчиняется только вашему `TELEGRAM_CHAT_ID`; webhook проверяет секретный заголовок Telegram.
- Текст запроса передаётся в workflow через переменные окружения, а не в команду shell.
- Письмо = одно на домен, с отпиской; `DRY_RUN` блокирует реальную отправку даже после нажатия кнопки.
- Если подряд нажать «Отправить» у нескольких лидов, GitHub оставляет в очереди только последний запуск;
  нажмите кнопку у пропущенного письма ещё раз.
