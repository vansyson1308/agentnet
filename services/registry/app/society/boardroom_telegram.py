"""The ONE place the Telegram bot token is read (``SOCIETY_TELEGRAM_BOT_TOKEN``).

``send(settings, text)`` posts plain text to the configured operator chat through the Bot API's
``sendMessage`` (fixed host, no link previews, no parse mode) and returns ``{"ok", "status"}``
-- it never raises and never returns, logs or stores the token or the response body."""

from __future__ import annotations

import os
from typing import Any, Dict

import httpx

from .config import SocietySettings

TOKEN_ENV = "SOCIETY_TELEGRAM_BOT_TOKEN"
API = "https://api.telegram.org"
TIMEOUT_SECONDS = 10.0


def send(settings: SocietySettings, text: str) -> Dict[str, Any]:
    token = os.getenv(TOKEN_ENV, "").strip()
    if not token:
        return {"ok": False, "status": "no bot token configured"}
    if not settings.telegram_chat_id:
        return {"ok": False, "status": "no operator chat configured"}
    try:
        resp = httpx.post(f"{API}/bot{token}/sendMessage", json={"chat_id": settings.telegram_chat_id, "text": text, "disable_web_page_preview": True},
                          timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return {"ok": False, "status": f"transport error: {type(exc).__name__}"}
    return {"ok": resp.status_code == 200, "status": resp.status_code}


def sender(settings: SocietySettings):
    return lambda body: send(settings, body)
