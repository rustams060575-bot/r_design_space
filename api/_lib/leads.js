// Public lead qualifier: state of conversations with EXTERNAL clients (people who come from Instagram / TikTok).
// Kept in leads.json in the same private repository as the owner's memory, but strictly separate from it:
// the client conversation never reads the owner's memory, and the owner's chat never reads this file by accident.

"use strict";

const { parseSource } = require("./memory");

const MAX_TEXT = 1200; // characters taken from one client message
const MAX_HISTORY = 24; // messages kept per client
const DAILY_LIMIT = 60; // messages per client per day: protects the Anthropic budget from abuse
const MAX_LEADS = 500;

const STANDARD_EUR = 1000; // minimum check
const PREMIUM_EUR = 3000; // premium

const emptyLeads = () => ({ version: 1, last_update_id: 0, leads: {} });

function normalizeLeads(raw) {
  const m = raw && typeof raw === "object" ? raw : {};
  return {
    version: 1,
    last_update_id: Number(m.last_update_id) || 0,
    leads: m.leads && typeof m.leads === "object" ? m.leads : {},
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
      tier: "unknown",
      profile: { task: "", company: "", role: "", budget_eur: null, budget_note: "", timeline: "", contact: "" },
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
  return db.leads[key];
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
  for (const k of ["task", "company", "role", "timeline", "contact", "budget_note"]) {
    const v = clip(p[k], k === "task" ? 300 : 160);
    if (v) prof[k] = v;
  }
  const eur = Number(p.budget_eur);
  if (Number.isFinite(eur) && eur > 0 && eur < 100000000) prof.budget_eur = Math.round(eur);
  const summary = clip(proposal && proposal.summary, 400);
  if (summary) lead.summary = summary;

  const before = lead.tier;
  lead.tier = tierFromBudget(prof.budget_eur);
  if (lead.status === "new") lead.status = "in_dialog";
  let handoffNow = false;
  if (proposal && proposal.handoff && prof.task && !lead.notified_handoff) {
    lead.status = "handoff";
    handoffNow = true;
  }
  return { upgraded: TIER_RANK[lead.tier] > TIER_RANK[before], handoffNow };
}

// ---------------------------------------------------------------- prompt context for the model

function clientContext(lead) {
  const prof = lead.profile;
  const known = [
    prof.task && `задача: ${prof.task}`,
    prof.company && `компания: ${prof.company}`,
    prof.role && `роль: ${prof.role}`,
    prof.budget_eur && `бюджет: ${prof.budget_eur} EUR`,
    prof.budget_note && `про бюджет: ${prof.budget_note}`,
    prof.timeline && `сроки: ${prof.timeline}`,
    prof.contact && `контакт: ${prof.contact}`,
  ].filter(Boolean);
  const lines = [];
  lines.push(lead.source ? `Пришёл из: ${lead.source.label}.` : "Источник перехода неизвестен.");
  lines.push(`Имя в Telegram: ${lead.name || "не указано"}${lead.username ? " (@" + lead.username + ")" : ""}.`);
  lines.push(known.length ? `Уже известно: ${known.join("; ")}.` : "Пока ничего не известно о задаче клиента.");
  const dialog = lead.history.slice(-14);
  if (dialog.length) lines.push("", "Диалог до этого момента:", ...dialog.map((h) => `${h.role === "user" ? "Клиент" : "Ассистент"}: ${h.text}`));
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

function handoffNotice(lead) {
  const p = lead.profile;
  const head = lead.tier === "premium" ? "🔥 ПРЕМИУМ-лид готов к связи" : lead.tier === "cold" ? "🧊 Лид (бюджет ниже минимума)" : "✅ Квалифицированный лид";
  return [
    head,
    who(lead),
    `Источник: ${lead.source ? lead.source.label : "неизвестен"}`,
    `Задача: ${p.task || "—"}`,
    `Компания/роль: ${[p.company, p.role].filter(Boolean).join(", ") || "—"}`,
    `Бюджет: ${p.budget_eur ? p.budget_eur + " €" : p.budget_note || "не назван"} · ${TIER_RU[lead.tier]}`,
    `Сроки: ${p.timeline || "—"}`,
    `Контакт: ${p.contact || "Telegram"}${lead.username ? " · https://t.me/" + lead.username : ""} · tg://user?id=${lead.id}`,
    lead.summary ? `Резюме: ${lead.summary}` : "",
  ]
    .filter(Boolean)
    .join("\n");
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
  DAILY_LIMIT, MAX_TEXT, STANDARD_EUR, PREMIUM_EUR,
  emptyLeads, normalizeLeads, tierFromBudget, getLead, setSource, countMessage, push, parseModelJson, applyProposal,
  clientContext, newLeadNotice, handoffNotice, renderLeads, clip,
};
