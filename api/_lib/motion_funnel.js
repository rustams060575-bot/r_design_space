// FUNNEL_MOTION_SPAIN: Spanish lead magnet for the Reels / TikTok funnel. Trigger: "motion", "/motion" or the deep link
// t.me/r_design_outreach_bot?start=motion (optionally motion_<source>, e.g. motion_tt_reel_12).
//
// James (the studio assistant) hands out four short pieces of the method, one per message, each behind an inline button:
//   wireframe -> estetica -> codigo -> motion, then asks a qualifying question. Choosing "renew" or "new" notifies the
//   admin chat at once and asks the client for a short description and a way to be contacted; the reply is forwarded too.
// Voice messages (OpenAI TTS, Spanish) are sent on the greeting and right before the qualifying question.
//
// The module has no storage or network code of its own: client_flow.js injects everything through `deps`:
//   tg(method, payload)            Telegram Bot API call
//   sendVoice(chat, ogg)           sends an OGG/Opus buffer as a voice message
//   tts(text) -> Promise<Buffer|null>   never rejects; null when no audio could be produced
//   update(mutator, commitMessage) leads.json read-modify-write (undefined when storage is not configured)
//   notifyOwner(text, reply_markup)
// Step buttons carry the next step in callback_data ("mf:est"), so the funnel keeps working even if storage is down;
// stored state only protects against double presses and tells which message the client is answering.

"use strict";

const leads = require("./leads");

const FUNNEL = "FUNNEL_MOTION_SPAIN";
const STEPS = ["wireframe", "estetica", "codigo", "motion", "qualify", "await_contact", "done"];
const CONTACT_TTL_MS = 48 * 3600 * 1000; // an unanswered "describe your project" prompt expires after two days

// ---------------------------------------------------------------- texts (Castilian, informal "tú", quiet luxury)

const ES = {
  intro: "¡Hola! Me llamo James, soy parte del equipo y estoy aquí para ayudarte a llevar tu proyecto al siguiente nivel.",
  introAfter: "Te dejo el método con el que diseñamos interfaces de nivel premium: cuatro pasos breves, uno por mensaje. Empezamos.",
  restart: "Retomamos el método desde el principio.",

  wireframe:
    "Paso 1 de 4 · Wireframe\n\n" +
    "Una interfaz premium se decide antes de abrir Figma: en la estructura.\n\n" +
    "1. Un objetivo por pantalla. Si no puedes decir en una frase qué debe hacer el usuario ahí, sobra algo.\n" +
    "2. Jerarquía antes que decoración. Ordena el contenido en tres niveles: lo que se lee primero, lo que se lee después y lo que apoya. Lo demás se elimina.\n" +
    "3. Una sola acción principal por vista. El resto de acciones son secundarias y se nota.\n" +
    "4. Ritmo y espacio. Define una rejilla de 12 columnas y una escala de espacios (8, 16, 24, 48, 96) y no te salgas de ella.\n" +
    "5. Pruébalo en gris. Si la pantalla funciona sin color, sin imágenes y sin tipografía bonita, la estructura es sólida.",

  estetica:
    "Paso 2 de 4 · Estética\n\n" +
    "El lujo en una interfaz no grita: se nota en lo que decides no poner.\n\n" +
    "1. Paleta contenida: un neutro, un color de acento y nada más. Contraste de texto de al menos 4,5:1.\n" +
    "2. Tipografía con criterio: dos familias como máximo, por ejemplo una serif editorial para titulares y una sans limpia para el texto, con una escala modular fija.\n" +
    "3. El espacio vacío es un material. Márgenes generosos, bloques que respiran y un título que no compite con nada.\n" +
    "4. Profundidad sutil: sombras suaves y difusas, bordes de 1 px de baja opacidad, nada de degradados estridentes.\n" +
    "5. Imágenes con intención: pocas, bien recortadas y con el mismo tratamiento en todo el sitio.\n" +
    "6. Todo sale de tokens (color, tamaño, radio, sombra). Si algo no está en el sistema, no existe.",

  codigo:
    "Paso 3 de 4 · Código\n\n" +
    "Una plantilla se reconoce a primera vista. Una maquetación a medida, también, pero por lo contrario.\n\n" +
    "1. HTML semántico y componentes propios, sin constructores ni bloques genéricos.\n" +
    "2. Los tokens del diseño pasan tal cual al CSS como variables (custom properties): un cambio en un sitio, coherencia en todo el proyecto.\n" +
    "3. CSS moderno: grid, clamp() para tipografía y espacios fluidos, container queries. Menos hacks, menos código.\n" +
    "4. Rendimiento como parte del diseño: imágenes en AVIF o WebP, fuentes con font-display, nada de librerías pesadas para lo que resuelve el navegador. Objetivo: LCP por debajo de 2,5 s.\n" +
    "5. Accesibilidad de serie: foco visible, navegación por teclado, textos alternativos.\n" +
    "6. Mobile first. La mayoría de tus visitas llegan desde el móvil, y es ahí donde se decide la primera impresión.",

  motion:
    "Paso 4 de 4 · Motion\n\n" +
    "El movimiento es lo que convierte una página correcta en una interfaz que se siente viva.\n\n" +
    "1. Animar con propósito: cada movimiento explica algo (de dónde viene, qué cambió, qué es clicable). Si solo decora, se quita.\n" +
    "2. Tiempos: 150 a 300 ms para microinteracciones y 400 a 700 ms para transiciones de sección. Más largo se siente lento.\n" +
    "3. Curvas, nunca lineal. Un ease-out suave, como cubic-bezier(0.22, 1, 0.36, 1), da esa sensación de peso y calma.\n" +
    "4. Coreografía: los elementos entran en cascada, con 40 a 80 ms entre uno y otro, siguiendo la jerarquía del paso 1.\n" +
    "5. Solo transform y opacity, para mantener 60 fps incluso en móviles modestos.\n" +
    "6. Respeta prefers-reduced-motion: el lujo también es no marear a nadie.\n" +
    "7. Una firma de movimiento: un gesto reconocible que se repite en todo el producto.",

  voiceBridge: "Y eso es todo el método.",
  question: "¿Tienes un proyecto en marcha o un sitio web que necesite una renovación total para alcanzar este nivel?",

  askDetails:
    "Excelente. Cuéntame en un par de líneas en qué consiste tu proyecto (y, si ya existe, la URL de tu web) y dime cómo prefieres que te contactemos: aquí en Telegram, por correo o por WhatsApp.",
  thanks:
    "Gracias, lo tengo todo. Paso tu proyecto al equipo y te escribiremos personalmente muy pronto. Si quieres añadir algo más, escríbelo aquí.",
  consult:
    "Con mucho gusto. Sin compromiso: escríbeme tu duda sobre diseño, desarrollo o animación de interfaces y te oriento. Si más adelante quieres repasar el método, escribe «motion».",
};

const BTN = {
  estetica: "Siguiente paso: Estética ➔",
  codigo: "Siguiente paso: Código ➔",
  motion: "Paso final: Motion ➔",
  renew: "Sí, quiero renovar mi web",
  fresh: "Tengo un proyecto desde cero",
  consult: "Solo quiero consultar",
};

// callback_data -> what the press means
const CB = {
  "mf:est": { to: "estetica" },
  "mf:cod": { to: "codigo" },
  "mf:mot": { to: "motion" },
  "mf:renew": { choice: "renew" },
  "mf:new": { choice: "new" },
  "mf:consult": { choice: "consult" },
};
const CHOICE_LABEL = { renew: "renovación de su web", new: "proyecto desde cero", consult: "solo quiere consultar" };

const kb = (text, data) => ({ inline_keyboard: [[{ text, callback_data: data }]] });
const QUESTION_KEYBOARD = {
  inline_keyboard: [
    [{ text: BTN.renew, callback_data: "mf:renew" }],
    [{ text: BTN.fresh, callback_data: "mf:new" }],
    [{ text: BTN.consult, callback_data: "mf:consult" }],
  ],
};

// ---------------------------------------------------------------- trigger

// Returns { source } when the text starts the funnel, otherwise null. Only the bare word "motion" counts (not a sentence).
function detectTrigger(text) {
  const t = String(text || "").trim();
  let m = /^\/start(?:@\w+)?\s+motion(?:_(\S{1,60}))?$/i.exec(t);
  if (m) return { source: m[1] || "" };
  if (/^\/motion(?:@\w+)?$/i.test(t)) return { source: "" };
  if (/^[¡!¿?"'«“\s]*motion[\s.,!¡"'»”]*$/i.test(t)) return { source: "" };
  return null;
}

const stepIndex = (s) => STEPS.indexOf(s);
const iso = (ms) => new Date(ms).toISOString();

function isAwaitingContact(lead, now = Date.now()) {
  const f = lead && lead.funnel;
  return !!f && f.name === FUNNEL && f.step === "await_contact" && now - Date.parse(f.updated_at || 0) < CONTACT_TTL_MS;
}

// ---------------------------------------------------------------- helpers

// Runs `fn(lead, db)` inside a storage update. Returns the lead as stored, or null when storage is missing or failed.
async function mutate(deps, from, chat, fn, commitMessage) {
  if (!deps.update) return null;
  let lead = null;
  try {
    await deps.update((d) => {
      lead = leads.getLead(d, from, chat, deps.now());
      return fn(lead, d);
    }, commitMessage);
  } catch (err) {
    console.error("funnel save error:", err && err.message);
    return null;
  }
  return lead;
}

async function sayVoice(deps, chat, audioPromise) {
  try {
    const audio = await audioPromise;
    if (audio) await deps.sendVoice(chat, audio);
  } catch (err) {
    console.error("funnel voice error:", err && err.message); // the text has already been delivered
  }
}

const who = (lead, from) => {
  const username = (lead && lead.username) || (from && from.username) || "";
  const name = (lead && lead.name) || [from && from.first_name, from && from.last_name].filter(Boolean).join(" ") || "Sin nombre";
  return { username, name, id: lead ? lead.id : String(from && from.id) };
};

// ---------------------------------------------------------------- entry point: trigger -> greeting + step 1

// msg: Telegram message; trigger: result of detectTrigger(); updateId: for de-duplication of redelivered updates.
async function start(deps, msg, trigger, updateId) {
  const chat = msg.chat.id;
  const now = deps.now();
  let first = true;
  let announce = false;
  const lead = await mutate(
    deps,
    msg.from,
    chat,
    (l, d) => {
      first = !l.funnel;
      announce = !l.notified_new;
      l.notified_new = true;
      if (trigger.source) leads.setSource(l, trigger.source, now);
      l.lang = "es";
      if (l.status === "new") l.status = "in_dialog";
      l.funnel = { name: FUNNEL, step: "wireframe", reached: Math.max((l.funnel && l.funnel.reached) || 0, 1), choice: "", started_at: (l.funnel && l.funnel.started_at) || iso(now), updated_at: iso(now) };
      leads.push(l, "user", msg.text, now);
      leads.push(l, "assistant", "[funnel motion] paso 1: wireframe", now);
      if (updateId) d.last_update_id = Math.max(d.last_update_id || 0, updateId);
    },
    `leads: funnel start ${chat} [skip ci]`
  );
  if (!lead) first = true; // storage is down: greet in full, the funnel itself still works

  await deps.tg("sendChatAction", { chat_id: chat, action: first ? "record_voice" : "typing" });
  if (first) {
    const audio = deps.tts(`${ES.intro} ${ES.introAfter}`);
    await deps.tg("sendMessage", { chat_id: chat, text: `${ES.intro}\n\n${ES.introAfter}` });
    await sayVoice(deps, chat, audio);
  } else {
    await deps.tg("sendMessage", { chat_id: chat, text: ES.restart });
  }
  await deps.tg("sendMessage", { chat_id: chat, text: ES.wireframe, reply_markup: kb(BTN.estetica, "mf:est") });
  if (lead && announce) await deps.notifyOwner(leads.newLeadNotice(lead, msg.text));
}

// ---------------------------------------------------------------- inline buttons

async function onCallback(deps, cb) {
  const chat = cb.message && cb.message.chat && cb.message.chat.id;
  const action = CB[cb.data];
  if (!chat || !cb.message.chat.type || cb.message.chat.type !== "private" || !action) {
    return deps.tg("answerCallbackQuery", { callback_query_id: cb.id });
  }
  const now = deps.now();
  const target = action.to || "await_contact";

  // A button is accepted once: if the stored funnel is already at (or past) the target step, it was pressed before.
  let duplicate = false;
  let lead = await mutate(
    deps,
    cb.from,
    chat,
    (l) => {
      duplicate = false;
      const f = l.funnel;
      if (f && f.name === FUNNEL && stepIndex(f.step) >= stepIndex(target)) {
        duplicate = true;
        return;
      }
      l.lang = "es";
      l.funnel = { ...(f || { name: FUNNEL, choice: "", started_at: iso(now) }), name: FUNNEL, step: action.choice ? "await_contact" : target, updated_at: iso(now) };
      if (action.to) l.funnel.reached = Math.max(l.funnel.reached || 0, stepIndex(target) + 1);
      if (action.choice) {
        l.funnel.choice = action.choice;
        if (action.choice === "consult") l.funnel.step = "done";
        l.funnel.reached = Math.max(l.funnel.reached || 0, 5);
        l.status = action.choice === "consult" ? l.status : "handoff";
        if (action.choice !== "consult") l.notified_handoff = true;
      }
    },
    `leads: funnel ${cb.data} ${chat} [skip ci]`
  );
  await deps.tg("answerCallbackQuery", { callback_query_id: cb.id });
  if (duplicate) return;

  // The pressed button disappears so that a step cannot be triggered twice.
  await deps.tg("editMessageReplyMarkup", { chat_id: chat, message_id: cb.message.message_id, reply_markup: { inline_keyboard: [] } });

  if (action.to === "estetica") return deps.tg("sendMessage", { chat_id: chat, text: ES.estetica, reply_markup: kb(BTN.codigo, "mf:cod") });
  if (action.to === "codigo") return deps.tg("sendMessage", { chat_id: chat, text: ES.codigo, reply_markup: kb(BTN.motion, "mf:mot") });

  if (action.to === "motion") {
    // Final material, then a voice message and the qualifying question (buttons come last).
    await deps.tg("sendChatAction", { chat_id: chat, action: "record_voice" });
    const audio = deps.tts(`${ES.voiceBridge} ${ES.question}`);
    await deps.tg("sendMessage", { chat_id: chat, text: ES.motion });
    await sayVoice(deps, chat, audio);
    await deps.tg("sendMessage", { chat_id: chat, text: ES.question, reply_markup: QUESTION_KEYBOARD });
    if (lead) {
      await mutate(
        deps,
        cb.from,
        chat,
        (l) => {
          if (l.funnel && l.funnel.step === "motion") l.funnel.step = "qualify";
        },
        `leads: funnel qualify ${chat} [skip ci]`
      );
    }
    return;
  }

  // Answer to the qualifying question.
  const p = who(lead, cb.from);
  if (action.choice === "consult") {
    if (lead) leads.push(lead, "user", `[funnel motion] ${BTN.consult}`, now);
    return deps.tg("sendMessage", { chat_id: chat, text: ES.consult });
  }
  await deps.tg("sendMessage", { chat_id: chat, text: ES.askDetails });
  const who_ = p.username ? `@${p.username}` : `${p.name} (tg://user?id=${p.id})`;
  await deps.notifyOwner(
    `🔥 ¡Nuevo lead desde TikTok/Instagram! Interesado en desarrollo/renovación web. Usuario: ${who_}\n` +
      `Воронка motion (ES) · выбор: ${CHOICE_LABEL[action.choice]}${lead && lead.source ? " · источник: " + lead.source.label : ""}\n` +
      "Жду от клиента описание задачи и контакт - придёт следующим сообщением."
  );
}

// ---------------------------------------------------------------- free text while the funnel waits for details

async function onDetails(deps, msg, updateId) {
  const chat = msg.chat.id;
  const now = deps.now();
  const text = leads.clip(msg.text, leads.MAX_TEXT);
  let duplicate = false;
  let wasWaiting = true;
  const lead = await mutate(
    deps,
    msg.from,
    chat,
    (l, d) => {
      duplicate = false;
      wasWaiting = true;
      if (updateId && updateId <= d.last_update_id) {
        duplicate = true;
        return;
      }
      if (!isAwaitingContact(l, now)) {
        wasWaiting = false;
        return;
      }
      l.funnel.step = "done";
      l.funnel.details = text;
      l.funnel.updated_at = iso(now);
      l.profile.task = leads.clip(`${CHOICE_LABEL[l.funnel.choice] || ""}: ${text}`, 300);
      l.updated = iso(now);
      leads.push(l, "user", text, now);
      leads.push(l, "assistant", ES.thanks, now);
      if (updateId) d.last_update_id = Math.max(d.last_update_id || 0, updateId);
    },
    `leads: funnel details ${chat} [skip ci]`
  );
  if (duplicate) return true;
  if (!lead || !wasWaiting) return false; // not ours: let the normal qualifier answer

  await deps.tg("sendMessage", { chat_id: chat, text: ES.thanks });
  const p = who(lead, msg.from);
  await deps.notifyOwner(
    [
      "📩 Детали лида из воронки motion (ES)",
      `👤 ${p.name}${p.username ? " @" + p.username : ""}`,
      `🎯 Выбор: ${CHOICE_LABEL[lead.funnel.choice] || "—"}`,
      `📌 ${lead.source ? lead.source.label : "источник не определён"}`,
      `💬 ${leads.clip(text, 600)}`,
      "",
      `Связь: ${p.username ? "https://t.me/" + p.username + " · " : ""}tg://user?id=${p.id}`,
    ].join("\n"),
    leads.leadKeyboard(lead)
  );
  return true;
}

// ---------------------------------------------------------------- /admin line

function renderFunnelStats(list) {
  const f = list.filter((l) => l.funnel && l.funnel.name === FUNNEL);
  if (!f.length) return "";
  const reached = (n) => f.filter((l) => (l.funnel.reached || 0) >= n).length;
  const choice = (c) => f.filter((l) => l.funnel.choice === c).length;
  return (
    `Воронка motion (ES): старт ${f.length} · шаг 2: ${reached(2)} · шаг 3: ${reached(3)} · шаг 4: ${reached(4)} · вопрос: ${reached(5)}\n` +
    `   выбор: обновить сайт ${choice("renew")} · с нуля ${choice("new")} · консультация ${choice("consult")}`
  );
}

module.exports = { FUNNEL, STEPS, ES, BTN, detectTrigger, isAwaitingContact, start, onCallback, onDetails, renderFunnelStats };
