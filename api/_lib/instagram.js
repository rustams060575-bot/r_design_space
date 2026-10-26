// Instagram Graph API: comment webhook -> private reply (Direct message) with the link to the Telegram funnel.
//
// A viewer comments "Motion" under a Reel -> Meta POSTs a `comments` change to /api/instagram -> we answer that comment
// with a "private reply" (POST /{ig-user-id}/messages with recipient.comment_id), which opens the Direct conversation.
// Meta allows ONE private reply per comment, within 7 days of the comment; the text may contain a link.
//
// Environment variables (Vercel -> Settings -> Environment Variables):
//   IG_VERIFY_TOKEN    any string you choose; the same value goes into the webhook settings of the Meta app
//   IG_APP_SECRET      App Secret of the Meta app: used to verify X-Hub-Signature-256 (requests without it are rejected)
//   IG_ACCESS_TOKEN    token with instagram_manage_messages (+ instagram_manage_comments) for the Instagram professional account
//   IG_USER_ID         id of that Instagram professional account (the `entry.id` of the webhook)
//   optional: IG_API_VERSION (default v21.0), IG_GRAPH_HOST (default graph.facebook.com; graph.instagram.com for Instagram Login tokens),
//             IG_FUNNEL_LINK, IG_DM_TEXT

"use strict";

const crypto = require("crypto");

const FUNNEL_LINK = "https://t.me/r_design_outreach_bot?start=motion";
const DM_TEXT = (link) =>
  "¡Hola! Soy James, del equipo de R Design Space. Aquí tienes la guía Motion, paso a paso: wireframe, estética, código y animación.\n\n" +
  `${link}\n\n` +
  "Pulsa «Start» en Telegram y te la envío al momento.";

// "Motion" in any case, as a word of its own: "MOTION", "motion!", "Motion 🔥" match; "emotion" does not.
const TRIGGER = /(^|[^\p{L}\p{N}_])motion($|[^\p{L}\p{N}_])/iu;
const isTrigger = (text) => TRIGGER.test(String(text || ""));

// Meta signs the RAW body: X-Hub-Signature-256: sha256=<hex hmac of the body with the app secret>.
function validSignature(rawBody, header, appSecret) {
  if (!appSecret || !header) return false;
  const expected = "sha256=" + crypto.createHmac("sha256", appSecret).update(rawBody).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(String(header));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// Flattens the webhook payload into comments: [{ igId, commentId, parentId, text, fromId, fromUsername, mediaId }].
function extractComments(payload) {
  const out = [];
  if (!payload || payload.object !== "instagram" || !Array.isArray(payload.entry)) return out;
  for (const entry of payload.entry) {
    for (const change of entry.changes || []) {
      if (change.field !== "comments" || !change.value) continue;
      const v = change.value;
      out.push({
        igId: String(entry.id || ""),
        commentId: String(v.id || ""),
        parentId: v.parent_id ? String(v.parent_id) : "",
        text: String(v.text || ""),
        fromId: String((v.from && v.from.id) || ""),
        fromUsername: String((v.from && v.from.username) || ""),
        mediaId: String((v.media && v.media.id) || ""),
      });
    }
  }
  return out;
}

// POST /{ig-user-id}/messages { recipient: { comment_id }, message: { text } }. Returns { ok, status, error }.
async function sendPrivateReply(commentId, text, cfg, doFetch = (...a) => globalThis.fetch(...a)) {
  const url = `https://${cfg.host}/${cfg.version}/${cfg.igUserId || "me"}/messages`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const res = await doFetch(url, {
      method: "POST",
      headers: { Authorization: `Bearer ${cfg.token}`, "content-type": "application/json" },
      body: JSON.stringify({ recipient: { comment_id: commentId }, message: { text } }),
      signal: ctrl.signal,
    });
    if (res.ok) return { ok: true, status: res.status };
    let error = "";
    try {
      const j = await res.json();
      error = j && j.error ? `${j.error.code || ""}/${j.error.error_subcode || ""} ${j.error.message || ""}`.trim() : "";
    } catch (_) {
      /* no JSON body */
    }
    return { ok: false, status: res.status, error }; // the response never contains our token
  } catch (err) {
    return { ok: false, status: 0, error: err && err.name === "AbortError" ? "timeout after 8s" : String((err && err.message) || err) };
  } finally {
    clearTimeout(timer);
  }
}

module.exports = { FUNNEL_LINK, DM_TEXT, isTrigger, validSignature, extractComments, sendPrivateReply };
