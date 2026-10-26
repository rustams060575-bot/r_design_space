// Public lead qualifier: state of conversations with EXTERNAL clients (people who come from Instagram / TikTok).
// Kept in leads.json in the same private repository as the owner's memory, but strictly separate from it:
// the client conversation never reads the owner's memory, and the owner's chat never reads this file by accident.

"use strict";

const { parseSource } = require("./memory");
const funnelStats = () => require("./motion_funnel").renderFunnelStats; // lazy: motion_funnel requires this module

const MAX_TEXT = 1200; // characters taken from one client message
const MAX_HISTORY = 24; // messages kept per client
const DAILY_LIMIT = 60; // messages per client per day: protects the Anthropic budget from abuse
const MAX_LEADS = 500;

const STANDARD_EUR = 1000; // minimum check
const PREMIUM_EUR = 3000; // premium

const LANGS = ["es", "en", "ru"];
const AUDIT_STATUSES = ["new", "in_work", "audit_sent", "done"];

const emptyLeads = () => ({ version: 1, last_update_id: 0, leads: {}, stats: { starts: {} } });

function normalizeLeads(raw) {
  const m = raw && typeof raw === "object" ? raw : {};
  return {
    version: 1,
    last_update_id: Number(m.last_update_id) || 0,
    leads: m.leads && typeof m.leads === "object" ? m.leads : {},
    stats: { starts: m.stats && m.stats.starts && typeof m.stats.starts === "object" ? m.stats.starts : {} },
  };
}

const clip = (v, n) => String(v == null ? "" : v).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, " ").trim().slice(0, n);

// The tier is computed from the number, not left to the model: the thresholds are business rules.
function tierFromBudget(eur) {
  const n = Number(eur);
  if (!Number.isFinite(n) || n <= 0) return "unknown";
  if (n >= PREMIUM_EUR) return "premium";
  if (n >= STANDARD_EUR) return "standard";
  return "cold";
}
const TIER_RU = { unknown: "бюджет не назван", cold: "холодный (до 1 000 €)", standard: "стандарт (от 1 000 €)", premium: "ПРЕМИУМ (от 3 000 €)" };
const TIER_RANK = { unknown: 0, cold: 1, standard: 2, premium: 3 };

function getLead(db, from, chatId, now) {
  const key = String(chatId);
  if (!db.leads[key]) {
    db.leads[key] = {
      id: key,
      name: clip([from && from.first_name, from && from.last_name].filter(Boolean).join(" "), 80),
      username: clip(from && from.username, 64),
      source: null,
      first_seen: new Date(now).toISOString(),
      updated: new Date(now).toISOString(),
      status: "new", // new -> in_dialog -> handoff
      audit_status: "new", // set by the owner's card buttons: new -> in_work -> audit_sent -> done
      lang: langFromCode(from && from.language_code),
      tier: "unknown",
      profile: { task: "", company: "", role: "", budget_eur: null, budget_note: "", timeline: "", contact: "", website: "", contact_pref: "" },
      summary: "",
      notified_new: false,
      notified_handoff: false,
      day: new Date(now).toISOString().slice(0, 10),
      day_count: 0,
      history: [],
    };
    const keys = Object.keys(db.leads);
    if (keys.length > MAX_LEADS) {
      const oldest = keys.sort((a, b) => Date.parse(db.leads[a].updated) - Date.parse(db.leads[b].updated))[0];
      delete db.leads[oldest];
    }
  }
  const lead = db.leads[key];
  // Leads saved before the audit funnel existed get the new fields.
  if (!lead.audit_status) lead.audit_status = "new";
  if (!lead.lang) lead.lang = langFromCode(from && from.language_code);
  if (lead.profile.website === undefined) lead.profile.website = "";
  if (lead.profile.contact_pref === undefined) lead.profile.contact_pref = "";
  return lead;
}

// Telegram's language_code is only the first guess; the model confirms the real language from the messages.
function langFromCode(code) {
  const c = String(code || "").toLowerCase().slice(0, 2);
  return LANGS.includes(c) ? c : "en";
}

// Counts a deep-link visit (every /start with a valid payload), separately from the lead's current source.
function countStart(db, code) {
  if (!db.stats) db.stats = { starts: {} };
  db.stats.starts[code] = (db.stats.starts[code] || 0) + 1;
}

// A valid deep-link payload (ig_reel_N, tt_reel_N, ig_bio, tt_bio) sets or replaces the source of the lead.
function setSource(lead, payload, now) {
  const source = parseSource(payload);
  if (source && (!lead.source || lead.source.code !== source.code)) {
    lead.source = { code: source.code, label: source.label, at: new Date(now).toISOString() };
  }
  return source;
}

// Counts the message against the daily limit. Returns false when the client is over the limit.
function countMessage(lead, now) {
  const today = new Date(now).toISOString().slice(0, 10);
  if (lead.day !== today) {
    lead.day = today;
    lead.day_count = 0;
  }
  lead.day_count += 1;
  return lead.day_count <= DAILY_LIMIT;
}

function push(lead, role, text, now) {
  lead.history.push({ role, text: clip(text, role === "user" ? MAX_TEXT : 1500), at: new Date(now).toISOString() });
  if (lead.history.length > MAX_HISTORY) lead.history.splice(0, lead.history.length - MAX_HISTORY);
  lead.updated = new Date(now).toISOString();
}

function parseModelJson(text) {
  const raw = String(text || "");
  const a = raw.indexOf("{");
  const b = raw.lastIndexOf("}");
  if (a >= 0 && b > a) {
    try {
      const o = JSON.parse(raw.slice(a, b + 1));
      if (o && typeof o === "object") return o;
    } catch (_) {
      /* fall through to the plain-text fallback */
    }
  }
  return { reply: raw.replace(/[{}"]/g, "").trim() };
}

// Merges what the model learned into the lead. Returns { upgraded, handoffNow }.
function applyProposal(lead, proposal) {
  const p = proposal && typeof proposal.profile === "object" && proposal.profile ? proposal.profile : {};
  const prof = lead.profile;
  for (const k of ["task", "company", "role", "timeline", "contact", "budget_note", "website", "contact_pref"]) {
    const v = clip(p[k], k === "task" ? 300 : 160);
    if (v) prof[k] = v;
  }
  const eur = Number(p.budget_eur);
  if (Number.isFinite(eur) && eur > 0 && eur < 100000000) prof.budget_eur = Math.round(eur);
  const lang = String((proposal && proposal.lang) || "").toLowerCase();
  if (LANGS.includes(lang)) lead.lang = lang;
  const summary = clip(proposal && proposal.summary, 400);
  if (summary) lead.summary = summary;

  const before = lead.tier;
  lead.tier = tierFromBudget(prof.budget_eur);
  if (lead.status === "new") lead.status = "in_dialog";
  let handoffNow = false;
  if (proposal && proposal.handoff && (prof.website || prof.task) && !lead.notified_handoff) {
    lead.status = "handoff";
    handoffNow = true;
  }
  return { upgraded: TIER_RANK[lead.tier] > TIER_RANK[before], handoffNow };
}

// ---------------------------------------------------------------- prompt context for the model

function clientContext(lead) {
  const prof = lead.profile;
  const known = [
    prof.website && `website: ${prof.website}`,
    prof.task && `main task / pain: ${prof.task}`,
    prof.contact_pref && `preferred contact: ${prof.contact_pref}`,
    prof.company && `company: ${prof.company}`,
    prof.role && `role: ${prof.role}`,
    prof.budget_eur && `budget: ${prof.budget_eur} EUR`,
    prof.budget_note && `budget note: ${prof.budget_note}`,
    prof.timeline && `timeline: ${prof.timeline}`,
    prof.contact && `contact: ${prof.contact}`,
  ].filter(Boolean);
  const lines = [];
  lines.push(lead.source ? `Came from: ${lead.source.label}.` : "Source of the visit is unknown.");
  lines.push(`Telegram name: ${lead.name || "not set"}${lead.username ? " (@" + lead.username + ")" : ""}. Language guess so far: ${lead.lang}.`);
  if (lead.funnel) lines.push(`The client went through the Spanish "motion" lead magnet (step ${lead.funnel.step}${lead.funnel.choice ? ", chose: " + lead.funnel.choice : ""}); keep answering in Spanish.`);
  lines.push(known.length ? `Already known: ${known.join("; ")}.` : "Nothing is known about the client yet.");
  const dialog = lead.history.slice(-14);
  if (dialog.length) lines.push("", "Dialogue so far:", ...dialog.map((h) => `${h.role === "user" ? "Client" : "Assistant"}: ${h.text}`));
  return lines.join("\n");
}

// ---------------------------------------------------------------- messages to the owner (plain text, no parse_mode)

function who(lead) {
  return `${lead.name || "Без имени"}${lead.username ? " @" + lead.username : ""} (id ${lead.id})`;
}

function newLeadNotice(lead, firstText) {
  return [
    "🆕 Новый клиент в боте",
    who(lead),
    `Источник: ${lead.source ? lead.source.label : "прямой заход / неизвестен"}`,
    `Первое сообщение: ${clip(firstText, 300) || "/start"}`,
  ].join("\n");
}

const AUDIT_BUTTONS = [
  ["in_work", "take", "Взять в работу"],
  ["audit_sent", "audit", "Отправить аудит"],
  ["done", "done", "Завершить"],
];

// Inline keyboard under the lead card. Reached steps get a check mark; a finished lead has no buttons.
function leadKeyboard(lead) {
  if (lead.audit_status === "done") return { inline_keyboard: [] };
  const reached = AUDIT_STATUSES.indexOf(lead.audit_status);
  return {
    inline_keyboard: [
      AUDIT_BUTTONS.map(([status, action, label]) => ({
        text: AUDIT_STATUSES.indexOf(status) <= reached ? "✔ " + label : label,
        callback_data: `ls:${action}:${lead.id}`,
      })),
    ],
  };
}

// Card for the owner when a client leaves a site for the free UX audit: Name | Language | URL | Source | Message.
function handoffNotice(lead) {
  const p = lead.profile;
  const lastUser = [...lead.history].reverse().find((h) => h.role === "user");
  return [
    "🔥 Новая заявка на UX-аудит",
    `👤 ${lead.name || "Без имени"}${lead.username ? " @" + lead.username : ""}`,
    `🌐 Язык: ${lead.lang.toUpperCase()}`,
    `🔗 ${p.website || "сайт не указан"}`,
    `📌 ${lead.source ? lead.source.label : "прямой заход / неизвестен"}`,
    `💬 ${clip(lastUser ? lastUser.text : p.task, 300) || "—"}`,
    "",
    `Задача: ${p.task || "—"}`,
    `Связь: ${p.contact_pref || "Telegram"}${p.contact ? " · " + p.contact : ""}${lead.username ? " · https://t.me/" + lead.username : ""} · tg://user?id=${lead.id}`,
    p.company || p.role ? `Компания/роль: ${[p.company, p.role].filter(Boolean).join(", ")}` : "",
    lead.summary ? `Резюме: ${lead.summary}` : "",
    p.budget_eur || p.budget_note ? `Бюджет: ${p.budget_eur ? p.budget_eur + " € · " : ""}${p.budget_note || TIER_RU[lead.tier]}` : "",
  ]
    .filter((l, i, a) => l !== "" || (i > 0 && a[i - 1] !== ""))
    .join("\n");
}

// /admin: one-screen dashboard for the owner.
function renderStats(db, now = Date.now()) {
  const list = Object.values(db.leads);
  const count = (arr, key) => arr.reduce((acc, l) => ((acc[key(l)] = (acc[key(l)] || 0) + 1), acc), {});
  const week = list.filter((l) => now - Date.parse(l.first_seen) < 7 * 86400000).length;
  const langs = count(list, (l) => l.lang || "en");
  const dialog = count(list, (l) => l.status);
  const audit = count(list.filter((l) => l.status === "handoff"), (l) => l.audit_status || "new");
  const starts = (db.stats && db.stats.starts) || {};
  const byLead = count(list.filter((l) => l.source), (l) => l.source.code);
  const direct = list.filter((l) => !l.source).length;
  const n = (o, k) => o[k] || 0;

  const codes = [...new Set([...Object.keys(starts), ...Object.keys(byLead)])];
  const group = (title, re) => {
    const rows = codes
      .filter((c) => re.test(c))
      .sort((a, b) => n(starts, b) - n(starts, a) || a.localeCompare(b, "en", { numeric: true }))
      .map((c) => `   ${c}: ${n(starts, c)} перех. · ${n(byLead, c)} лид.`);
    return rows.length ? [title, ...rows] : [];
  };

  return [
    "📊 Статистика бота",
    `Всего обращений: ${list.length} (за 7 дней: ${week})`,
    `Языки: ES ${n(langs, "es")} · EN ${n(langs, "en")} · RU ${n(langs, "ru")}`,
    `Диалоги: новые ${n(dialog, "new")} · в разговоре ${n(dialog, "in_dialog")} · передано мне ${n(dialog, "handoff")}`,
    `Заявки на аудит: ждут ${n(audit, "new")} · в работе ${n(audit, "in_work")} · отправлено ${n(audit, "audit_sent")} · завершено ${n(audit, "done")}`,
    "",
    "Источники (переходы по диплинкам):",
    ...group("▫️ Instagram reels", /^ig_reel_/),
    ...group("▫️ TikTok reels", /^tt_reel_/),
    ...group("▫️ Карусели", /_carousel_/),
    ...group("▫️ Профиль (bio)", /_bio$/),
    `▫️ Без источника: ${direct} лид.`,
    ...(funnelStats()(list) ? ["", funnelStats()(list)] : []),
  ].join("\n");
}

function renderLeads(db, n = 10) {
  const list = Object.values(db.leads).sort((a, b) => Date.parse(b.updated) - Date.parse(a.updated)).slice(0, n);
  if (!list.length) return "Лидов от внешних клиентов пока нет.";
  const mark = { premium: "🔥", standard: "✅", cold: "🧊", unknown: "•" };
  const lines = list.map(
    (l) =>
      `${mark[l.tier] || "•"} ${l.name || "Без имени"}${l.username ? " @" + l.username : ""} · ${l.updated.slice(0, 10)} · ${l.source ? l.source.code : "—"} · ${l.status}\n   ${clip(l.profile.task || l.summary || "задача не выяснена", 100)}`
  );
  return `📇 Лиды (последние ${list.length} из ${Object.keys(db.leads).length}):\n\n${lines.join("\n\n")}`;
}

module.exports = {
  DAILY_LIMIT, MAX_TEXT, STANDARD_EUR, PREMIUM_EUR, LANGS, AUDIT_STATUSES,
  emptyLeads, normalizeLeads, tierFromBudget, getLead, langFromCode, countStart, setSource, countMessage, push, parseModelJson, applyProposal,
  clientContext, newLeadNotice, handoffNotice, leadKeyboard, renderStats, renderLeads, clip,
};
