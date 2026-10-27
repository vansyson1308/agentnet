"""The ONE place the Telegram bot token is read (``SOCIETY_TELEGRAM_BOT_TOKEN``).

``send(settings, text)`` posts plain text to the configured operator chat through the Bot API's
``sendMessage`` (fixed host, no link previews, no parse mode) and returns ``{"ok", "status"}``
-- it never raises and never returns, logs or stores the token or the response body.

``get_updates(settings, offset)`` (step 2, boardroom_commands.py) reads new messages with
``getUpdates`` (short poll, messages only) and returns only the fields a command needs.

The Bot API puts the token in the URL path, and httpx logs every request URL at INFO: a filter on
the ``httpx``/``httpcore`` loggers drops any record that names this API's ``/bot`` path, whatever
the process's log level."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx

from .config import SocietySettings

TOKEN_ENV = "SOCIETY_TELEGRAM_BOT_TOKEN"
API = "https://api.telegram.org"
TIMEOUT_SECONDS = 10.0
UPDATES_LIMIT = 20


class _NoTokenUrls(logging.Filter):
    """Drop a client log record that carries a Bot API URL (``/bot<token>/``)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return f"{API.split('//', 1)[1]}/bot" not in record.getMessage()
        except Exception:  # noqa: BLE001 -- a record we cannot render is not passed on either
            return False


for _name in ("httpx", "httpcore"):
    if not any(isinstance(f, _NoTokenUrls) for f in logging.getLogger(_name).filters):
        logging.getLogger(_name).addFilter(_NoTokenUrls())


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


def get_updates(settings: SocietySettings, offset: Optional[int]) -> Dict[str, Any]:
    """New messages after ``offset`` (which confirms the earlier ones to Telegram), as
    ``{"ok", "status", "updates": [{update_id, chat_id, from_id, from_is_bot, sender_chat,
    forwarded, date, text}]}``. Never raises; the token and the raw body are never returned."""
    token = os.getenv(TOKEN_ENV, "").strip()
    if not token:
        return {"ok": False, "status": "no bot token configured", "updates": []}
    body: Dict[str, Any] = {"timeout": 0, "limit": UPDATES_LIMIT, "allowed_updates": ["message"]}
    if offset is not None:
        body["offset"] = offset
    try:
        resp = httpx.post(f"{API}/bot{token}/getUpdates", json=body, timeout=TIMEOUT_SECONDS)
        data = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "status": f"transport error: {type(exc).__name__}", "updates": []}
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("result"), list):
        return {"ok": False, "status": resp.status_code, "updates": []}
    return {"ok": True, "status": resp.status_code, "updates": [u for u in (_update(x) for x in data["result"]) if u is not None]}


def _update(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("update_id"), int):
        return None
    msg = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
    sender = msg.get("from") if isinstance(msg.get("from"), dict) else {}
    text = msg.get("text")
    return {"update_id": raw["update_id"], "chat_id": chat.get("id"), "from_id": sender.get("id"), "from_is_bot": bool(sender.get("is_bot")),
            "sender_chat": "sender_chat" in msg, "forwarded": any(k in msg for k in ("forward_origin", "forward_from", "forward_from_chat", "forward_date")),
            "date": msg.get("date"), "text": text if isinstance(text, str) else None}


def sender(settings: SocietySettings):
    return lambda body: send(settings, body)


def fetcher(settings: SocietySettings):
    return lambda offset: get_updates(settings, offset)
