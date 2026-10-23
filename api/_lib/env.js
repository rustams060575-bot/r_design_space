// Environment variables pasted into dashboards often carry invisible junk (spaces, a trailing newline, quotes,
// zero-width characters copied from chats or browsers). None of these characters is ever valid in a token, secret or
// chat id, so they are stripped before use.

"use strict";

const crypto = require("crypto");

const INVISIBLE = /[\s­​-‏‪-‮⁠-⁤﻿]/g;

function clean(value) {
  return String(value == null ? "" : value).replace(INVISIBLE, "").replace(/^["'`]+|["'`]+$/g, "");
}

const get = (name) => clean(process.env[name]);

// Copy of process.env with the variables used by the memory store cleaned.
function storeEnv() {
  const env = { ...process.env };
  for (const k of ["MEMORY_REPO", "GH_MEMORY_TOKEN", "GH_DISPATCH_TOKEN"]) env[k] = clean(env[k]);
  return env;
}

// Webhook secret derived from the bot token: nothing extra to copy between Telegram, Vercel and .env.
// The same formula lives in agent/set_webhook.py. An explicit TELEGRAM_WEBHOOK_SECRET is still accepted.
function derivedWebhookSecret(token) {
  return token ? crypto.createHmac("sha256", token).update("telegram-webhook").digest("base64url").slice(0, 43) : "";
}

module.exports = { clean, get, storeEnv, derivedWebhookSecret };
