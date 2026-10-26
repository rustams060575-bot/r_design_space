// Instagram webhook on Vercel: comments under Reels -> "Motion" -> private reply in Direct with the Telegram funnel link.
// Logic and the list of environment variables: api/_lib/instagram.js.
//
//  GET  /api/instagram   webhook verification handshake (hub.mode / hub.verify_token / hub.challenge)
//  POST /api/instagram   events; the signature (X-Hub-Signature-256) is checked against IG_APP_SECRET

"use strict";

const env = require("./_lib/env");
const ig = require("./_lib/instagram");

// We need the raw bytes for the signature, so the body must not be parsed before we read it.
module.exports.config = { api: { bodyParser: false } };

async function readRaw(req) {
  if (typeof req.body === "string") return req.body;
  if (Buffer.isBuffer(req.body)) return req.body.toString("utf8");
  const chunks = [];
  for await (const chunk of req) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks).toString("utf8");
}

// Meta re-delivers events that were not answered quickly; a warm instance remembers the comments it has already handled.
// (Meta itself accepts only one private reply per comment, so a duplicate on a cold instance is rejected by the API.)
const handled = new Set();
const once = (id) => {
  if (handled.has(id)) return false;
  handled.add(id);
  if (handled.size > 500) handled.delete(handled.values().next().value);
  return true;
};

// Log lines hold ids, the commenter's public username and a short clip of the comment: no tokens, no secrets.
const log = (event, data) => console.log(JSON.stringify({ ig: event, ...data }));

module.exports = async (req, res) => {
  if (req.method === "GET") {
    const q = req.query || {};
    const token = env.get("IG_VERIFY_TOKEN");
    if (q["hub.mode"] === "subscribe" && token && String(q["hub.verify_token"] || "") === token) {
      log("verify", { ok: true });
      return res.status(200).send(String(q["hub.challenge"] || ""));
    }
    log("verify", { ok: false, reason: token ? "token-mismatch" : "IG_VERIFY_TOKEN-not-set" });
    return res.status(403).end();
  }
  if (req.method !== "POST") return res.status(405).end();

  let raw = "";
  try {
    raw = await readRaw(req);
  } catch (err) {
    log("read-error", { error: String(err && err.message) });
    return res.status(400).end();
  }
  if (!ig.validSignature(raw, req.headers["x-hub-signature-256"], env.get("IG_APP_SECRET"))) {
    log("rejected", { reason: env.get("IG_APP_SECRET") ? "bad-signature" : "IG_APP_SECRET-not-set" });
    return res.status(401).end();
  }

  try {
    const payload = JSON.parse(raw);
    const comments = ig.extractComments(payload);
    log("webhook", { object: payload.object, entries: (payload.entry || []).length, comments: comments.length });

    const cfg = {
      token: env.get("IG_ACCESS_TOKEN"),
      igUserId: env.get("IG_USER_ID"),
      version: env.get("IG_API_VERSION") || "v21.0",
      host: env.get("IG_GRAPH_HOST") || "graph.facebook.com",
    };
    const link = env.get("IG_FUNNEL_LINK") || ig.FUNNEL_LINK;
    const text = process.env.IG_DM_TEXT ? process.env.IG_DM_TEXT.replace("{link}", link) : ig.DM_TEXT(link);

    for (const c of comments) {
      const matched = ig.isTrigger(c.text);
      const own = c.fromId && (c.fromId === c.igId || c.fromId === cfg.igUserId); // our own replies must never trigger a DM
      log("comment", { comment: c.commentId, media: c.mediaId, from: c.fromUsername || c.fromId, text: c.text.slice(0, 120), matched, own });
      if (!matched || own || !c.commentId || !once(c.commentId)) continue;
      if (!cfg.token) {
        log("skip", { comment: c.commentId, reason: "IG_ACCESS_TOKEN-not-set" });
        continue;
      }
      const r = await ig.sendPrivateReply(c.commentId, text, cfg);
      log(r.ok ? "dm-sent" : "dm-failed", { comment: c.commentId, status: r.status, error: r.error || undefined });
    }
  } catch (err) {
    log("error", { error: String(err && err.message) });
  }
  return res.status(200).json({ ok: true }); // always 200 for an authentic request, otherwise Meta keeps retrying
};
