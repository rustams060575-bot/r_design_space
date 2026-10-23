"""Thin LLM client shared by the qualifier and the pitch writer (Anthropic preferred, OpenAI as fallback)."""
from __future__ import annotations

import os

import requests


def _anthropic(system: str, prompt: str, cfg: dict, max_tokens: int, timeout: int) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": cfg["llm"]["anthropic_model"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")


def _openai(system: str, prompt: str, cfg: dict, max_tokens: int, timeout: int) -> str:
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={
            "model": cfg["llm"]["openai_model"],
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def available(cfg: dict) -> bool:
    provider = cfg["llm"].get("provider", "auto")
    return bool((provider in ("auto", "anthropic") and os.getenv("ANTHROPIC_API_KEY"))
                or (provider in ("auto", "openai") and os.getenv("OPENAI_API_KEY")))


def complete(system: str, prompt: str, cfg: dict, max_tokens: int | None = None) -> str | None:
    """Returns the model text, or None when no key is configured or every provider failed."""
    provider = cfg["llm"].get("provider", "auto")
    calls = []
    if provider in ("auto", "anthropic") and os.getenv("ANTHROPIC_API_KEY"):
        calls.append(_anthropic)
    if provider in ("auto", "openai") and os.getenv("OPENAI_API_KEY"):
        calls.append(_openai)

    for call in calls:
        try:
            return call(system, prompt, cfg, max_tokens or cfg["llm"]["max_tokens"],
                        cfg["limits"]["request_timeout"] * 4)
        except (requests.RequestException, KeyError, ValueError) as exc:
            print(f"[llm] {call.__name__} failed: {type(exc).__name__}")  # never print URLs/keys
    return None
