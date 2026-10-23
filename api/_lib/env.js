// Environment variables pasted into dashboards often carry invisible junk (spaces, a trailing newline, quotes,
// zero-width characters copied from chats or browsers). None of these characters is ever valid in a token, secret or
// chat id, so they are stripped before use.

"use strict";

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

module.exports = { clean, get, storeEnv };
