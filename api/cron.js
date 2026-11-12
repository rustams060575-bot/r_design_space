// External trigger for the lead search: an exact-time alternative to GitHub's own (delayed) schedule.
//
//   GET https://<project>.vercel.app/api/cron?key=<CRON_KEY>
//
// Point a scheduler such as cron-job.org at that URL, set its timezone to Europe/Madrid (Alicante) and run it at
// 09:00 and 21:00. The scheduler follows daylight saving itself, so the hours stay exact all year.
// The endpoint only starts the existing GitHub Actions workflow (outreach.yml, action=search); it does nothing else.
//
// CRON_KEY is derived from TELEGRAM_BOT_TOKEN (HMAC, like the webhook secret), so there is no extra variable to keep
// in sync. Print it with: python agent/cron_key.py

"use strict";

const crypto = require("crypto");
const env = require("./_lib/env");

function cronKey(token) {
  return token ? crypto.createHmac("sha256", token).update("cron-trigger").digest("base64url").slice(0, 32) : "";
}

const same = (a, b) => a.length === b.length && crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));

module.exports = async function handler(req, res) {
  const expected = cronKey(env.get("TELEGRAM_BOT_TOKEN"));
  const given = String((req.query && req.query.key) || "");
  if (!expected || !same(expected, given)) return res.status(401).end();

  // ?action=autopilot publishes the next ar02 queue item (09:00 / 21:00 America/New_York); ?action=strategy|content starts the weekly james jobs; anything else (or nothing) is the normal lead search.
  const action = ["strategy", "content", "publish", "autopilot"].includes(String((req.query && req.query.action) || "")) ? req.query.action : "search";
  const repo = process.env.GITHUB_REPO || "rustams060575-bot/r_design_space";
  const workflow = process.env.GITHUB_WORKFLOW || "outreach.yml";
  const headers = {
    Authorization: `Bearer ${env.get("GH_DISPATCH_TOKEN")}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "ar-design-cron",
  };
  // The ping is the reliable backup for GitHub's best-effort schedule, so a transient GitHub API error is retried
  // (3 attempts, 2 s apart) before answering 502; a 502 makes cron-job.org flag the run and retry/alert on its side.
  const body = JSON.stringify({ ref: process.env.GITHUB_REF || "main", inputs: { action, query: "", lead_id: "", scheduled: action === "search" ? "true" : "" } });
  let last = 0;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const out = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`, {
        method: "POST",
        headers: { ...headers, "content-type": "application/json" },
        body,
        signal: AbortSignal.timeout(5000),
      });
      last = out.status;
      if (out.status === 204) return res.status(200).json({ ok: true, started: action, attempt });
      console.error(`cron dispatch attempt ${attempt} -> ${out.status}`);
      if (out.status === 401 || out.status === 403 || out.status === 404 || out.status === 422) break; // retrying cannot fix these
    } catch (err) {
      last = 0;
      console.error(`cron dispatch attempt ${attempt} error:`, err && err.message);
    }
    if (attempt < 3) await new Promise((r) => setTimeout(r, 2000));
  }
  return res.status(502).json({ ok: false, github: last });
};

module.exports.cronKey = cronKey;
