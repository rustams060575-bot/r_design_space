// Telegram webhook on Vercel (serverless: no 24/7 server).
// /start and /help are answered right here. /search and the "Send" button start the GitHub Actions workflow
// (workflow_dispatch), which does the heavy work (scanning, AI, email) and writes the results back to Telegram.
//
// Environment variables (Vercel -> Project -> Settings -> Environment Variables):
//   TELEGRAM_BOT_TOKEN       bot token from @BotFather
//   TELEGRAM_CHAT_ID         your chat id - the ONLY chat the bot obeys
//   TELEGRAM_WEBHOOK_SECRET  random string, also passed to setWebhook (agent/set_webhook.py)
//   GH_DISPATCH_TOKEN        fine-grained GitHub token: this repo only, permission "Actions: Read and write"
//   GITHUB_REPO              optional, default rustams060575-bot/r_design_space
//   GITHUB_REF               optional, default main
//   GITHUB_WORKFLOW          optional, default outreach.yml

const START_TEXT =
  "👋 R Design Space · бот лидогенерации\n\n" +
  "Ищу стартапы и SaaS по всему миру (США, Европа, СНГ, Азия, Латинская Америка), отбираю подходящие ИИ, " +
  "пишу письма на языке фаундера и присылаю карточки. Письмо уходит только после вашего нажатия на кнопку.\n\n" +
  "Команды:\n/search - поиск прямо сейчас\n/search fintech dashboard - поиск по своему запросу\n/help - справка";

const HELP_TEXT =
  "ℹ️ Как это работает\n\n" +
  "1. /search запускает сканирование в GitHub Actions - постоянный сервер не нужен.\n" +
  "2. Через 2-4 минуты придут карточки: анализ на русском и черновик письма.\n" +
  "3. Кнопка «✅ Отправить письмо» отправляет черновик с вашей почты, «⏭ Пропустить» убирает лид.\n\n" +
  "Свой запрос лучше писать по-английски: /search CRM startup. Каждый день в 09:00 UTC поиск идёт сам.\n\n" +
  "🧪 Пока в репозитории DRY_RUN=true, письма не отправляются - можно безопасно проверять карточки.";

const LEAD_ID = /^[a-z_]+:[A-Za-z0-9]{1,40}$/;

async function tg(method, payload) {
  const res = await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) console.error(`telegram ${method} -> ${res.status}`); // never log the URL: it holds the token
  return res.ok;
}

async function dispatch(inputs) {
  const repo = process.env.GITHUB_REPO || "rustams060575-bot/r_design_space";
  const workflow = process.env.GITHUB_WORKFLOW || "outreach.yml";
  const res = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.GH_DISPATCH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "r-design-space-bot",
      "content-type": "application/json",
    },
    body: JSON.stringify({ ref: process.env.GITHUB_REF || "main", inputs }),
  });
  if (res.status !== 204) console.error(`github dispatch -> ${res.status}`);
  return res.status === 204;
}

function parseCommand(text) {
  const m = /^\/([a-z_]+)(?:@\w+)?(?:\s+([\s\S]*))?$/i.exec((text || "").trim());
  return m ? { name: m[1].toLowerCase(), arg: (m[2] || "").replace(/[\u0000-\u001f]/g, " ").trim().slice(0, 100) } : null;
}

async function onMessage(msg) {
  const chat = msg.chat.id;
  const cmd = parseCommand(msg.text);
  if (!cmd) return tg("sendMessage", { chat_id: chat, text: "Не понял. Напишите /help" });

  if (cmd.name === "start") return tg("sendMessage", { chat_id: chat, text: START_TEXT });
  if (cmd.name === "help") return tg("sendMessage", { chat_id: chat, text: HELP_TEXT });
  if (cmd.name === "search") {
    const ok = await dispatch({ action: "search", query: cmd.arg, lead_id: "" });
    const what = cmd.arg ? ` по запросу «${cmd.arg}»` : "";
    return tg("sendMessage", {
      chat_id: chat,
      text: ok
        ? `🔎 Запускаю поиск${what}. Карточки придут через 2-4 минуты.`
        : "⚠️ Не удалось запустить поиск (GitHub Actions). Проверьте GH_DISPATCH_TOKEN и имя репозитория в настройках Vercel.",
    });
  }
  return tg("sendMessage", { chat_id: chat, text: "Неизвестная команда. Напишите /help" });
}

async function onCallback(cb) {
  const chat = cb.message && cb.message.chat.id;
  const [action, ...rest] = (cb.data || "").split(":");
  const leadId = rest.join(":");
  if (!LEAD_ID.test(leadId) || !["send", "skip"].includes(action)) {
    return tg("answerCallbackQuery", { callback_query_id: cb.id, text: "Неизвестное действие" });
  }

  if (action === "skip") {
    await tg("answerCallbackQuery", { callback_query_id: cb.id, text: "Пропущено" });
    return tg("editMessageReplyMarkup", { chat_id: chat, message_id: cb.message.message_id, reply_markup: { inline_keyboard: [] } });
  }

  const ok = await dispatch({ action: "send", query: "", lead_id: leadId });
  await tg("answerCallbackQuery", {
    callback_query_id: cb.id,
    text: ok ? "Отправляю письмо... итог придёт сообщением" : "Не удалось запустить отправку",
    show_alert: !ok,
  });
  if (ok) {
    await tg("editMessageReplyMarkup", { chat_id: chat, message_id: cb.message.message_id, reply_markup: { inline_keyboard: [] } });
  }
}

module.exports = async (req, res) => {
  // Always answer 200 once the request is authentic, otherwise Telegram retries the update.
  if (req.method !== "POST") return res.status(405).end();

  const secret = process.env.TELEGRAM_WEBHOOK_SECRET;
  if (!secret || req.headers["x-telegram-bot-api-secret-token"] !== secret) return res.status(401).end();

  try {
    const update = typeof req.body === "string" ? JSON.parse(req.body) : req.body || {};
    const message = update.message;
    const callback = update.callback_query;
    const chat = message ? message.chat.id : callback && callback.message ? callback.message.chat.id : null;

    // Only the owner's chat may run the bot; everyone else is ignored silently.
    if (chat !== undefined && chat !== null && String(chat) === String(process.env.TELEGRAM_CHAT_ID)) {
      if (message && message.text) await onMessage(message);
      else if (callback) await onCallback(callback);
    }
  } catch (err) {
    console.error("webhook error:", err && err.message);
  }
  return res.status(200).json({ ok: true });
};
