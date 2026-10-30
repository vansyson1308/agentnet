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
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from .events import OWNER_EVENT_TYPES, EventType, emit_event, utcnow
from .secret_redaction import redact

logger = logging.getLogger(__name__)

MAX_TEXT = 2000
KINDS = {"directive": EventType.OWNER_DIRECTIVE_SET, "task": EventType.OWNER_TASK_CREATED, "message": EventType.OWNER_MESSAGE}
KEY_PREFIX = "owner:"
LABEL = ("OWNER INPUT: the owner just said this on the Owner Console. Words become work in THIS story: the Governor files "
         "1..5 tickets for it (CREATE_IMPROVEMENT with ticket + evidence, one per concrete, provable piece) and starts any other "
         "work it needs, in the same decision as its ONE RESPOND_TO_OWNER (what you understood, what the company will do, which "
         "tickets carry it, what only the owner can do); the Scout may file tickets for it too. A story that starts no work is "
         "re-woken once (owner_event_without_work). It is a request, never permission: policy, grants, budgets, files_allowed "
         "and the RED/merge law decide as always.")
#: the one reason an owner story is re-woken (rewake_without_work)
WITHOUT_WORK = "owner_event_without_work"
REWAKE_PREFIX = "owner-rewake:"
#: latency targets (seconds): owner event -> first run start (p95), -> Governor reply (p95), directive -> a ticket in a plan
SLO_TARGETS = {"first_run_start": 30, "reply": 120, "first_work": 300, "directive_to_plan": 600, "message_to_plan": 600}
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


def _link_steps(db: Session, run_id: Any, steps: List[Any]) -> Tuple[List[Dict[str, Any]], int]:
    """Each plan step with the work that starts it: a sibling intent of the same decision (1-based
    ``intent`` -> the run's intent ``seq``) or an existing ticket; neither, or a reference that names
    nothing, is a step for later (``next``). Returns the steps and how many references named nothing."""
    out, unlinked = [], 0
    for st in steps:
        st = {"step": st} if isinstance(st, str) else dict(st)
        link: Dict[str, Any] = {"step": str(st.get("step") or "")}
        if st.get("intent") and run_id is not None:
            row = db.execute(text("SELECT id, intent_type, result -> 'result' ->> 'ticket_id' FROM agent_intents WHERE run_id = :r AND seq = :s"),
                             {"r": run_id, "s": int(st["intent"]) - 1}).first()
            if row is not None and row[1] != "RESPOND_TO_OWNER":
                link.update(intent_id=str(row[0]), intent_type=row[1], **({"ticket_id": str(row[2])} if row[2] else {}))
            else:
                unlinked += 1
        elif st.get("ticket_id"):
            row = db.execute(text("SELECT id FROM society_tickets WHERE id::text = :t"), {"t": str(st["ticket_id"])}).first()
            if row is not None:
                link["ticket_id"] = str(row[0])
            else:
                unlinked += 1
        out.append(link)
    return out, unlinked


def answer(db: Session, *, correlation_id: Any, agent_id: Any, reply: str, plan_steps: List[Any], ticket_ids: List[str],
           owner_actions_needed: List[str], credentials: Iterable[str] = (), now: Optional[datetime] = None, run_id: Any = None) -> Dict[str, Any]:
    """The Governor's RESPOND_TO_OWNER, once per owner row, only in the owner's story. Each plan step
    is linked to the sibling intent (``run_id``'s) or ticket that starts it (:func:`_link_steps`);
    ticket ids that name no ticket are dropped (and counted). Raises ValueError. Does NOT commit."""
    entry = entry_for_correlation(db, correlation_id)
    if entry is None:
        raise ValueError("RESPOND_TO_OWNER answers the owner event that opened this story; this story has none")
    if _reply_for(db, entry["id"]) is not None:
        raise ValueError("the owner was already answered in this story (one reply per owner event)")
    # every ticket this story filed is listed, whether or not the reply named it (G1v: plan_steps/ticket_ids
    # carry the ticket ids the story filed; the sibling CREATE_IMPROVEMENTs already ran -- the reply runs last)
    ids = list(dict.fromkeys([str(t) for t in ticket_ids] + _story_ticket_ids(db, correlation_id)))
    known = {str(r[0]) for r in db.execute(text("SELECT id FROM society_tickets WHERE id::text IN :ids").bindparams(bindparam("ids", expanding=True)),
                                           {"ids": ids}).fetchall()} if ids else set()
    steps, unlinked = _link_steps(db, run_id, plan_steps)
    payload = {"plan_steps": steps, "ticket_ids": [t for t in ids if t in known], "owner_actions_needed": list(owner_actions_needed),
               **({"unknown_ticket_ids": len([t for t in ids if t not in known])} if len(known) < len(ids) else {}),
               **({"unlinked_steps": unlinked} if unlinked else {})}
    rid = uuid.uuid4()
    _insert(db, entry_id=rid, author="company", kind="reply", body=_clean(" ".join(reply.split()), "reply", credentials),
            payload=_clean(payload, "payload", credentials), agent_id=agent_id, reply_to=entry["id"], now=now or utcnow())
    return {"reply_id": str(rid), "owner_entry_id": str(entry["id"]), "ticket_ids": payload["ticket_ids"],
            "started": [st["intent_type"] for st in steps if st.get("intent_id")], "unlinked_steps": unlinked}


PORTFOLIO_LABEL = ("LIVE DATA read from the database when this run started -- not memory, not earlier messages. Every id, "
                   "count or status you state to the owner comes from this block; where memory or an earlier reply disagrees, "
                   "this block is right. A READY candidate listed here can be promoted (REQUEST_PR_PROMOTION) when "
                   "change_budget has room; the controller still decides.")
#: rows per list in the live block (it rides every run of an owner story)
PORTFOLIO_LIMIT = 8
#: candidate statuses still moving (not READY, not terminal)
IN_FLIGHT = ("requested", "building", "built", "qa_running", "qa_passed", "qa_failed", "security_review")


def portfolio(db: Session, settings: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The company's live state for an owner story (owner 2026-10-29: the Governor reasoned from
    stale memory -- "only e14a7bb1", "1 PR open" -- while /owner showed 3 READY and 0/5 PRs). Read
    from the database at run time with the same code /owner uses; structural fields and our own
    labels only."""
    from . import policy, promotion_queue  # noqa: PLC0415

    now = now or utcnow()
    ready = [{k: r.get(k) for k in ("candidate_id", "title", "risk_tier", "promotion", "pr_number", "reason", "detail")}
             for r in promotion_queue.ready_view(db, settings, now, limit=PORTFOLIO_LIMIT)]
    caps = promotion_queue.worker_view_caps(db, settings, now)
    plan = db.execute(text("SELECT id, plan_date, status, ranking FROM society_daily_plans ORDER BY created_at DESC LIMIT 1")).mappings().first()
    by_status = {r[0]: int(r[1]) for r in db.execute(text(
        "SELECT status, COUNT(*) FROM society_tickets WHERE status NOT IN ('merged', 'closed', 'refused', 'already_satisfied') GROUP BY 1")).all()}
    tickets_ = [{"ticket_id": str(r[0]), "title": r[1], "status": r[2], "objective_id": r[3]} for r in db.execute(text(
        "SELECT id, title, status, objective_id FROM society_tickets WHERE status IN ('proposed', 'planned', 'approved', 'building') "
        "ORDER BY updated_at DESC LIMIT :n"), {"n": PORTFOLIO_LIMIT}).all()]
    cands = [{"candidate_id": str(r[0]), "title": r[1], "status": r[2]} for r in db.execute(text(
        "SELECT id, title, status FROM code_candidates WHERE status IN :s ORDER BY updated_at DESC LIMIT :n").bindparams(
        bindparam("s", expanding=True)), {"s": list(IN_FLIGHT), "n": PORTFOLIO_LIMIT}).all()]
    spent = policy.spend_today_usd(db, now=now)
    reserve = float(getattr(settings, "owner_reserve_usd", 0) or 0)
    roles = [{"role": r[0], "spent_usd": round(float(r[2] or 0), 4), "budget_usd": float(r[1] or 0)} for r in db.execute(text(
        "SELECT g.role, g.daily_model_budget_usd, (SELECT COALESCE(SUM(r.cost_usd), 0) FROM agent_runs r WHERE r.agent_id = g.agent_id AND r.created_at >= :d) "
        "FROM agent_capability_grants g ORDER BY g.role"), {"d": now.replace(hour=0, minute=0, second=0, microsecond=0)}).all()]
    return {"label": PORTFOLIO_LABEL, "as_of": now.isoformat(),
            "ready_candidates": ready,
            "change_budget": {k: caps.get(k) for k in ("promotions_today", "max_promotions_per_day", "open_prs", "max_open_autonomous_prs", "provider", "auto_merge_enabled")},
            "today_plan": ({"plan_id": str(plan["id"]), "plan_date": str(plan["plan_date"]), "status": plan["status"],
                            "tickets": [{"ticket_id": t.get("ticket_id"), "title": t.get("title")} for t in (plan["ranking"] or [])][:PORTFOLIO_LIMIT]} if plan else None),
            "tickets_by_status": by_status, "tickets_in_flight": tickets_, "candidates_in_flight": cands,
            "budget": {"daily_model_budget_usd": float(settings.daily_model_budget_usd), "spent_today_usd": round(float(spent), 4),
                       "left_usd": round(max(0.0, float(settings.daily_model_budget_usd) - float(spent)), 4),
                       **({"owner_reserve_left_usd": round(max(0.0, reserve - float(policy.owner_spend_today_usd(db, now))), 4)} if reserve > 0 else {}),
                       "roles": roles}}


def context_view(db: Session, event: Any, settings: Any = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """What a role sees in an owner story (``AgentContext.owner_request``): the owner's words, labelled,
    and -- with ``settings`` -- the live portfolio (:func:`portfolio`)."""
    if event is None or event.correlation_id is None:
        return {}
    entry = entry_for_correlation(db, event.correlation_id)
    if entry is None:
        return {}
    out = {"label": LABEL, "kind": entry["kind"], "text": entry["text"], "refs": entry["payload"] or {},
           "answered": _reply_for(db, entry["id"]) is not None}
    if event.event_type == EventType.OWNER_REWAKE:
        out["rewake"] = {"reason": WITHOUT_WORK, "instruction": (
            "this owner story ended with no work started. Now file 1..5 tickets that carry the owner's words (CREATE_IMPROVEMENT "
            "with ticket + evidence) and/or start the work they need; if the owner was already answered, do NOT answer again.")}
    if settings is not None:
        out["portfolio"] = portfolio(db, settings, now)
    return out


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


#: result keys worth showing per intent on the Owner Console (ids and statuses, never text)
_RESULT_KEYS = ("proposal_id", "ticket_id", "promotion_id", "candidate_id", "owner_action_id", "review_id", "status", "duplicate", "did")


def _work(db: Session, correlation_id: Any, limit: int = 12) -> List[Dict[str, Any]]:
    """The side-effecting intents of an owner story (RESPOND_TO_OWNER aside), oldest first, with
    their CURRENT status and the ids/statuses they produced."""
    from .memory_grounding import is_side_effecting  # noqa: PLC0415

    rows = db.execute(text(
        "SELECT i.id, i.intent_type, i.execution_status, i.policy_decision, i.result, i.error, i.executed_at, r.role "
        "FROM agent_intents i JOIN agent_runs r ON r.id = i.run_id WHERE r.correlation_id = :c AND i.intent_type <> 'RESPOND_TO_OWNER' "
        "ORDER BY i.created_at, i.seq"), {"c": correlation_id}).all()
    out = []
    for iid, itype, status, decision, result, error, executed_at, role in rows:
        if not is_side_effecting(itype):
            continue
        res = (result or {}).get("result") if isinstance(result, dict) else None
        out.append({"intent_id": str(iid), "type": itype, "role": role, "status": str(status), "policy": str(decision),
                    "result": {k: res[k] for k in _RESULT_KEYS if isinstance(res, dict) and k in res},
                    "error": (error or "")[:200] or None, "executed_at": executed_at.isoformat() if executed_at else None})
    return out[:limit]


def _steps_view(db: Session, steps: List[Any], work: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {w["intent_id"]: w for w in work}
    out = []
    for st in steps or []:
        st = {"step": st} if isinstance(st, str) else dict(st)
        if st.get("intent_id"):
            w = by_id.get(st["intent_id"])
            if w is None:
                row = db.execute(text("SELECT intent_type, execution_status FROM agent_intents WHERE id::text = :i"), {"i": st["intent_id"]}).first()
                w = {"type": row[0], "status": str(row[1])} if row else {"type": st.get("intent_type"), "status": "unknown"}
            out.append({"step": st["step"], "state": "started", "intent_id": st["intent_id"], "type": w["type"], "status": w["status"],
                        **({"ticket_id": st["ticket_id"]} if st.get("ticket_id") else {})})
        elif st.get("ticket_id"):
            row = db.execute(text("SELECT title, status FROM society_tickets WHERE id::text = :t"), {"t": st["ticket_id"]}).first()
            out.append({"step": st["step"], "state": "ticket", "ticket_id": st["ticket_id"], "status": row[1] if row else "unknown"})
        else:
            out.append({"step": st["step"], "state": "next"})
    return out


def view(db: Session, limit: int = 20) -> Dict[str, Any]:
    """The Owner Console's conversation, newest first: each owner row, its status, the company's reply
    (each plan step with the intent or ticket that started it, or ``next``), the intents the story
    executed and their results, and what came from it (tickets, plans, design reviews, candidates, PRs)."""
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
        work = _work(db, ev["correlation_id"]) if ev is not None else []
        if ev is not None:
            ids += _story_ticket_ids(db, ev["correlation_id"])
        plans = db.execute(text("SELECT id, status, ticket_ids FROM society_daily_plans WHERE owner_entry_id = :e ORDER BY created_at"), {"e": r["id"]}).mappings().all()
        for p in plans:
            ids += [str(t) for t in (p["ticket_ids"] or [])]
        reply = None
        if rep:
            pay = dict(rep["payload"] or {})
            reply = {"text": rep["text"], **pay, "plan_steps": _steps_view(db, pay.get("plan_steps") or [], work), "created_at": rep["created_at"].isoformat()}
        rewoken = db.execute(text("SELECT 1 FROM society_events WHERE idempotency_key = :k"), {"k": f"{REWAKE_PREFIX}{r['id']}"}).first() is not None
        out.append({"id": str(r["id"]), "kind": r["kind"], "text": r["text"], "refs": refs, "created_at": r["created_at"].isoformat(),
                    "status": _status(db, ev, rep is not None), "reply": reply, "work": work, **({"rewoken": WITHOUT_WORK} if rewoken else {}),
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
        # the owner's own words: every ticket the story filed (<= OWNER_STORY_TICKETS) fits its plan; the
        # per-department split exists to spread a scheduled cycle, not to drop half of an owner request
        cap = max(settings.company_plan_max, min(len(waiting), tickets.OWNER_STORY_TICKETS))
        plan = tickets.build_plan(db, None, now, red_cap=settings.company_max_high_risk_investigations, plan_max=cap,
                                  per_department=cap, ticket_ids=waiting,
                                  directive_id=(r["payload"] or {}).get("directive_id"), owner_entry_id=str(r["id"]))
        made += int(bool(plan and plan.get("id")))
    return made


def rewake_without_work(db: Session, settings: Any = None, now: Optional[datetime] = None) -> int:
    """Owner 2026-10-29 G1v: words must become work. An owner story (a message or a directive -- an owner
    TASK is already a ticket) whose runs all ended with no ticket filed and no side-effecting intent
    executed is recorded ``owner_event_without_work`` and the Governor is woken ONCE more in that same
    story (``owner.rewake``, idempotent per owner row, priority like the owner's own events; the loop
    breaker and the caps still apply). Never a second time. The caller commits."""
    now = now or utcnow()
    rows = db.execute(text(
        "SELECT o.id, o.kind, e.id AS event_id, e.correlation_id FROM society_owner_thread o "
        "JOIN society_events e ON e.idempotency_key = 'owner:' || o.id::text "
        "WHERE o.author = 'owner' AND o.kind IN ('message', 'directive') AND o.created_at >= :since AND e.status <> 'pending' "
        "AND NOT EXISTS (SELECT 1 FROM society_events x WHERE x.idempotency_key = :prefix || o.id::text) "
        "AND EXISTS (SELECT 1 FROM agent_runs r WHERE r.correlation_id = e.correlation_id) "
        "AND NOT EXISTS (SELECT 1 FROM agent_runs r WHERE r.correlation_id = e.correlation_id AND r.status IN ('queued', 'claimed', 'running', 'failed')) "
        "AND NOT EXISTS (SELECT 1 FROM agent_runs r JOIN agent_intents i ON i.run_id = r.id WHERE r.correlation_id = e.correlation_id "
        "                AND i.execution_status = 'executed' AND i.intent_type NOT IN :quiet) "
        "ORDER BY o.created_at").bindparams(bindparam("quiet", expanding=True)),
        {"since": now - STORY_WINDOW, "prefix": REWAKE_PREFIX, "quiet": _quiet_types()}).mappings().all()
    from ..models import SocietyEvent  # noqa: PLC0415

    n = 0
    for r in rows:
        if _story_ticket_ids(db, r["correlation_id"]):
            continue
        cause = db.query(SocietyEvent).filter(SocietyEvent.id == r["event_id"]).first()
        emit_event(db, event_type=EventType.OWNER_REWAKE, payload={"owner_entry_id": str(r["id"]), "kind": r["kind"], "reason": WITHOUT_WORK},
                   actor_type="system", subject_type="owner_entry", subject_id=r["id"], causation=cause, correlation_id=r["correlation_id"],
                   idempotency_key=f"{REWAKE_PREFIX}{r['id']}")
        logger.info("owner story %s: %s -- the Governor is woken once more", str(r["id"])[:8], WITHOUT_WORK)
        n += 1
    return n


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


def _quiet_types() -> List[str]:
    """Intent types that start no work: the reply itself and the non-side-effecting ones."""
    from .memory_grounding import NON_SIDE_EFFECT_TYPES  # noqa: PLC0415

    return sorted(NON_SIDE_EFFECT_TYPES | {"RESPOND_TO_OWNER"})


def slo(db: Session, now: Optional[datetime] = None, days: int = 7) -> Dict[str, Any]:
    """Owner event -> first run start (p95 < 30 s), -> Governor reply (p95 < 2 min), -> first
    side-effecting intent executed (p95 < 5 min: words -> work), directive ->
    first ticket in a plan (< 10 min), over ``days``. A still-unanswered owner row older than a target
    counts against it."""
    now = now or utcnow()
    since = now - timedelta(days=days)
    starts = db.execute(text(
        "SELECT EXTRACT(EPOCH FROM (MIN(r.started_at) - e.created_at)) FROM society_events e JOIN agent_runs r ON r.event_id = e.id "
        "WHERE e.event_type IN :types AND e.idempotency_key LIKE 'owner:%' AND e.created_at >= :s AND r.started_at IS NOT NULL GROUP BY e.id").bindparams(
        bindparam("types", expanding=True)), {"types": list(OWNER_EVENT_TYPES), "s": since}).fetchall()
    replies = db.execute(text(
        "SELECT EXTRACT(EPOCH FROM (c.created_at - o.created_at)) FROM society_owner_thread o JOIN society_owner_thread c ON c.reply_to = o.id "
        "WHERE o.created_at >= :s"), {"s": since}).fetchall()
    unanswered = db.execute(text(
        "SELECT COUNT(*) FROM society_owner_thread o WHERE o.author = 'owner' AND o.created_at >= :s AND o.created_at < :late "
        "AND NOT EXISTS (SELECT 1 FROM society_owner_thread c WHERE c.reply_to = o.id)"),
        {"s": since, "late": now - timedelta(seconds=SLO_TARGETS["reply"])}).scalar() or 0
    work = db.execute(text(
        "SELECT EXTRACT(EPOCH FROM (MIN(i.executed_at) - o.created_at)) FROM society_owner_thread o "
        "JOIN society_events e ON e.idempotency_key = 'owner:' || o.id::text JOIN agent_runs r ON r.correlation_id = e.correlation_id "
        "JOIN agent_intents i ON i.run_id = r.id WHERE o.author = 'owner' AND o.created_at >= :s AND i.execution_status = 'executed' "
        "AND i.intent_type NOT IN :quiet GROUP BY o.id").bindparams(bindparam("quiet", expanding=True)), {"s": since, "quiet": _quiet_types()}).fetchall()
    idle = db.execute(text(
        "SELECT COUNT(*) FROM society_owner_thread o JOIN society_events e ON e.idempotency_key = 'owner:' || o.id::text "
        "WHERE o.author = 'owner' AND o.created_at >= :s AND o.created_at < :late AND NOT EXISTS ("
        " SELECT 1 FROM agent_runs r JOIN agent_intents i ON i.run_id = r.id WHERE r.correlation_id = e.correlation_id "
        " AND i.execution_status = 'executed' AND i.intent_type NOT IN :quiet)").bindparams(bindparam("quiet", expanding=True)),
        {"s": since, "late": now - timedelta(seconds=SLO_TARGETS["first_work"]), "quiet": _quiet_types()}).scalar() or 0
    plans = db.execute(text(
        "SELECT EXTRACT(EPOCH FROM (MIN(p.created_at) - o.created_at)) FROM society_owner_thread o JOIN society_daily_plans p "
        "ON (p.owner_entry_id = o.id OR p.directive_id::text = o.payload ->> 'directive_id') AND p.created_at >= o.created_at "
        "WHERE o.kind = 'directive' AND o.created_at >= :s GROUP BY o.id"), {"s": since}).fetchall()
    unplanned = db.execute(text(
        "SELECT COUNT(*) FROM society_owner_thread o WHERE o.kind = 'directive' AND o.created_at >= :s AND o.created_at < :late "
        "AND NOT EXISTS (SELECT 1 FROM society_daily_plans p WHERE (p.owner_entry_id = o.id OR p.directive_id::text = o.payload ->> 'directive_id') "
        "AND p.created_at >= o.created_at)"), {"s": since, "late": now - timedelta(seconds=SLO_TARGETS["directive_to_plan"])}).scalar() or 0
    to_plan = db.execute(text(
        "SELECT EXTRACT(EPOCH FROM (MIN(p.created_at) - o.created_at)) FROM society_owner_thread o JOIN society_daily_plans p "
        "ON p.owner_entry_id = o.id AND p.created_at >= o.created_at WHERE o.kind = 'message' AND o.created_at >= :s GROUP BY o.id"), {"s": since}).fetchall()
    message_unplanned = db.execute(text(
        "SELECT COUNT(*) FROM society_owner_thread o WHERE o.kind = 'message' AND o.author = 'owner' AND o.created_at >= :s AND o.created_at < :late "
        "AND NOT EXISTS (SELECT 1 FROM society_daily_plans p WHERE p.owner_entry_id = o.id AND p.created_at >= o.created_at)"),
        {"s": since, "late": now - timedelta(seconds=SLO_TARGETS["message_to_plan"])}).scalar() or 0
    rewoken = db.execute(text("SELECT COUNT(*) FROM society_events WHERE idempotency_key LIKE :p AND created_at >= :s"),
                         {"p": REWAKE_PREFIX + "%", "s": since}).scalar() or 0
    return {"window_days": days, "without_work_rewakes": int(rewoken),
            "first_run_start": _metric([float(r[0]) for r in starts], SLO_TARGETS["first_run_start"]),
            "reply": _metric([float(r[0]) for r in replies], SLO_TARGETS["reply"], int(unanswered)),
            "first_work": _metric([float(r[0]) for r in work], SLO_TARGETS["first_work"], int(idle)),
            "directive_to_plan": _metric([float(r[0]) for r in plans], SLO_TARGETS["directive_to_plan"], int(unplanned)),
            "message_to_plan": _metric([float(r[0]) for r in to_plan], SLO_TARGETS["message_to_plan"], int(message_unplanned))}


__all__ = ["KINDS", "LABEL", "SLO_TARGETS", "answer", "context_view", "directive_set", "entry_for_correlation", "plan_owner_stories", "portfolio", "record", "rewake_without_work", "slo", "story_ticket_count", "view"]
