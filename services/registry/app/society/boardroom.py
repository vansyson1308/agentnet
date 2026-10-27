"""The Telegram boardroom, step 1: a read-only daily digest for the owner (deterministic, no model).

One plain-text page: the daily report's numbers, the latest plan and how to approve it, the READY
candidates with their PR links (or why there is none) and the owner actions waiting. It is sent to
ONE operator chat (``SOCIETY_TELEGRAM_CHAT_ID``) once per UTC day after
``SOCIETY_TELEGRAM_DIGEST_HOUR_UTC`` when ``SOCIETY_TELEGRAM_ENABLED`` (default off).

Structural text only: numbers, ids, statuses, our own labels and system URLs. A title a model wrote
(a candidate, an agent-proposed owner action, a ticket) passes through the same redaction as an
intent row (secret_redaction.py), loses control characters and is cut to ``TITLE_MAX`` -- nothing
else a model wrote (rationales, payloads, minutes) is ever forwarded. The digest reads; it decides
nothing: the plan and the owner actions are still decided only through the operator API."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import daily_report, goals, promotion_queue
from .config import SocietySettings
from .events import emit_event, utcnow
from .secret_redaction import redact_text

logger = logging.getLogger("agentnet.society.boardroom")

TITLE_MAX = 90
#: Telegram's message limit is 4096 characters
DIGEST_MAX = 3900
SENT_EVENT = "boardroom.digest_sent"
FAILED_EVENT = "boardroom.digest_failed"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def clean(value: Any, limit: int = TITLE_MAX) -> str:
    """Text we did not compose, made safe to forward: redacted, one line, bounded."""
    out, _ = redact_text(str(value or ""), "", frozenset())
    out = " ".join(_CONTROL.sub(" ", out).split())
    return out if len(out) <= limit else out[: limit - 1] + "…"


def _plan(db: Session) -> List[str]:
    row = db.execute(text("SELECT id, plan_date, status, ranking FROM society_daily_plans ORDER BY created_at DESC LIMIT 1")).mappings().first()
    if row is None:
        return ["Plan: none yet"]
    ranking = row["ranking"] or []
    lines = [f"Plan {row['plan_date']}: {row['status']}, {len(ranking)} ticket(s)"]
    lines += [f"  {i}. [{t.get('objective_id')}/{t.get('department')}] {clean(t.get('title'))} ({t.get('risk')})" for i, t in enumerate(ranking[:5], 1)]
    if row["status"] == "awaiting_owner":
        lines.append(f"  approve or reject: POST /v1/society/company/plans/{row['id']}/approve|reject (operator)")
    return lines


def _ready(db: Session, settings: SocietySettings, now: datetime) -> List[str]:
    rows = db.execute(text(
        "SELECT c.id, c.title, p.status AS promotion, p.external_pr_url FROM code_candidates c "
        "LEFT JOIN LATERAL (SELECT status::text AS status, external_pr_url FROM code_promotions WHERE candidate_id = c.id ORDER BY created_at DESC LIMIT 1) p ON TRUE "
        "WHERE c.status = 'ready' ORDER BY c.updated_at DESC LIMIT 8")).mappings().all()
    waiting = {b["candidate_id"]: b["reason"] for b in promotion_queue.ready_backlog(db, settings, now)}
    lines = [f"READY: {len(rows)}"]
    for r in rows:
        cid = str(r["id"])
        pr = f"PR {r['external_pr_url']} ({r['promotion']})" if r["external_pr_url"] else (f"promotion {r['promotion']}" if r["promotion"] else "no promotion")
        why = f"; waiting: {waiting[cid]}" if cid in waiting else ""
        lines.append(f"  {cid[:8]} {clean(r['title'])} -- {pr}{why}")
    return lines


def _owner_actions(db: Session) -> List[str]:
    rows = db.execute(text("SELECT id, objective_id, title, source FROM society_owner_actions WHERE status = 'open' ORDER BY created_at LIMIT 8")).mappings().all()
    lines = [f"Owner actions open: {len(rows)}"]
    lines += [f"  [{r['objective_id']}] {clean(r['title'])} ({r['source']}, {str(r['id'])[:8]})" for r in rows]
    if rows:
        lines.append("  close: POST /v1/society/company/owner-actions/<id>/done|dismissed (operator)")
    return lines


def digest(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> str:
    now = now or utcnow()
    d = daily_report.build(db, now)["data"]
    roles = ", ".join(f"{r} ${c:.2f}" for r, c in d["spend_usd_by_role"].items()) or "none"
    reviews = ", ".join(f"{k} {v}" for k, v in sorted(d["design_reviews"].items())) or "none"
    lines = [f"AgentNet boardroom {now.date().isoformat()} (UTC)",
             f"Last 24h: {d['cycles']} cycle(s); model spend ${d['spend_usd_total']:.2f} ({roles})",
             f"Builds: {d['builds']['delivered']} delivered / {d['builds']['attempted']} attempted",
             f"Design reviews: {reviews}"]
    card = goals.scorecard(db, now)
    if card.get("north_star"):
        lines += goals.summary(card).splitlines()
    lines += _plan(db) + _ready(db, settings, now) + _owner_actions(db)
    out = "\n".join(lines)
    return out if len(out) <= DIGEST_MAX else out[: DIGEST_MAX - 12] + "\n[truncated]"


def _recorded(db: Session, key: str) -> bool:
    return db.execute(text("SELECT 1 FROM society_events WHERE idempotency_key = :k"), {"k": key}).first() is not None


def maybe_send_digest(db: Session, settings: SocietySettings, send: Callable[[str], Dict[str, Any]], now: Optional[datetime] = None, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """Send today's digest once (after the digest hour), or now when ``force`` (the operator's
    send-now). ``send`` returns ``{"ok": bool, "status": ...}`` and never raises. A failure is
    recorded once per hour and retried on a later cycle. Commits when it sent or failed."""
    now = now or utcnow()
    if not force and (not settings.telegram_enabled or now.hour < settings.telegram_digest_hour_utc or _recorded(db, f"{SENT_EVENT}:{now.date().isoformat()}")
                      or _recorded(db, f"{FAILED_EVENT}:{now.strftime('%Y-%m-%dT%H')}:scheduled")):
        return None
    body = digest(db, settings, now)
    result = send(body)
    ok = bool(result.get("ok"))
    key = f"{SENT_EVENT}:{now.date().isoformat()}" if ok and not force else f"{SENT_EVENT if ok else FAILED_EVENT}:{now.strftime('%Y-%m-%dT%H')}:{'forced' if force else 'scheduled'}"
    emit_event(db, event_type=SENT_EVENT if ok else FAILED_EVENT, payload={"chars": len(body), "status": str(result.get("status"))[:80], "forced": force},
               actor_type="system", idempotency_key=key, notify=False)
    db.commit()
    (logger.info if ok else logger.warning)("boardroom digest %s (%d chars, status %s)", "sent" if ok else "NOT sent", len(body), result.get("status"))
    return {"ok": ok, "chars": len(body), "status": result.get("status")}


__all__ = ["clean", "digest", "maybe_send_digest"]
