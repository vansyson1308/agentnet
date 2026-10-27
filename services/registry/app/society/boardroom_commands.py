"""The Telegram boardroom, step 2: the owner's commands from the ONE operator chat (deterministic, no model).

Owner 2026-10-27 (G2): owner commands (approve the plan, close an owner action) only through the
existing operator API. So a command adds no authority and no new decision path:

- WHO: a Telegram account speaks for an operator only after that operator LINKED it -- the operator
  API (``POST /company/boardroom/link``, ``require_operator``) hands out a one-time code (stored as
  its sha256, ``LINK_CODE_TTL``) and the account claims it with ``/link <code>`` in the operator
  chat. On every command ``operator_auth.operator_for_telegram`` re-checks the durable operator role.
- WHERE: only messages in ``SOCIETY_TELEGRAM_CHAT_ID``; other chats are dropped unanswered. A bot,
  a channel/anonymous admin (``sender_chat``) or a forwarded message never commands.
- WHAT: the very functions the operator routes call -- ``tickets.decide_plan`` (``/approve``,
  ``/reject``) and ``owner_actions.decide`` (``/done``, ``/dismiss``) -- with the operator's user id,
  on an id the owner typed (at least 8 hex characters, one match). ``/plan`` and ``/digest`` read.
- WHEN: each Telegram update is handled once (``boardroom.command:<update_id>``); a command older
  than ``COMMAND_MAX_AGE`` is refused (send it again), so a backlog never fires after an outage.

Replies and the recorded event are structural (command, outcome, id prefixes); the message text is
never stored or forwarded to the Society. Off unless ``SOCIETY_TELEGRAM_COMMANDS_ENABLED``."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import boardroom, owner_actions, tickets
from .config import SocietySettings
from .events import emit_event, utcnow
from .operator_auth import operator_for_telegram

logger = logging.getLogger("agentnet.society.boardroom_commands")

LINK_CODE_TTL = timedelta(minutes=10)
COMMAND_MAX_AGE = timedelta(minutes=15)
#: the worker polls Telegram at most this often
POLL_SECONDS = 10
COMMAND_EVENT = "boardroom.command"
NOTE_MAX = 300
_COMMAND = re.compile(r"^/([a-z]+)(?:@\w+)?(?:\s+(.*))?$", re.S | re.I)
_ID = re.compile(r"^[0-9a-f]{8}(?:[0-9a-f-]{0,28})$")
HELP = ("Commands (operator chat, linked operator only):\n"
        "/plan -- the latest plan\n"
        "/approve <plan id> | /reject <plan id>\n"
        "/done <action id> [note] | /dismiss <action id> [note]\n"
        "/digest -- today's digest now\n"
        "/link <code> -- link this account (code from POST /v1/society/company/boardroom/link)\n"
        "/unlink -- remove this account's link\n"
        "Ids: the first 8+ characters shown in the digest.")


def _sha(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


# ── links (the operator API side) ─────────────────────────────────────


def issue_link_code(db: Session, user_id: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """A fresh one-time code for this operator; it replaces any earlier link or code. Commits.
    The code is returned once and only its sha256 is stored."""
    now = now or utcnow()
    code = secrets.token_urlsafe(9)
    db.execute(text("INSERT INTO society_telegram_links (user_id, telegram_user_id, code_sha256, code_expires_at, linked_at) VALUES (:u, NULL, :h, :e, NULL) "
                    "ON CONFLICT (user_id) DO UPDATE SET telegram_user_id = NULL, code_sha256 = :h, code_expires_at = :e, linked_at = NULL"),
               {"u": user_id, "h": _sha(code), "e": now + LINK_CODE_TTL})
    db.commit()
    return {"code": code, "expires_at": (now + LINK_CODE_TTL).isoformat(), "how": f"send /link {code} in the operator chat within {int(LINK_CODE_TTL.total_seconds() // 60)} minutes"}


def link_status(db: Session, user_id: Any) -> Dict[str, Any]:
    row = db.execute(text("SELECT telegram_user_id, linked_at, code_expires_at FROM society_telegram_links WHERE user_id = :u"), {"u": user_id}).mappings().first()
    if row is None:
        return {"linked": False, "telegram_user_id": None, "linked_at": None, "code_expires_at": None}
    return {"linked": row["linked_at"] is not None, "telegram_user_id": row["telegram_user_id"],
            "linked_at": row["linked_at"].isoformat() if row["linked_at"] else None,
            "code_expires_at": row["code_expires_at"].isoformat() if row["code_expires_at"] else None}


def unlink(db: Session, user_id: Any) -> bool:
    gone = db.execute(text("DELETE FROM society_telegram_links WHERE user_id = :u"), {"u": user_id}).rowcount
    db.commit()
    return bool(gone)


def _claim(db: Session, code: str, telegram_user_id: int, now: datetime) -> str:
    row = db.execute(text("SELECT user_id FROM society_telegram_links WHERE code_sha256 = :h AND code_expires_at > :n FOR UPDATE"),
                     {"h": _sha(code), "n": now}).first()
    if row is None:
        return "refused: unknown or expired code"
    db.execute(text("DELETE FROM society_telegram_links WHERE telegram_user_id = :t AND user_id <> :u"), {"t": telegram_user_id, "u": row[0]})
    db.execute(text("UPDATE society_telegram_links SET telegram_user_id = :t, linked_at = :n, code_sha256 = NULL, code_expires_at = NULL WHERE user_id = :u"),
               {"t": telegram_user_id, "n": now, "u": row[0]})
    return "linked"


# ── commands ──────────────────────────────────────────────────────────


def _one(db: Session, sql: str, prefix: str) -> Optional[uuid.UUID]:
    rows = db.execute(text(sql), {"p": prefix + "%"}).all()
    return rows[0][0] if len(rows) == 1 else None


def _target(arg: str) -> Optional[str]:
    word = (arg or "").split(maxsplit=1)[0].lower() if (arg or "").strip() else ""
    return word if _ID.fullmatch(word) else None


def _plan_command(db: Session, operator: Any, arg: str, approve: bool) -> tuple:
    prefix = _target(arg)
    if prefix is None:
        return "refused: give the plan id (8+ characters)", "bad_id", None
    plan_id = _one(db, "SELECT id FROM society_daily_plans WHERE status = 'awaiting_owner' AND id::text LIKE :p", prefix)
    if plan_id is None:
        return f"refused: no single plan awaiting you starts with {prefix}", "no_match", prefix[:8]
    try:
        plan = tickets.decide_plan(db, plan_id, approve=approve, user_id=operator.id)
    except (KeyError, ValueError) as exc:
        db.rollback()
        return f"refused: {exc}", "refused", prefix[:8]
    return f"plan {str(plan_id)[:8]}: {plan['status']}", plan["status"], str(plan_id)[:8]


def _action_command(db: Session, operator: Any, arg: str, decision: str) -> tuple:
    prefix = _target(arg)
    if prefix is None:
        return "refused: give the owner action id (8+ characters)", "bad_id", None
    action_id = _one(db, "SELECT id FROM society_owner_actions WHERE status = 'open' AND id::text LIKE :p", prefix)
    if action_id is None:
        return f"refused: no single open owner action starts with {prefix}", "no_match", prefix[:8]
    parts = arg.strip().split(maxsplit=1)
    note = parts[1][:NOTE_MAX] if len(parts) > 1 else None
    try:
        owner_actions.decide(db, action_id, decision, user_id=operator.id, note=note)
    except (KeyError, ValueError) as exc:
        db.rollback()
        return f"refused: {exc}", "refused", prefix[:8]
    db.commit()
    return f"owner action {str(action_id)[:8]}: {decision}", decision, str(action_id)[:8]


def handle(db: Session, settings: SocietySettings, update: Dict[str, Any], now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """One Telegram update -> ``{"reply", "command", "outcome", "target", "user_id"}``, or None when
    it is not ours to answer (another chat, not a command)."""
    now = now or utcnow()
    if str(update.get("chat_id")) != settings.telegram_chat_id or not settings.telegram_chat_id:
        return None
    m = _COMMAND.match((update.get("text") or "").strip())
    if m is None:
        return None
    command, arg = m.group(1).lower(), (m.group(2) or "").strip()
    out = {"command": command, "target": None, "user_id": None}
    if update.get("from_is_bot") or update.get("sender_chat") or update.get("forwarded") or not isinstance(update.get("from_id"), int):
        return {**out, "reply": "refused: commands come from a person's own account, not a bot, a channel or a forward", "outcome": "not_a_person"}
    sent = datetime.fromtimestamp(update["date"], tz=timezone.utc) if isinstance(update.get("date"), int) else None
    if sent is None or now - sent > COMMAND_MAX_AGE:
        return {**out, "reply": f"refused: older than {int(COMMAND_MAX_AGE.total_seconds() // 60)} minutes, send it again", "outcome": "stale"}
    if command == "help" or command == "start":
        return {**out, "reply": HELP, "outcome": "help"}
    if command == "link":
        outcome = _claim(db, arg, update["from_id"], now) if arg else "refused: /link <code>"
        db.commit()
        return {**out, "reply": outcome, "outcome": "linked" if outcome == "linked" else "link_refused"}
    operator = operator_for_telegram(db, update["from_id"])
    if operator is None:
        return {**out, "reply": "refused: this account is not linked to an operator (see /help)", "outcome": "not_operator"}
    out["user_id"] = str(operator.id)
    if command == "unlink":
        unlink(db, operator.id)
        return {**out, "reply": "unlinked", "outcome": "unlinked"}
    if command == "plan":
        return {**out, "reply": "\n".join(boardroom._plan(db, commands=True)), "outcome": "read"}
    if command == "digest":
        return {**out, "reply": boardroom.digest(db, settings, now), "outcome": "read"}
    if command in ("approve", "reject"):
        reply, outcome, target = _plan_command(db, operator, arg, command == "approve")
    elif command in ("done", "dismiss"):
        reply, outcome, target = _action_command(db, operator, arg, "done" if command == "done" else "dismissed")
    else:
        return {**out, "reply": "unknown command (see /help)", "outcome": "unknown"}
    return {**out, "reply": reply, "outcome": outcome, "target": target}


def _recorded(db: Session, key: str) -> bool:
    return db.execute(text("SELECT 1 FROM society_events WHERE idempotency_key = :k"), {"k": key}).first() is not None


def poll(db: Session, settings: SocietySettings, fetch: Callable[[Optional[int]], Dict[str, Any]], send: Callable[[str], Dict[str, Any]],
         offset: Optional[int] = None, now: Optional[datetime] = None) -> Optional[int]:
    """Fetch new updates after ``offset``, handle each once, reply in the chat. Returns the next
    offset (unchanged when nothing came or the fetch failed). Commits per update."""
    if not (settings.telegram_enabled and settings.telegram_commands_enabled):
        return offset
    got = fetch(offset)
    if not got.get("ok"):
        logger.warning("boardroom commands: getUpdates failed (status %s)", got.get("status"))
        return offset
    for update in got.get("updates") or []:
        offset = max(offset or 0, int(update["update_id"]) + 1)
        key = f"{COMMAND_EVENT}:{update['update_id']}"
        if _recorded(db, key):
            continue
        try:
            res = handle(db, settings, update, now)
        except Exception:  # noqa: BLE001 -- one bad update never blocks the rest
            db.rollback()
            logger.exception("boardroom command failed (update %s)", update["update_id"])
            res = {"reply": "failed: see the worker log", "command": "?", "outcome": "error", "target": None, "user_id": None}
        if res is None:
            continue
        emit_event(db, event_type=COMMAND_EVENT, payload={k: res[k] for k in ("command", "outcome", "target", "user_id")}, actor_type="system",
                   idempotency_key=key, notify=False)
        db.commit()
        logger.info("boardroom command /%s: %s%s", res["command"], res["outcome"], f" ({res['target']})" if res["target"] else "")
        send(res["reply"][: boardroom.DIGEST_MAX])
    return offset


__all__ = ["COMMAND_EVENT", "handle", "issue_link_code", "link_status", "poll", "unlink"]
