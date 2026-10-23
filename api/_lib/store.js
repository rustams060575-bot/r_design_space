// Persistent storage for memory.json: a file in a PRIVATE GitHub repository, accessed through the Contents API.
// No database and no 24/7 server: every read/write is a short HTTPS call from the serverless function.
//
// Why a separate private repo: conversations contain personal data. The code repository of this project may be
// public, so the store refuses to write to any repository that is not private (override only with
// MEMORY_ALLOW_PUBLIC=true, which you should not do).

"use strict";

const { emptyMemory, normalize } = require("./memory");

class MemoryDisabled extends Error {
  constructor(reason) {
    super(reason);
    this.reason = reason;
  }
}

function createStore(env, fetchImpl) {
  const repo = env.MEMORY_REPO;
  const token = env.GH_MEMORY_TOKEN || env.GH_DISPATCH_TOKEN;
  const path = env.MEMORY_PATH || "memory.json";
  const branch = env.MEMORY_BRANCH || "main";
  if (!repo) return { enabled: false, reason: "MEMORY_REPO не задан в настройках Vercel" };
  if (!token) return { enabled: false, reason: "не задан GH_MEMORY_TOKEN (или GH_DISPATCH_TOKEN)" };

  const doFetch = fetchImpl || ((...a) => globalThis.fetch(...a));
  const api = `https://api.github.com/repos/${repo}`;
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "r-design-space-bot",
    "content-type": "application/json",
  };
  let verified = false;

  async function ensurePrivate() {
    if (verified) return;
    const res = await doFetch(api, { headers });
    if (!res.ok) throw new MemoryDisabled(`репозиторий памяти недоступен (HTTP ${res.status}): проверьте MEMORY_REPO и права токена`);
    const info = await res.json();
    if (!info.private && env.MEMORY_ALLOW_PUBLIC !== "true") {
      throw new MemoryDisabled("репозиторий памяти ПУБЛИЧНЫЙ - личные данные в нём были бы видны всем; сделайте его приватным");
    }
    verified = true;
  }

  async function load() {
    await ensurePrivate();
    const res = await doFetch(`${api}/contents/${encodeURIComponent(path)}?ref=${encodeURIComponent(branch)}`, { headers });
    if (res.status === 404) return { memory: emptyMemory(), sha: null };
    if (!res.ok) throw new Error(`memory load failed: HTTP ${res.status}`);
    const file = await res.json();
    const text = Buffer.from(file.content || "", "base64").toString("utf8");
    return { memory: normalize(text.trim() ? JSON.parse(text) : null), sha: file.sha };
  }

  async function save(memory, sha, message) {
    const body = { message, branch, content: Buffer.from(JSON.stringify(memory, null, 2) + "\n", "utf8").toString("base64") };
    if (sha) body.sha = sha;
    const res = await doFetch(`${api}/contents/${encodeURIComponent(path)}`, { method: "PUT", headers, body: JSON.stringify(body) });
    if (res.status === 200 || res.status === 201) return { ok: true };
    return { ok: false, conflict: res.status === 409 || res.status === 422, status: res.status };
  }

  // Read-modify-write with optimistic locking: if another request saved first, reload and re-apply the mutator.
  async function update(mutator, message = "memory: update [skip ci]") {
    for (let attempt = 0; attempt < 3; attempt++) {
      const { memory, sha } = await load();
      const result = await mutator(memory);
      const saved = await save(memory, sha, message);
      if (saved.ok) return { memory, result };
      if (!saved.conflict) throw new Error(`memory save failed: HTTP ${saved.status}`);
    }
    throw new Error("memory save conflict (3 attempts)");
  }

  return { enabled: true, read: async () => (await load()).memory, update };
}

module.exports = { createStore, MemoryDisabled };
