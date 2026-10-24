// Long-term memory: pure functions over a plain JSON document (no I/O).
//
// Shape of the document (stored as memory.json in a PRIVATE repository, see store.js):
//   { version, last_update_id, items: [ {id, type, title, text, tags[], project, created, updated} ],
//     projects: { "<name>": {summary, updated} }, history: [ {ts, role, text} ] }

"use strict";

const crypto = require("crypto");

const TYPES = ["project", "agreement", "interest", "fact", "note"];
const TYPE_RU = { project: "проект", agreement: "договорённость", interest: "интерес", fact: "факт", note: "заметка" };
const MAX_ITEMS = 500;
const MAX_HISTORY = 40;
const DAY = 86400000;

const STOP = new Set(
  ("и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из " +
    "ему когда даже ну ли если уже или ни быть был него до вас там потом себя ничего ей может они тут где есть надо для мы тебя " +
    "их чем была сам без чего раз тоже себе под будет тогда кто этот того это эти эту этой при про над после через нас всех " +
    "the and for are was with that this have from you your what about when where which who how про расскажи напомни скажи " +
    "помнишь какие какой какая какое было были").split(" ")
);

// Anything that looks like a credential is never written to memory.
const SECRET_PATTERNS = [
  /sk-[A-Za-z0-9_-]{20,}/, /gh[pousr]_[A-Za-z0-9]{20,}/, /github_pat_[A-Za-z0-9_]{20,}/,
  /\b\d{8,10}:[A-Za-z0-9_-]{30,}\b/, /AKIA[0-9A-Z]{16}/,
  // "пароль: x", "token=x" (no \b: it does not work for Cyrillic in JS)
  /(?:пароль|password|passwd|pwd|токен|token|api[_ -]?key|секрет|secret)\s*[:=]\s*\S+/iu,
  // "пароль от почты qwerty123": a password-like word containing a digit shortly after the keyword
  /(?:пароль|password|passwd|пин-?код|(?<!\p{L})pin(?![\p{L}])(?:-?(?:code|код))?)[^\p{N}\n]{0,25}(?=[A-Za-z0-9!@#$%^&*_-]*\d)[A-Za-z0-9!@#$%^&*_-]{6,}/iu,
  /\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b/,
];

function stem(t) {
  return t.length >= 5 ? t.slice(0, 4) : t; // crude prefix stemming: "клубам" / "клубов" / "клубе" -> "клуб"
}

function tokenize(text) {
  return (String(text || "").toLowerCase().match(/[\p{L}\p{N}]+/gu) || [])
    .filter((t) => t.length >= 3 && !STOP.has(t))
    .map(stem);
}

function hasSecret(text) {
  return SECRET_PATTERNS.some((re) => re.test(String(text || "")));
}

function emptyMemory() {
  return { version: 1, last_update_id: 0, items: [], projects: {}, history: [], lead_source: null };
}

function normalize(mem) {
  const m = mem && typeof mem === "object" ? mem : {};
  return {
    version: 1,
    last_update_id: Number(m.last_update_id) || 0,
    items: Array.isArray(m.items) ? m.items : [],
    projects: m.projects && typeof m.projects === "object" ? m.projects : {},
    history: Array.isArray(m.history) ? m.history : [],
    lead_source: m.lead_source && typeof m.lead_source === "object" ? m.lead_source : null,
  };
}

// ---------------------------------------------------------------- lead source (deep-link /start <payload>)

const SOURCE_PLATFORM = { ig: "Instagram", tt: "TikTok" };

// "ig_reel_201" / "tt_reel_7" -> a reel, "ig_carousel_3" / "tt_carousel_3" -> a carousel, "ig_bio" / "tt_bio" -> a profile link.
// Anything else is ignored.
function parseSource(payload) {
  const m = /^(ig|tt)_(?:(reel|carousel)_(\d{1,5})|(bio))$/.exec(String(payload || "").trim().toLowerCase());
  if (!m) return null;
  const platform = SOURCE_PLATFORM[m[1]];
  if (m[4]) return { code: `${m[1]}_bio`, platform, kind: "bio", num: null, reel: null, label: `${platform}, профиль (bio)` };
  const num = Number(m[3]);
  const reel = m[2] === "reel";
  return {
    code: `${m[1]}_${m[2]}_${num}`,
    platform,
    kind: m[2],
    num,
    reel: reel ? num : null,
    label: `${platform}, ${reel ? "ролик" : "карусель"} №${num}`,
  };
}

// ---------------------------------------------------------------- dates

const iso = (now) => new Date(now).toISOString();
const dateRu = (s) => {
  const d = new Date(s);
  return isNaN(d) ? "?" : `${String(d.getUTCDate()).padStart(2, "0")}.${String(d.getUTCMonth() + 1).padStart(2, "0")}.${d.getUTCFullYear()}`;
};

function ageText(s, now) {
  const days = Math.floor((now - Date.parse(s)) / DAY);
  if (!(days >= 0)) return "только что";
  if (days === 0) return "сегодня";
  if (days === 1) return "вчера";
  if (days < 14) return `${days} дн. назад`;
  if (days < 60) return `${Math.floor(days / 7)} нед. назад`;
  return `${Math.floor(days / 30)} мес. назад`;
}

// ---------------------------------------------------------------- retrieval

function scoreItem(item, qTokens, now) {
  const title = new Set(tokenize(item.title));
  const body = new Set(tokenize(item.text));
  const tags = new Set(tokenize(`${(item.tags || []).join(" ")} ${item.project || ""}`));
  let overlap = 0;
  for (const q of new Set(qTokens)) {
    if (title.has(q)) overlap += 2;
    if (tags.has(q)) overlap += 2;
    if (body.has(q)) overlap += 1;
  }
  const relevance = qTokens.length ? overlap / (new Set(qTokens).size * 2) : 0;
  const age = Math.max(0, (now - Date.parse(item.updated || item.created)) / DAY);
  const recency = Math.exp(-age / 90) * 0.35; // a memory from weeks ago is still found, just ranked a bit lower
  return { relevance, score: relevance + recency + (item.type === "agreement" ? 0.05 : 0) };
}

// Items relevant to the query (best first) plus the most recent ones for continuity.
function retrieve(mem, query, now, { limit = 8, recent = 3 } = {}) {
  const q = tokenize(query);
  const scored = mem.items.map((item) => ({ item, ...scoreItem(item, q, now) }));
  const relevant = scored.filter((s) => s.relevance > 0).sort((a, b) => b.score - a.score).slice(0, limit).map((s) => s.item);
  const latest = [...mem.items].sort((a, b) => Date.parse(b.updated) - Date.parse(a.updated)).slice(0, recent);
  const seen = new Set(relevant.map((i) => i.id));
  return { relevant, recent: latest.filter((i) => !seen.has(i.id)) };
}

// ---------------------------------------------------------------- writing

function jaccard(a, b) {
  if (!a.size || !b.size) return 0;
  let common = 0;
  for (const x of a) if (b.has(x)) common++;
  return common / (a.size + b.size - common);
}

function newId(now) {
  return "m" + now.toString(36).slice(-5) + crypto.randomBytes(2).toString("hex");
}

// Validates one memory proposed by the model. Returns null when it must not be stored.
function sanitize(m) {
  if (!m || typeof m !== "object") return null;
  const title = String(m.title || "").trim().slice(0, 80);
  const text = String(m.text || "").trim().slice(0, 400);
  if (!title || !text || hasSecret(`${title} ${text}`)) return null;
  return {
    update_id: /^m[a-z0-9]{3,20}$/.test(String(m.update_id || "")) ? String(m.update_id) : "",
    type: TYPES.includes(m.type) ? m.type : "note",
    title,
    text,
    tags: (Array.isArray(m.tags) ? m.tags : []).map((t) => String(t).trim().slice(0, 30)).filter(Boolean).slice(0, 6),
    project: String(m.project || "").trim().slice(0, 60),
  };
}

function upsert(mem, incoming, now) {
  const inc = sanitize(incoming);
  if (!inc) return null;
  const stamp = iso(now);
  let target = inc.update_id ? mem.items.find((i) => i.id === inc.update_id) : null;
  if (!target) {
    const t = new Set(tokenize(inc.title));
    target = mem.items.find((i) => i.type === inc.type && jaccard(new Set(tokenize(i.title)), t) >= 0.7);
  }
  if (target) {
    Object.assign(target, {
      type: inc.type, title: inc.title, text: inc.text, project: inc.project || target.project || "",
      tags: [...new Set([...(target.tags || []), ...inc.tags])].slice(0, 8), updated: stamp,
    });
    return target;
  }
  const item = { id: newId(now), type: inc.type, title: inc.title, text: inc.text, tags: inc.tags, project: inc.project, created: stamp, updated: stamp };
  mem.items.push(item);
  if (mem.items.length > MAX_ITEMS) { // drop the oldest low-value entries first
    const weight = (i) => (["agreement", "project"].includes(i.type) ? 1 : 0);
    mem.items.sort((a, b) => weight(a) - weight(b) || Date.parse(a.updated) - Date.parse(b.updated));
    mem.items = mem.items.slice(mem.items.length - MAX_ITEMS);
  }
  return item;
}

function touchProject(mem, name, summary, now) {
  name = String(name || "").trim().slice(0, 60);
  if (!name) return;
  const old = mem.projects[name] || {};
  mem.projects[name] = { summary: String(summary || old.summary || "").slice(0, 200), updated: iso(now) };
}

function forget(mem, id) {
  const i = mem.items.findIndex((x) => x.id === id);
  return i < 0 ? null : mem.items.splice(i, 1)[0];
}

function pushHistory(mem, role, text, now) {
  mem.history.push({ ts: iso(now), role, text: String(text || "").slice(0, 600) });
  mem.history = mem.history.slice(-MAX_HISTORY);
}

// Applies the model's JSON proposal; returns the stored items.
function applyProposal(mem, proposal, now) {
  const stored = [];
  for (const m of proposal.memories || []) {
    const item = upsert(mem, m, now);
    if (item) {
      stored.push(item);
      if (item.project) touchProject(mem, item.project, (proposal.project_summaries || {})[item.project], now);
    }
  }
  for (const [name, summary] of Object.entries(proposal.project_summaries || {})) {
    if (mem.projects[name]) touchProject(mem, name, summary, now);
  }
  return stored;
}

// ---------------------------------------------------------------- prompt context

function contextBlock(mem, query, now) {
  const weekday = new Date(now).toLocaleDateString("ru-RU", { weekday: "long", timeZone: "UTC" });
  const lines = [`Сегодня: ${iso(now).slice(0, 10)} (${weekday}), UTC.`];

  const projects = Object.entries(mem.projects).sort((a, b) => Date.parse(b[1].updated) - Date.parse(a[1].updated)).slice(0, 8);
  lines.push("", "Активные проекты:");
  lines.push(...(projects.length ? projects.map(([n, p]) => `- ${n} (обновлено ${ageText(p.updated, now)})${p.summary ? ": " + p.summary : ""}`) : ["(пока нет)"]));

  const { relevant, recent } = retrieve(mem, query, now);
  const fmt = (i) => `- [${i.id}] ${iso(Date.parse(i.updated)).slice(0, 10)} · ${TYPE_RU[i.type] || i.type}${i.project ? " · " + i.project : ""} · ${i.title}: ${i.text}`;
  lines.push("", "Записи памяти, похожие на вопрос:", ...(relevant.length ? relevant.map(fmt) : ["(ничего подходящего)"]));
  if (recent.length) lines.push("", "Самые свежие записи:", ...recent.map(fmt));

  if (mem.lead_source) lines.push("", `Источник обращения (deep-link /start, ${dateRu(mem.lead_source.at)}): ${mem.lead_source.label}. Учитывай тему этого ролика при квалификации.`);

  const dialog = mem.history.slice(-6);
  if (dialog.length) lines.push("", "Недавний диалог:", ...dialog.map((h) => `${h.role === "user" ? "Пользователь" : "Ассистент"}: ${h.text}`));
  return lines.join("\n");
}

// ---------------------------------------------------------------- Telegram views (plain text, phone friendly)

const LINE = "━━━━━━━━━━━━━━";
const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

function renderOverview(mem, now) {
  const projects = Object.entries(mem.projects).sort((a, b) => Date.parse(b[1].updated) - Date.parse(a[1].updated)).slice(0, 6);
  const byType = (type, n) => mem.items.filter((i) => i.type === type).sort((a, b) => Date.parse(b.updated) - Date.parse(a.updated)).slice(0, n);
  const out = ["🧠 Что я помню", LINE, ""];
  if (!mem.items.length && !projects.length) return out.concat(["Пока пусто. Расскажите о проектах и договорённостях - я запомню главное.", "", "Подсказка: /remember <текст> сохраняет заметку сразу."]).join("\n");

  out.push("📁 Активные проекты");
  out.push(...(projects.length ? projects.map(([n, p]) => {
    const count = mem.items.filter((i) => i.project === n).length;
    return `• ${n} · ${count} зап. · ${ageText(p.updated, now)}${p.summary ? "\n   " + clip(p.summary, 140) : ""}`;
  }) : ["• -"]));

  const section = (title, items) => items.length ? ["", title, ...items.map((i) => `• ${dateRu(i.updated)} [${i.id}] ${i.title}: ${clip(i.text, 120)}`)] : [];
  out.push(...section("🤝 Договорённости", byType("agreement", 5)));
  out.push(...section("⭐ Интересы", byType("interest", 4)));
  out.push(...section("📄 Проекты и документы", byType("project", 4)));
  out.push("", `Всего записей: ${mem.items.length}. /history - хронология, /forget <id> - удалить.`);
  return clip(out.join("\n"), 3900);
}

function renderTimeline(mem, n, now) {
  const items = [...mem.items].sort((a, b) => Date.parse(b.updated) - Date.parse(a.updated)).slice(0, n);
  if (!items.length) return "🕘 История пуста.";
  return clip(["🕘 Хронология памяти", LINE, ...items.map((i) => `${dateRu(i.updated)} · ${TYPE_RU[i.type] || i.type}${i.project ? " · " + i.project : ""}\n[${i.id}] ${i.title}: ${clip(i.text, 140)}`)].join("\n\n"), 3900);
}

function renderSearch(mem, query, now) {
  const { relevant } = retrieve(mem, query, now, { limit: 8, recent: 0 });
  if (!relevant.length) return `🔍 По запросу «${query}» в памяти ничего не нашлось.`;
  return clip([`🔍 Нашлось по «${query}»`, LINE, ...relevant.map((i) => `${dateRu(i.updated)} · ${TYPE_RU[i.type] || i.type}\n[${i.id}] ${i.title}: ${clip(i.text, 160)}`)].join("\n\n"), 3900);
}

// ---------------------------------------------------------------- model output

function parseModelJson(text) {
  const m = /\{[\s\S]*\}/.exec(String(text || ""));
  if (m) {
    try {
      const data = JSON.parse(m[0]);
      if (typeof data.reply === "string" && data.reply.trim()) {
        return { reply: data.reply.trim(), memories: Array.isArray(data.memories) ? data.memories : [], project_summaries: data.project_summaries && typeof data.project_summaries === "object" ? data.project_summaries : {} };
      }
    } catch (_) { /* fall through */ }
  }
  // Truncated or malformed JSON: never show raw JSON to the user, salvage the "reply" string if it is there.
  const part = /"reply"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(String(text || ""));
  if (part) {
    let reply = part[1];
    try { reply = JSON.parse(`"${reply}"`); } catch (_) { reply = reply.replace(/\\n/g, "\n").replace(/\\"/g, '"'); }
    if (reply.trim()) return { reply: reply.trim(), memories: [], project_summaries: {} };
  }
  return { reply: String(text || "").trim() || "Не получилось сформулировать ответ, попробуйте ещё раз.", memories: [], project_summaries: {} };
}

module.exports = {
  TYPES, emptyMemory, normalize, parseSource, tokenize, hasSecret, retrieve, upsert, sanitize, forget, pushHistory, applyProposal,
  touchProject, contextBlock, renderOverview, renderTimeline, renderSearch, parseModelJson, dateRu, ageText,
};
