// Minimal Claude (Anthropic Messages API) client for the Telegram assistant.

"use strict";

async function askClaude({ apiKey, model, system, user, maxTokens = 1200, fetchImpl }) {
  const doFetch = fetchImpl || ((...a) => globalThis.fetch(...a));
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 45000); // Vercel function limit is 60s (vercel.json)
  try {
    const res = await doFetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      signal: ctrl.signal,
      headers: { "x-api-key": apiKey, "anthropic-version": "2023-06-01", "content-type": "application/json" },
      body: JSON.stringify({ model, max_tokens: maxTokens, system, messages: [{ role: "user", content: user }] }),
    });
    if (!res.ok) throw new Error(`Claude API HTTP ${res.status}`);
    const data = await res.json();
    return (data.content || []).filter((b) => b.type === "text").map((b) => b.text).join("");
  } finally {
    clearTimeout(timer);
  }
}

module.exports = { askClaude };
