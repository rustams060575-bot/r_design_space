"""Generates a short, personalized English cold email with Claude (preferred) or OpenAI."""
from __future__ import annotations

import os

import requests

SYSTEM_PROMPT = (
    "You write concise, respectful cold emails for Rustam, a freelance UI/UX designer and interactive web "
    "developer. His strengths: SaaS/CRM dashboards and data-heavy interfaces, design systems, "
    "custom frontend (HTML/CSS/JS, GSAP), and Tilda Zero Block sites. "
    "Rules: 90-130 words; plain text; no hype, no emojis; one specific, honest observation based ONLY on the "
    "product description given; never invent metrics, clients, or claims that Rustam used the product; "
    "one clear low-pressure call to action (a short reply or a quick chat); do not add a signature or "
    "unsubscribe line (they are added automatically). "
    "Output format exactly:\nSUBJECT: <subject under 70 chars>\n\n<email body>"
)


def _prompt(lead: dict, cfg: dict) -> str:
    portfolio = cfg.get("vercel_url") or "(no portfolio link yet - do not mention one)"
    return (
        f"Product: {lead['name']}\n"
        f"Description: {lead.get('summary') or 'n/a'}\n"
        f"Website: {lead.get('website') or 'n/a'}\n"
        f"Matched topics: {', '.join(lead.get('keywords', []))}\n"
        f"Portfolio: {portfolio}\n"
        f"Reply-to Telegram: {cfg['owner']['telegram_handle']}\n\n"
        "Write the email."
    )


def _call_anthropic(prompt: str, cfg: dict, timeout: int) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": cfg["llm"]["anthropic_model"],
            "max_tokens": cfg["llm"]["max_tokens"],
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")


def _call_openai(prompt: str, cfg: dict, timeout: int) -> str:
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={
            "model": cfg["llm"]["openai_model"],
            "max_tokens": cfg["llm"]["max_tokens"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def parse_pitch(text: str) -> dict:
    text = text.strip()
    subject, body = "", text
    if text.upper().startswith("SUBJECT:"):
        first, _, rest = text.partition("\n")
        subject, body = first.split(":", 1)[1].strip(), rest.strip()
    if not subject or not body:
        raise ValueError("model output did not follow the SUBJECT/body format")
    return {"subject": subject[:90], "body": body}


def fallback_pitch(lead: dict, cfg: dict) -> dict:
    """Used when no API key is configured or the model call fails."""
    owner = cfg["owner"]
    topic = lead["keywords"][0] if lead.get("keywords") else "product"
    link = f"\nPortfolio: {cfg['vercel_url']}\n" if cfg.get("vercel_url") else "\n"
    body = (
        f"Hi {lead['name']} team,\n\n"
        f"I came across {lead['name']} and noticed it sits in the {topic} space. "
        "I'm Rustam, a UI/UX designer and interactive web developer focused on SaaS and CRM dashboards: "
        "dense data tables, analytics views and design systems that stay fast and readable as a product grows.\n\n"
        "If you are planning interface improvements or a new landing page (including Tilda Zero Block), "
        "I'd be glad to share a few relevant examples and a quick idea or two for your product."
        f"{link}\n"
        f"Would a short reply or a quick chat on Telegram ({owner['telegram_handle']}) work for you?\n\n"
        "Best,\nRustam"
    )
    return {"subject": f"UI/UX help for {lead['name']}", "body": body}


def write_pitch(lead: dict, cfg: dict) -> dict:
    timeout = cfg["limits"]["request_timeout"] * 4
    provider = cfg["llm"].get("provider", "auto")
    prompt = _prompt(lead, cfg)

    attempts = []
    if provider in ("auto", "anthropic") and os.getenv("ANTHROPIC_API_KEY"):
        attempts.append(_call_anthropic)
    if provider in ("auto", "openai") and os.getenv("OPENAI_API_KEY"):
        attempts.append(_call_openai)

    for call in attempts:
        try:
            return parse_pitch(call(prompt, cfg, timeout))
        except (requests.RequestException, KeyError, ValueError) as exc:
            print(f"[pitch] {call.__name__} failed: {type(exc).__name__}")
    return fallback_pitch(lead, cfg)
