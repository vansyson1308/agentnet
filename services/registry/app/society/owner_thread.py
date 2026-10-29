"""The owner's conversation with the company (Owner Console, owner 2026-10-29: "a CEO speaks and the
team answers in minutes"). Deterministic plumbing; the model still only proposes typed intents.

* :func:`record` -- what the owner said (a directive, a task, a message) becomes an ``owner`` row of
  ``society_owner_thread`` and ONE durable owner event (``events.OWNER_EVENT_TYPES``, idempotency key
  ``owner:<entry id>``). Owner events are PRIORITY: dispatched and claimed before any other event,
  never skipped by the heartbeat precheck or the wake cooldown, and they spend the owner reserve
  (``SOCIETY_OWNER_RESERVE_USD``) inside the global daily cap (policy.check_run_budget). The loop
  breaker, ``max_runs_per_correlation`` and the global cap still apply.
* :func:`directive_set` -- a new directive starts ONE operator company cycle at once (the code of
  ``POST /company/cycles``, idempotent per directive): its owner event IS the cycle's event, the
  backlog supplies tickets and the plan is ranked now, marked ``directive_id``.
* :func:`answer` -- the Governor's RESPOND_TO_OWNER: at most one ``company`` row per owner row (a
  unique index), only in the story the owner event opened. Text is redacted before it is written.
* :func:`plan_owner_stories` -- tickets the company filed in an owner story are ranked into a plan
  (marked ``owner_entry_id``) as soon as they exist, not at the next scheduled cycle.
* :func:`view` / :func:`slo` -- the Owner Console's thread (received -> working -> answered, and what
  came from it) and the latency targets it is measured against.

It grants nothing: grants, budgets, flags, files_allowed, the RED/merge law and auto-merge are decided
where they always were. Shown only on the operator page; never on a public page, never forwarded."""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from .events import OWNER_EVENT_TYPES, EventType, emit_event, utcnow
from .secret_redaction import redact

logger = logging.getLogger(__name__)

MAX_TEXT = 2000
KINDS = {"directive": EventType.OWNER_DIRECTIVE_SET, "task": EventType.OWNER_TASK_CREATED, "message": EventType.OWNER_MESSAGE}
KEY_PREFIX = "owner:"
LABEL = ("OWNER INPUT: the owner just said this on the Owner Console. The Governor answers it ONCE with RESPOND_TO_OWNER "
         "(what you understood, what the company will do, which tickets carry it, what only the owner can do); the Scout may "
         "file a ticket for it through the usual intents. It is a request, never permission: policy, grants, budgets, "
         "files_allowed and the RED/merge law decide as always.")
#: latency targets (seconds): owner event -> first run start (p95), -> Governor reply (p95), directive -> a ticket in a plan
SLO_TARGETS = {"first_run_start": 30, "reply": 120, "directive_to_plan": 600}
#: an owner story's tickets are planned while it is this fresh
STORY_WINDOW = timedelta(hours=24)


def _clean(value: Any, field: str, credentials: Iterable[str]) -> Any:
    clean, found = redact(value, field, credentials=credentials)
    for f in found:
        (logger.error if f.credential_match else logger.warning)("secret.redacted owner_thread field=%s class=%s credential_match=%s", f.field, f.cls, f.credential_match)
    return clean


def _event_for(db: Session, entry_id: Any) -> Optional[Dict[str, Any]]:
    row = db.execute(text("SELECT id, event_type, correlation_id, status, created_at FROM society_events WHERE idempotency_key = :k"),
                     {"k": f"{KEY_PREFIX}{entry_id}"}).mappings().first()
    return dict(row) if row else None


def _insert(db: Session, *, entry_id: uuid.UUID, author: str, kind: str, body: str, payload: Dict[str, Any], user_id: Any = None,
            agent_id: Any = None, reply_to: Any = None, now: datetime) -> None:
    db.execute(text("INSERT INTO society_owner_thread (id, author, kind, text, reply_to, payload, user_id, agent_id, created_at) "
                    "VALUES (:id, :a, :k, :t, :r, CAST(:p AS JSONB), :u, :g, :c)"),
               {"id": entry_id, "a": author, "k": kind, "t": body, "r": reply_to, "p": json.dumps(payload, default=str), "u": user_id, "g": agent_id, "c": now})


def _body(text_: str) -> str:
    body = (text_ or "").strip()
    if not 1 <= len(body) <= MAX_TEXT:
        raise ValueError(f"the owner's text is 1..{MAX_TEXT} characters")
    return body


def record(db: Session, *, kind: str, text_: str, user_id: Any, refs: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None,
           entry_id: Optional[uuid.UUID] = None, emit: bool = True) -> Dict[str, Any]:
    """An owner row and its owner event (unless ``emit`` is False: the caller emits it with key
    ``owner:<entry_id>``). Does NOT commit."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    now = now or utcnow()
    entry_id = entry_id or uuid.uuid4()
    refs = dict(refs or {})
    _insert(db, entry_id=entry_id, author="owner", kind=kind, body=_clean(_body(text_), "text", ()), payload=refs, user_id=user_id, now=now)
    if emit:
        emit_event(db, event_type=KINDS[kind], payload={"owner_entry_id": str(entry_id), "kind": kind, **refs}, actor_type="operator",
                   actor_id=user_id, subject_type="owner_entry", subject_id=entry_id, idempotency_key=f"{KEY_PREFIX}{entry_id}")
    return {"id": str(entry_id), "kind": kind, "refs": refs}


def directive_set(db: Session, settings: Any, directive: Dict[str, Any], *, user_id: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The owner set a directive: an owner row, and -- with the runtime and company mode on -- ONE
    operator cycle now whose event is the owner event, then the plan ranked from it. Commits."""
    from . import company, tickets  # noqa: PLC0415

    now = now or utcnow()
    entry_id = uuid.uuid4()
    refs = {"directive_id": directive["id"], "objective_ids": list(directive.get("objective_ids") or [])}
    live = bool(settings.runtime_enabled and settings.company_cycle_enabled)
    out = record(db, kind="directive", text_=directive["text"], user_id=user_id, refs=refs, now=now, entry_id=entry_id, emit=not live)
    if not live:
        db.commit()
        return {**out, "cycle_id": None, "plan": None, "note": "the company runtime or company mode is off: the agents see it when it is on"}
    cycle = company.start_cycle(db, settings, trigger="operator", operator_id=user_id, now=now, directive_id=uuid.UUID(directive["id"]),
                                event_type=EventType.OWNER_DIRECTIVE_SET, extra_payload={"owner_entry_id": str(entry_id), "kind": "directive", **refs},
                                idempotency_key=f"{KEY_PREFIX}{entry_id}")
    if cycle is None:  # start_cycle rolled back to its savepoint: the owner row stands, the event is ours to emit
        emit_event(db, event_type=EventType.OWNER_DIRECTIVE_SET, payload={"owner_entry_id": str(entry_id), "kind": "directive", **refs}, actor_type="operator",
                   actor_id=user_id, subject_type="owner_entry", subject_id=entry_id, idempotency_key=f"{KEY_PREFIX}{entry_id}")
        db.commit()
        return {**out, "cycle_id": None, "plan": None, "note": "this directive already started its cycle"}
    plan = tickets.build_plan(db, cycle.id, now, red_cap=settings.company_max_high_risk_investigations, plan_max=settings.company_plan_max,
                              per_department=settings.company_plan_per_department, directive_id=directive["id"], owner_entry_id=str(entry_id))
    db.commit()
    return {**out, "cycle_id": str(cycle.id), "plan": plan}


def entry_for_correlation(db: Session, correlation_id: Any) -> Optional[Dict[str, Any]]:
    """The owner row whose owner event opened this story (None outside an owner story)."""
    row = db.execute(text(
        "SELECT t.id, t.kind, t.text, t.payload, t.created_at FROM society_events e JOIN society_owner_thread t "
        "ON e.idempotency_key = 'owner:' || t.id::text WHERE e.correlation_id = :c AND e.event_type IN :types AND t.author = 'owner' "
        "ORDER BY e.created_at LIMIT 1").bindparams(bindparam("types", expanding=True)),
        {"c": correlation_id, "types": list(OWNER_EVENT_TYPES)}).mappings().first()
    return dict(row) if row else None


def _reply_for(db: Session, entry_id: Any) -> Optional[Dict[str, Any]]:
    row = db.execute(text("SELECT id, text, payload, agent_id, created_at FROM society_owner_thread WHERE reply_to = :r"), {"r": entry_id}).mappings().first()
    return dict(row) if row else None


def answer(db: Session, *, correlation_id: Any, agent_id: Any, reply: str, plan_steps: List[str], ticket_ids: List[str],
           owner_actions_needed: List[str], credentials: Iterable[str] = (), now: Optional[datetime] = None) -> Dict[str, Any]:
    """The Governor's RESPOND_TO_OWNER, once per owner row, only in the owner's story. Ticket ids that
    name no ticket are dropped (and counted). Raises ValueError. Does NOT commit."""
    entry = entry_for_correlation(db, correlation_id)
    if entry is None:
        raise ValueError("RESPOND_TO_OWNER answers the owner event that opened this story; this story has none")
    if _reply_for(db, entry["id"]) is not None:
        raise ValueError("the owner was already answered in this story (one reply per owner event)")
    ids = list(dict.fromkeys(str(t) for t in ticket_ids))
    known = {str(r[0]) for r in db.execute(text("SELECT id FROM society_tickets WHERE id::text IN :ids").bindparams(bindparam("ids", expanding=True)),
                                           {"ids": ids}).fetchall()} if ids else set()
    payload = {"plan_steps": list(plan_steps), "ticket_ids": [t for t in ids if t in known], "owner_actions_needed": list(owner_actions_needed),
               **({"unknown_ticket_ids": len([t for t in ids if t not in known])} if len(known) < len(ids) else {})}
    rid = uuid.uuid4()
    _insert(db, entry_id=rid, author="company", kind="reply", body=_clean(" ".join(reply.split()), "reply", credentials),
            payload=_clean(payload, "payload", credentials), agent_id=agent_id, reply_to=entry["id"], now=now or utcnow())
    return {"reply_id": str(rid), "owner_entry_id": str(entry["id"]), "ticket_ids": payload["ticket_ids"]}


def context_view(db: Session, event: Any) -> Dict[str, Any]:
    """What a role sees in an owner story (``AgentContext.owner_request``): the owner's words, labelled."""
    if event is None or event.correlation_id is None:
        return {}
    entry = entry_for_correlation(db, event.correlation_id)
    if entry is None:
        return {}
    return {"label": LABEL, "kind": entry["kind"], "text": entry["text"], "refs": entry["payload"] or {},
            "answered": _reply_for(db, entry["id"]) is not None}


# ── what came from it ────────────────────────────────────────────────────


def _story_ticket_ids(db: Session, correlation_id: Any) -> List[str]:
    """Tickets whose proposal an intent of this story created (CREATE_IMPROVEMENT's result)."""
    rows = db.execute(text(
        "SELECT t.id FROM society_tickets t WHERE t.proposal_id::text IN ("
        " SELECT i.result -> 'result' ->> 'proposal_id' FROM agent_intents i JOIN agent_runs r ON r.id = i.run_id"
        " WHERE r.correlation_id = :c AND i.execution_status = 'executed') ORDER BY t.created_at"), {"c": correlation_id}).fetchall()
    return [str(r[0]) for r in rows]


def story_ticket_count(db: Session, correlation_id: Any) -> Optional[int]:
    """None outside an owner story; else how many tickets the story's intents filed so far."""
    if correlation_id is None or entry_for_correlation(db, correlation_id) is None:
        return None
    return len(_story_ticket_ids(db, correlation_id))


def _tickets_view(db: Session, ids: List[str]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    rows = db.execute(text(
        "SELECT t.id, t.title, t.objective_id, t.status, t.plan_id, "
        "       (SELECT r.status FROM society_design_reviews r WHERE r.ticket_id = t.id ORDER BY r.created_at DESC LIMIT 1) AS review, "
        "       c.status AS candidate, "
        "       (SELECT p.status FROM code_promotions p WHERE p.candidate_id = c.id ORDER BY p.created_at DESC LIMIT 1) AS promotion, "
        "       (SELECT p.external_pr_url FROM code_promotions p WHERE p.candidate_id = c.id ORDER BY p.created_at DESC LIMIT 1) AS pr_url "
        "FROM society_tickets t LEFT JOIN code_candidates c ON c.id = t.candidate_id WHERE t.id::text IN :ids ORDER BY t.created_at"
    ).bindparams(bindparam("ids", expanding=True)), {"ids": list(ids)}).mappings().all()
    return [{"ticket_id": str(r["id"]), "title": r["title"], "objective_id": r["objective_id"], "status": r["status"], "planned": r["plan_id"] is not None,
             "review": r["review"], "candidate": r["candidate"], "promotion": r["promotion"], "pr_url": r["pr_url"]} for r in rows]


def _status(db: Session, ev: Optional[Dict[str, Any]], replied: bool) -> str:
    """received -> working -> answered; ``no_reply`` when every run of the owner event ended without one."""
    if replied:
        return "answered"
    if ev is None:
        return "received"
    runs = db.execute(text("SELECT status, started_at FROM agent_runs WHERE event_id = :e"), {"e": ev["id"]}).fetchall()
    if not runs:
        return "no_reply" if ev["status"] in ("ignored", "expired", "processed") else "received"
    if any(r[0] in ("queued", "claimed", "running") for r in runs):
        return "working" if any(r[1] is not None or r[0] in ("claimed", "running") for r in runs) else "received"
    return "no_reply"


def view(db: Session, limit: int = 20) -> Dict[str, Any]:
    """The Owner Console's conversation, newest first: each owner row, its status, the company's reply
    and what came from it (tickets, plans, design reviews, candidates, PRs)."""
    rows = db.execute(text("SELECT id, kind, text, payload, created_at FROM society_owner_thread WHERE author = 'owner' ORDER BY created_at DESC LIMIT :n"),
                      {"n": int(limit)}).mappings().all()
    out = []
    for r in rows:
        ev = _event_for(db, r["id"])
        rep = _reply_for(db, r["id"])
        refs = dict(r["payload"] or {})
        ids = [str(refs["ticket_id"])] if refs.get("ticket_id") else []
        if rep:
            ids += list((rep["payload"] or {}).get("ticket_ids") or [])
        if ev is not None:
            ids += _story_ticket_ids(db, ev["correlation_id"])
        plans = db.execute(text("SELECT id, status, ticket_ids FROM society_daily_plans WHERE owner_entry_id = :e ORDER BY created_at"), {"e": r["id"]}).mappings().all()
        for p in plans:
            ids += [str(t) for t in (p["ticket_ids"] or [])]
        out.append({"id": str(r["id"]), "kind": r["kind"], "text": r["text"], "refs": refs, "created_at": r["created_at"].isoformat(),
                    "status": _status(db, ev, rep is not None),
                    "reply": ({"text": rep["text"], **(rep["payload"] or {}), "created_at": rep["created_at"].isoformat()} if rep else None),
                    "plans": [{"id": str(p["id"]), "status": p["status"]} for p in plans],
                    "tickets": _tickets_view(db, list(dict.fromkeys(ids)))})
    return {"entries": out, "working": any(e["status"] in ("received", "working") for e in out)}


def plan_owner_stories(db: Session, settings: Any, now: Optional[datetime] = None) -> int:
    """Rank the proposed, unplanned tickets the company filed in a fresh owner story into a plan of
    their own (``owner_entry_id``) -- minutes, not the next scheduled cycle. The caller commits."""
    from . import tickets  # noqa: PLC0415

    now = now or utcnow()
    made = 0
    rows = db.execute(text(
        "SELECT t.id, t.payload, e.correlation_id FROM society_owner_thread t JOIN society_events e ON e.idempotency_key = 'owner:' || t.id::text "
        "WHERE t.author = 'owner' AND t.created_at >= :since ORDER BY t.created_at"), {"since": now - STORY_WINDOW}).mappings().all()
    for r in rows:
        ids = _story_ticket_ids(db, r["correlation_id"])
        if not ids:
            continue
        waiting = [str(x[0]) for x in db.execute(text("SELECT id FROM society_tickets WHERE id::text IN :ids AND status = 'proposed' AND plan_id IS NULL")
                                                .bindparams(bindparam("ids", expanding=True)), {"ids": ids}).fetchall()]
        if not waiting:
            continue
        plan = tickets.build_plan(db, None, now, red_cap=settings.company_max_high_risk_investigations, plan_max=settings.company_plan_max,
                                  per_department=settings.company_plan_per_department, ticket_ids=waiting,
                                  directive_id=(r["payload"] or {}).get("directive_id"), owner_entry_id=str(r["id"]))
        made += int(bool(plan and plan.get("id")))
    return made


# ── latency, measured ────────────────────────────────────────────────────


def _p95(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[max(0, math.ceil(0.95 * len(xs)) - 1)], 1)


def _metric(xs: List[float], target: int, late: int = 0) -> Dict[str, Any]:
    p95 = _p95(xs)
    return {"n": len(xs), "p95_s": p95, "max_s": round(max(xs), 1) if xs else None, "target_s": target, "late_unanswered": late,
            "ok": None if p95 is None and not late else bool(p95 is not None and p95 < target and not late)}


def slo(db: Session, now: Optional[datetime] = None, days: int = 7) -> Dict[str, Any]:
    """Owner event -> first run start (p95 < 30 s), -> Governor reply (p95 < 2 min), directive ->
    first ticket in a plan (< 10 min), over ``days``. A still-unanswered owner row older than a target
    counts against it."""
    now = now or utcnow()
    since = now - timedelta(days=days)
    starts = db.execute(text(
        "SELECT EXTRACT(EPOCH FROM (MIN(r.started_at) - e.created_at)) FROM society_events e JOIN agent_runs r ON r.event_id = e.id "
        "WHERE e.event_type IN :types AND e.created_at >= :s AND r.started_at IS NOT NULL GROUP BY e.id").bindparams(bindparam("types", expanding=True)),
        {"types": list(OWNER_EVENT_TYPES), "s": since}).fetchall()
    replies = db.execute(text(
        "SELECT EXTRACT(EPOCH FROM (c.created_at - o.created_at)) FROM society_owner_thread o JOIN society_owner_thread c ON c.reply_to = o.id "
        "WHERE o.created_at >= :s"), {"s": since}).fetchall()
    unanswered = db.execute(text(
        "SELECT COUNT(*) FROM society_owner_thread o WHERE o.author = 'owner' AND o.created_at >= :s AND o.created_at < :late "
        "AND NOT EXISTS (SELECT 1 FROM society_owner_thread c WHERE c.reply_to = o.id)"),
        {"s": since, "late": now - timedelta(seconds=SLO_TARGETS["reply"])}).scalar() or 0
    plans = db.execute(text(
        "SELECT EXTRACT(EPOCH FROM (MIN(p.created_at) - o.created_at)) FROM society_owner_thread o JOIN society_daily_plans p "
        "ON (p.owner_entry_id = o.id OR p.directive_id::text = o.payload ->> 'directive_id') AND p.created_at >= o.created_at "
        "WHERE o.kind = 'directive' AND o.created_at >= :s GROUP BY o.id"), {"s": since}).fetchall()
    unplanned = db.execute(text(
        "SELECT COUNT(*) FROM society_owner_thread o WHERE o.kind = 'directive' AND o.created_at >= :s AND o.created_at < :late "
        "AND NOT EXISTS (SELECT 1 FROM society_daily_plans p WHERE (p.owner_entry_id = o.id OR p.directive_id::text = o.payload ->> 'directive_id') "
        "AND p.created_at >= o.created_at)"), {"s": since, "late": now - timedelta(seconds=SLO_TARGETS["directive_to_plan"])}).scalar() or 0
    return {"window_days": days,
            "first_run_start": _metric([float(r[0]) for r in starts], SLO_TARGETS["first_run_start"]),
            "reply": _metric([float(r[0]) for r in replies], SLO_TARGETS["reply"], int(unanswered)),
            "directive_to_plan": _metric([float(r[0]) for r in plans], SLO_TARGETS["directive_to_plan"], int(unplanned))}


__all__ = ["KINDS", "LABEL", "SLO_TARGETS", "answer", "context_view", "directive_set", "entry_for_correlation", "plan_owner_stories", "record", "slo", "story_ticket_count", "view"]
