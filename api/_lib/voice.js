// Voice helpers on the OpenAI API: speech-to-text (Whisper) and text-to-speech (tts-1).
// Direct HTTPS calls from the serverless function: no queues, no GitHub Actions, no npm dependencies.
//
// Telegram voice notes are OGG/Opus. Whisper accepts that format as is, and the TTS endpoint can answer in Opus (an
// OGG container), which is exactly what sendVoice plays as a voice message, so no ffmpeg or transcoding is needed.
//
// Every call has its own timeout so that a slow API can never hold the function until Vercel kills it.

"use strict";

const OPENAI = "https://api.openai.com/v1";
const SPEECH_MAX_CHARS = 1200; // a spoken answer should last well under a minute; the full text is sent as a message
const STT_TIMEOUT_MS = 20000;
const TTS_TIMEOUT_MS = 20000;

// Domain hint for Whisper: improves the spelling of product and business terms. Language is detected automatically.
const STT_PROMPT = "R Design Space, Chief of Staff, CRM, SaaS, B2B, dashboard, дашборд, лендинг, Tilda Zero Block, Figma, UI/UX, дедлайн, лид.";

class ApiError extends Error {
  constructor(service, status) {
    super(`${service} HTTP ${status}`);
    this.service = service;
    this.status = status;
  }
  // A short explanation for the user; never includes keys or response bodies.
  get hint() {
    if (this.status === 401) return `ключ ${this.service === "openai" ? "OPENAI_API_KEY" : "API"} не принят (проверьте значение в Vercel)`;
    if (this.status === 429) return "лимит запросов или недостаточно средств на балансе OpenAI";
    if (this.status === 413) return "аудио слишком большое";
    return `ошибка сервиса (HTTP ${this.status})`;
  }
}

async function request(url, init, timeoutMs, doFetch) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await doFetch(url, { ...init, signal: ctrl.signal });
  } catch (err) {
    if (err && err.name === "AbortError") throw new Error(`timeout after ${timeoutMs / 1000}s`);
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

// Removes what a speech synthesizer would read aloud as noise: code, links, markup, emoji, ids in brackets.
function cleanForSpeech(text) {
  let t = String(text || "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/\[m[a-z0-9]{3,20}\]/g, " ")
    .replace(/[\p{Extended_Pictographic}‍️]/gu, " ")
    .replace(/[*_`~]+/g, "")
    .replace(/[#>|]+/g, " ")
    .replace(/^\s*[•\-–—]\s+/gm, "")
    .replace(/\n+/g, ". ")
    .replace(/\s+/g, " ")
    .replace(/\s+([.,:;!?])/g, "$1")      // "слово ." -> "слово."
    .replace(/[:;,]\s*([.!?])/g, "$1")    // "Подробности: ." (a link was removed) -> "Подробности."
    .replace(/([.!?…])(\s*\.)+/g, "$1")
    .trim();
  if (t.length > SPEECH_MAX_CHARS) {
    const cut = Math.max(t.lastIndexOf(". ", SPEECH_MAX_CHARS), t.lastIndexOf("! ", SPEECH_MAX_CHARS), t.lastIndexOf("? ", SPEECH_MAX_CHARS));
    t = t.slice(0, cut > 300 ? cut + 1 : SPEECH_MAX_CHARS).trim();
  }
  return t;
}

// OGG/Opus voice note -> text (OpenAI Whisper, POST /v1/audio/transcriptions).
async function transcribe(buffer, apiKey, { model = "whisper-1", timeoutMs = STT_TIMEOUT_MS, doFetch = (...a) => globalThis.fetch(...a) } = {}) {
  const form = new FormData();
  form.append("file", new Blob([buffer], { type: "audio/ogg" }), "voice.ogg");
  form.append("model", model);
  form.append("response_format", "json");
  form.append("prompt", STT_PROMPT);
  const res = await request(`${OPENAI}/audio/transcriptions`, { method: "POST", headers: { Authorization: `Bearer ${apiKey}` }, body: form }, timeoutMs, doFetch);
  if (!res.ok) throw new ApiError("openai", res.status);
  return String((await res.json()).text || "").trim();
}

// Text -> OGG/Opus bytes (OpenAI tts-1, POST /v1/audio/speech), ready for Telegram sendVoice.
async function speech(text, apiKey, { model = "tts-1", voice = "alloy", timeoutMs = TTS_TIMEOUT_MS, doFetch = (...a) => globalThis.fetch(...a) } = {}) {
  const input = cleanForSpeech(text);
  if (!input) throw new Error("nothing to speak");
  const res = await request(`${OPENAI}/audio/speech`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}`, "content-type": "application/json" },
    body: JSON.stringify({ model, voice, input, response_format: "opus" }),
  }, timeoutMs, doFetch);
  if (!res.ok) throw new ApiError("openai", res.status);
  return Buffer.from(await res.arrayBuffer());
}

module.exports = { cleanForSpeech, transcribe, speech, ApiError, SPEECH_MAX_CHARS };
