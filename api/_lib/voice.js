// Voice helpers: text-to-speech with the free Google Translate TTS endpoint (the one gTTS wraps) and an optional
// speech-to-text call. No npm dependencies, no ffmpeg: Telegram plays MP3 sent through sendVoice.
//
// Speech-to-text options (see api/telegram.js):
//   * default - fully free: the voice file is transcribed by open-source Whisper in GitHub Actions (agent/transcribe.py).
//   * optional fast path - GROQ_API_KEY (free tier of Groq, Whisper large-v3-turbo). The audio is sent to Groq.
// Telegram's own transcription is a Premium feature of the app and is not available to bots.

"use strict";

const TTS_CHUNK = 190;           // the endpoint rejects texts longer than about 200 characters
const SPEECH_MAX_CHARS = 1500;   // longer answers are shortened for the voice; the full text is sent as a message
const GOOGLE_LANG = {
  en: "en", ru: "ru", es: "es", de: "de", fr: "fr", pt: "pt", it: "it", nl: "nl", pl: "pl", tr: "tr", uk: "uk",
  ja: "ja", ko: "ko", zh: "zh-CN", ar: "ar", hi: "hi", id: "id", th: "th", vi: "vi",
};

// Removes everything a speech synthesizer would read out loud as noise: code, links, markup, emoji, ids in brackets.
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
    .replace(/[:;,]\s*([.!?])/g, "$1")    // "Подробности: ." (the link was removed) -> "Подробности."
    .replace(/([.!?…])(\s*\.)+/g, "$1")
    .trim();
  if (t.length > SPEECH_MAX_CHARS) {
    const cut = Math.max(t.lastIndexOf(". ", SPEECH_MAX_CHARS), t.lastIndexOf("! ", SPEECH_MAX_CHARS), t.lastIndexOf("? ", SPEECH_MAX_CHARS));
    t = t.slice(0, cut > 400 ? cut + 1 : SPEECH_MAX_CHARS).trim();
  }
  return t;
}

// Splits text into pieces of at most `max` characters, preferring sentence, then comma, then word boundaries.
function splitForTts(text, max = TTS_CHUNK) {
  const chunks = [];
  let rest = text.trim();
  while (rest.length > max) {
    const window = rest.slice(0, max + 1);
    let cut = Math.max(window.lastIndexOf(". "), window.lastIndexOf("! "), window.lastIndexOf("? "), window.lastIndexOf("; "));
    if (cut < max * 0.4) cut = Math.max(cut, window.lastIndexOf(", "), window.lastIndexOf(": "));
    if (cut < max * 0.3) cut = window.lastIndexOf(" ");
    if (cut < 1) cut = max - 1;
    chunks.push(rest.slice(0, cut + 1).trim());
    rest = rest.slice(cut + 1).trim();
  }
  if (rest) chunks.push(rest);
  return chunks.filter((c) => /[\p{L}\p{N}]/u.test(c));
}

// Script-based language guess for the synthesizer; Latin text falls back to the hint, then English.
function detectTtsLang(text, hint) {
  const letters = [...String(text || "")].filter((c) => /\p{L}/u.test(c));
  if (letters.length) {
    const share = (re) => letters.filter((c) => re.test(c)).length / letters.length;
    if (share(/[Ѐ-ӿ]/) > 0.3) return hint === "uk" ? "uk" : "ru";
    if (share(/[가-힯]/) > 0.2) return "ko";
    if (share(/[぀-ヿ]/) > 0.1) return "ja";
    if (share(/[一-鿿]/) > 0.3) return "zh-CN";
    if (share(/[؀-ۿ]/) > 0.3) return "ar";
    if (share(/[ऀ-ॿ]/) > 0.3) return "hi";
    if (share(/[฀-๿]/) > 0.3) return "th";
  }
  return GOOGLE_LANG[hint] || "en";
}

// Text -> one MP3 buffer. Throws when the synthesizer is unreachable or blocks the request.
async function ttsMp3(text, lang, doFetch = (...a) => globalThis.fetch(...a)) {
  const chunks = splitForTts(cleanForSpeech(text));
  if (!chunks.length) throw new Error("nothing to speak");
  const parts = [];
  for (let i = 0; i < chunks.length; i += 4) { // 4 parallel requests at a time
    const batch = chunks.slice(i, i + 4).map(async (chunk, j) => {
      const url = "https://translate.google.com/translate_tts?" + new URLSearchParams({
        ie: "UTF-8", tl: lang, client: "tw-ob", q: chunk, total: String(chunks.length), idx: String(i + j), textlen: String(chunk.length),
      });
      const res = await doFetch(url, { headers: { "User-Agent": "Mozilla/5.0", Referer: "http://translate.google.com/" } });
      const type = (res.headers && res.headers.get && res.headers.get("content-type")) || "audio/mpeg";
      if (!res.ok || !/audio/i.test(type)) throw new Error(`tts HTTP ${res.status}`);
      return Buffer.from(await res.arrayBuffer());
    });
    parts.push(...(await Promise.all(batch)));
  }
  return Buffer.concat(parts);
}

// Optional fast speech-to-text through the free tier of Groq (Whisper large-v3-turbo). Returns the text.
async function groqTranscribe(buffer, apiKey, doFetch = (...a) => globalThis.fetch(...a)) {
  const form = new FormData();
  form.append("file", new Blob([buffer], { type: "audio/ogg" }), "voice.ogg");
  form.append("model", process.env.GROQ_MODEL || "whisper-large-v3-turbo");
  form.append("response_format", "json");
  const res = await doFetch("https://api.groq.com/openai/v1/audio/transcriptions", {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}` },
    body: form,
  });
  if (!res.ok) throw new Error(`groq HTTP ${res.status}`);
  const data = await res.json();
  return String(data.text || "").trim();
}

module.exports = { cleanForSpeech, splitForTts, detectTtsLang, ttsMp3, groqTranscribe, TTS_CHUNK, SPEECH_MAX_CHARS };
