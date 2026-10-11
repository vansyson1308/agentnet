"""Tickets: the unit of company work and the meaning gate (deterministic, no model).

A ticket rides on one ImprovementProposal and says why the work exists: an
ACTIVE charter objective, one of its key-result metrics (number + direction)
and a proof (acceptance test node ids, or ``probe:<metric_id>``). In company
mode no candidate is created without one that passes ``check``; a proof that
already passes on the base revision closes it ``already_satisfied``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from . import charter, metrics
from .events import EventType, emit_event

logger = logging.getLogger(__name__)
DUPLICATE_WINDOW_DAYS = 14
LIVE_STATUSES = ("approved", "building", "ready", "merged")
#: Owner tasks (and incidents) skip the daily plan; they still pass the gate.
BYPASS_SOURCES = ("owner", "incident")
PLAN_MAX, PER_DEPARTMENT = 3, 2
MODEL_TICKETS_PER_DEPARTMENT = 2
#: Proofs that are not pytest node ids: a probe metric, a bench task, an owner issue.
NON_TEST_PROOFS = ("probe:", "bench:", "issue:")
#: Supplied tickets (``supply``): backlog source -> (charter department, KR metric). An issue maps to the objective its owner tagged.
SUPPLY = {"bench": ("engineering", "bench_holdout_pass_at_1"), "maintenance_incident": ("sre_release", "core_journey_success_rate"),
          "github_issue": ("strategy_product", None)}
OPEN_TICKET = ("proposed", "planned", "approved", "building", "ready")
_NS = uuid.UUID("6c1f0e3a-8a51-4c2e-9d0b-2f6a1c7e5b10")
DEPARTMENT_BY_ROLE = {"governor": "ceo_office", "scout": "strategy_product", "architect": "engineering", "builder": "engineering", "qa": "quality", "security": "security", "evaluator": "analytics"}


class GateRefused(Exception):
    pass


def create(db: Session, *, proposal_id: uuid.UUID, title: str, fields: Dict[str, Any], source: str, role: str, importance: int) -> uuid.UUID:
    tid = uuid.uuid4()
    db.execute(text(
        "INSERT INTO society_tickets (id, proposal_id, title, objective_id, metric_id, expected_effect, direction, proof, source, priority, department) "
        "VALUES (:id, :p, :t, :o, :m, :e, :d, CAST(:proof AS JSONB), :s, :pr, :dep)"),
        {"id": tid, "p": proposal_id, "t": title[:255], "o": fields["objective_id"], "m": fields["metric_id"], "e": fields["expected_effect"],
         "d": fields["direction"], "proof": json.dumps(list(fields.get("proof") or [])), "s": source, "pr": max(0, min(5, importance // 20)),
         "dep": DEPARTMENT_BY_ROLE.get(role, "strategy_product")})
    return tid


def model_ticket_refusal(db: Session, role: str, has_evidence: bool, now: datetime) -> Optional[str]:
    if not has_evidence:
        return "a ticket filed by a model must cite evidence (signal, baseline, observed, window, actionable_reason)"
    dept = DEPARTMENT_BY_ROLE.get(role, "strategy_product")
    n = db.execute(text("SELECT COUNT(*) FROM society_tickets WHERE department = :d AND source IN ('scout', 'department') AND created_at >= :t"),
                   {"d": dept, "t": now.replace(hour=0, minute=0, second=0, microsecond=0)}).scalar() or 0
    if n >= MODEL_TICKETS_PER_DEPARTMENT:
        return f"{dept} already filed {n} ticket(s) today (at most {MODEL_TICKETS_PER_DEPARTMENT}); the backlog supplies the rest"
    return None


def for_proposal(db: Session, proposal_id: Optional[uuid.UUID]) -> Optional[Dict[str, Any]]:
    if proposal_id is None:
        return None
    row = db.execute(text("SELECT * FROM society_tickets WHERE proposal_id = :p"), {"p": proposal_id}).mappings().first()
    return dict(row) if row else None


def for_correlation(db: Session, correlation_id: Any) -> Optional[Dict[str, Any]]:
    """The ticket a story designs: the one whose ``company.ticket_approved`` event opened it."""
    tid = db.execute(text("SELECT payload ->> 'ticket_id' FROM society_events WHERE correlation_id = :c AND event_type = :t ORDER BY created_at LIMIT 1"),
                     {"c": correlation_id, "t": EventType.COMPANY_TICKET_APPROVED}).scalar()
    if not tid:
        return None
    row = db.execute(text("SELECT * FROM society_tickets WHERE id = CAST(:id AS UUID)"), {"id": tid}).mappings().first()
    return dict(row) if row else None


def set_status(db: Session, ticket_id: Any, status: str, reason: Optional[str] = None, **cols: Any) -> None:
    sets = "".join(f", {k} = :{k}" for k in cols)
    db.execute(text(f"UPDATE society_tickets SET status = :s, reason = COALESCE(:r, reason), updated_at = NOW(){sets} WHERE id = :id"),  # noqa: S608 -- column names are ours
               {"s": status, "r": reason[:500] if reason else None, "id": ticket_id, **cols})


def add_cost(db: Session, ticket_id: Any, usd: Any) -> None:
    db.execute(text("UPDATE society_tickets SET cost_usd = cost_usd + :c, updated_at = NOW() WHERE id = :id"), {"c": float(usd or 0), "id": ticket_id})


def proof_tests(ticket: Dict[str, Any]) -> List[str]:
    return [p for p in (ticket.get("proof") or []) if isinstance(p, str) and not p.startswith(NON_TEST_PROOFS)]


def check(db: Session, proposal_id: Optional[uuid.UUID], spec: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """The meaning gate. Returns the ticket (now ``building``) or raises GateRefused
    after recording the refusal on the ticket (the caller commits)."""
    t = for_proposal(db, proposal_id)
    if t is None:
        raise GateRefused("company mode: the proposal has no ticket (objective_id, metric_id, expected_effect, direction, proof)")

    def refuse(why: str) -> None:
        set_status(db, t["id"], "refused", why)
        raise GateRefused(f"ticket refused: {why}")

    files = sorted(str(f) for f in spec.get("files_allowed") or [])
    if charter.CHARTER_REPO_PATH in files:
        refuse("the company charter is owner-edited only")
    obj = charter.find(db, str(t.get("objective_id") or ""))
    if obj is None or obj["status"] != "active":
        refuse(f"objective {t.get('objective_id')!r} is not active")
    if t.get("metric_id") not in [kr["metric_id"] for kr in obj["key_results"]] or not metrics.resolvable(str(t.get("metric_id"))):
        refuse(f"metric {t.get('metric_id')!r} is not a key result of {obj['id']}")
    proof = sorted(t.get("proof") or [])
    if not proof:
        refuse("no proof (acceptance test node ids or probe:<metric_id>)")
    missing = [p for p in proof_tests(t) if p not in (spec.get("acceptance_tests") or [])]
    if missing:
        refuse(f"the spec does not run the proof {missing}")
    if t["status"] not in ("approved", "building") and t["source"] not in BYPASS_SOURCES:
        raise GateRefused(f"ticket is {t['status']}: it builds only after the owner approves it in a daily plan")
    dup = db.execute(text(
        "SELECT id FROM society_tickets WHERE id <> :id AND status IN :live AND updated_at >= :since AND (status <> 'building' OR candidate_id IN "
        "(SELECT id FROM code_candidates WHERE status NOT IN ('rejected', 'failed', 'abandoned'))) "
        "AND proof = CAST(:proof AS JSONB) AND files = CAST(:files AS JSONB) LIMIT 1").bindparams(bindparam("live", expanding=True)),
        {"id": t["id"], "live": list(LIVE_STATUSES), "since": now - timedelta(days=DUPLICATE_WINDOW_DAYS),
         "proof": json.dumps(proof), "files": json.dumps(files)}).scalar()
    if dup is not None:
        refuse(f"duplicate of ticket {dup} (same proof and files within {DUPLICATE_WINDOW_DAYS} days)")
    db.execute(text("UPDATE society_tickets SET proof = CAST(:proof AS JSONB), files = CAST(:files AS JSONB) WHERE id = :id"),
               {"id": t["id"], "proof": json.dumps(proof), "files": json.dumps(files)})
    set_status(db, t["id"], "building")
    return {**t, "objective": obj}


def link_text(ticket: Dict[str, Any]) -> str:  # the objective link the PR body carries (spec expected_effect)
    return f"[{ticket['objective_id']} {ticket['metric_id']} {ticket['direction']} {float(ticket['expected_effect']):g}]"


# ── the daily plan (Chief of Staff ranking, deterministic) and owner decisions ──


def score(t: Dict[str, Any], obj: Dict[str, Any]) -> float:
    """owner_priority x expected KR impact (share of the KR's gap) / cost."""
    kr = next((k for k in obj["key_results"] if k["metric_id"] == t["metric_id"]), None)
    if kr is None or kr["direction"] != t["direction"]:
        return 0.0
    gap = abs(float(kr["target"]) - float(kr["baseline"] or 0)) or 1.0
    impact = min(1.0, abs(float(t["expected_effect"] or 0)) / gap)
    return round((6 - int(obj["owner_priority"])) * impact / (1.0 + float(t["cost_usd"] or 0)), 6)


def is_red(t: Dict[str, Any]) -> bool:
    """A ticket whose change is RED before any design: a bench ticket changes the builder
    harness (backlog.HARNESS_PATHS, RED trusted base). Others are judged from their diff."""
    return any(isinstance(p, str) and p.startswith("bench:") for p in t.get("proof") or [])


def red_slots(db: Session, red_cap: int) -> int:
    """RED work the company can still take on: the high-risk cap minus open RED candidates
    and approved RED tickets still waiting for their design."""
    open_red = db.execute(text("SELECT COUNT(*) FROM code_candidates WHERE risk_tier IN ('red', 'never') "
                               "AND status NOT IN ('ready', 'rejected', 'failed', 'abandoned')")).scalar() or 0
    waiting = sum(1 for (proof,) in db.execute(text("SELECT proof FROM society_tickets WHERE status = 'approved' AND candidate_id IS NULL")).all()
                  if is_red({"proof": proof}))
    return max(0, int(red_cap) - int(open_red) - waiting)


def build_plan(db: Session, cycle_id: Optional[uuid.UUID], now: datetime, *, red_cap: int = 1) -> Optional[Dict[str, Any]]:
    """Rank the proposed tickets of ACTIVE objectives (<= 2 per department) into
    one plan of <= 3 that awaits the owner. Nothing in it builds until approved.
    A plan is buildable: it holds at most as many RED tickets as the company can
    still investigate (``red_slots``); non-RED tickets fill the rest."""
    active = {o["id"]: o for o in charter.objectives(db) if o["status"] == "active"}
    rows = db.execute(text("SELECT * FROM society_tickets WHERE status = 'proposed' AND plan_id IS NULL AND source NOT IN :bypass "
                           "AND created_at >= :since ORDER BY created_at").bindparams(bindparam("bypass", expanding=True)),
                      {"bypass": list(BYPASS_SOURCES), "since": now - timedelta(days=7)}).mappings().all()
    ranked = sorted(((score(dict(r), active[r["objective_id"]]), dict(r)) for r in rows if r["objective_id"] in active), key=lambda x: -x[0])
    chosen, per_dep, slots, red_skipped = [], {}, red_slots(db, red_cap), 0
    for sc, t in ranked:
        if sc > 0 and per_dep.get(t["department"], 0) < PER_DEPARTMENT and len(chosen) < PLAN_MAX:
            red = is_red(t)
            if red and slots <= 0:
                red_skipped += 1
                continue
            slots -= int(red)
            per_dep[t["department"]] = per_dep.get(t["department"], 0) + 1
            chosen.append({"ticket_id": str(t["id"]), "title": t["title"], "objective_id": t["objective_id"], "metric_id": t["metric_id"],
                           "department": t["department"], "score": sc, "risk": "RED" if red else "judged_from_diff"})
    if not chosen:
        if not active:
            why = "no active objective (activate one: company:activate:O<n>)"
        elif not ranked:
            why = f"no active objective has evidence: no proposed ticket for {sorted(active)} (backlog empty or mapped elsewhere)"
        elif red_skipped:
            why = f"{red_skipped} proposed RED ticket(s) wait: no high-risk slot is free (SOCIETY_COMPANY_MAX_HIGH_RISK_INVESTIGATIONS={red_cap})"
        else:
            why = f"{len(ranked)} proposed ticket(s) scored 0 (key result already met or effect in the wrong direction)"
        return {"id": None, "empty_reason": why}
    pid = uuid.uuid4()
    db.execute(text("INSERT INTO society_daily_plans (id, cycle_id, plan_date, ticket_ids, ranking) VALUES (:id, :c, :d, CAST(:t AS JSONB), CAST(:r AS JSONB))"),
               {"id": pid, "c": cycle_id, "d": now.date(), "t": json.dumps([c["ticket_id"] for c in chosen]), "r": json.dumps(chosen)})
    for c in chosen:
        set_status(db, c["ticket_id"], "planned", plan_id=pid)
    return {"id": str(pid), "status": "awaiting_owner", "ranking": chosen}


def _approved(db: Session, t: Dict[str, Any], user_id: Any) -> None:
    set_status(db, t["id"], "approved")
    # one wake per approval: a ticket that came back (record_design_failure) is approved again in a NEW plan
    key = f"company.ticket_approved:{t['id']}" + (f":{t['plan_id']}" if t.get("plan_id") else "")
    emit_event(db, event_type=EventType.COMPANY_TICKET_APPROVED, actor_type="operator", actor_id=user_id, subject_type="proposal", subject_id=t["proposal_id"],
               payload={"ticket_id": str(t["id"]), "proposal_id": str(t["proposal_id"]), "title": t["title"], "objective_id": t["objective_id"],
                        "metric_id": t["metric_id"], "expected_effect": float(t["expected_effect"]), "direction": t["direction"], "proof": t["proof"]},
               idempotency_key=key)


#: Design attempts an approved ticket gets before it goes back to proposed (the owner re-plans it).
DESIGN_FAILURES_TO_RETURN = 2
#: An approved ticket nobody designed within this long counts as one failed design attempt.
DESIGN_STALE_AFTER = timedelta(hours=6)
#: Refusals about the company's capacity (executor portfolio cap, daily change budget), not the design.
CAPACITY_REFUSALS = ("portfolio full", "change budget exhausted")


def _ticket_ref(db: Session, ref: Any) -> Optional[Dict[str, Any]]:
    """The ticket of a proposal id -- or of a ticket id a model passed as proposal_id."""
    try:
        rid = uuid.UUID(str(ref))
    except (TypeError, ValueError):
        return None
    t = for_proposal(db, rid)
    if t is None:
        row = db.execute(text("SELECT * FROM society_tickets WHERE id = :id"), {"id": rid}).mappings().first()
        t = dict(row) if row else None
    return t


def _approved_at(db: Session, ticket_id: Any) -> Optional[datetime]:
    return db.execute(text("SELECT MAX(created_at) FROM society_events WHERE event_type = :t AND payload ->> 'ticket_id' = :id"),
                      {"t": EventType.COMPANY_TICKET_APPROVED, "id": str(ticket_id)}).scalar()


def record_design_failure(db: Session, ref: Any, why: str, *, key: str) -> Optional[str]:
    """WHY a design attempt (REQUEST_CODE_CHANGE, or none in time) failed goes on the
    approved ticket; the second failure since its latest approval returns it to
    ``proposed`` (plan cleared) with the reason -- never silently stuck in approved.
    Gate refusals already closed the ticket ``refused``; other states are untouched.
    Idempotent per ``key``. Returns the ticket's new status (None: not applicable).
    The caller commits."""
    t = _ticket_ref(db, ref)
    if t is None or t["status"] != "approved":
        return None
    why = " ".join(str(why or "unknown").split())[:300]
    if why.startswith(CAPACITY_REFUSALS):  # the company's capacity, not the design: the ticket waits, uncounted
        set_status(db, t["id"], "approved", f"waiting: {why}")
        return "approved"
    since = _approved_at(db, t["id"])
    ev_key = f"company.ticket_design_failed:{t['id']}:{key}"[:160]
    if db.execute(text("SELECT 1 FROM society_events WHERE idempotency_key = :k"), {"k": ev_key}).first() is not None:
        return None  # this attempt is already counted
    emit_event(db, event_type=EventType.COMPANY_TICKET_DESIGN_FAILED, actor_type="system", subject_type="proposal", subject_id=t["proposal_id"],
               payload={"ticket_id": str(t["id"]), "proposal_id": str(t["proposal_id"]), "reason": why}, idempotency_key=ev_key)
    n = db.execute(text("SELECT COUNT(*) FROM society_events WHERE event_type = :t AND payload ->> 'ticket_id' = :id AND created_at >= :since"),
                   {"t": EventType.COMPANY_TICKET_DESIGN_FAILED, "id": str(t["id"]), "since": since or datetime(1970, 1, 1)}).scalar() or 0
    if n < DESIGN_FAILURES_TO_RETURN:
        set_status(db, t["id"], "approved", f"design failed ({n}/{DESIGN_FAILURES_TO_RETURN}): {why}")
        return "approved"
    reason = f"refused {n}x by REQUEST_CODE_CHANGE, back to proposed: {why}"
    set_status(db, t["id"], "proposed", reason, plan_id=None)
    emit_event(db, event_type=EventType.COMPANY_TICKET_RETURNED, actor_type="system", subject_type="proposal", subject_id=t["proposal_id"],
               payload={"ticket_id": str(t["id"]), "proposal_id": str(t["proposal_id"]), "reason": reason[:300], "design_failures": int(n)},
               idempotency_key=f"company.ticket_returned:{t['id']}:{since.isoformat() if since else 'never'}"[:160])
    return "proposed"


def note_intent_failure(db: Session, settings: Any, row: Any) -> None:
    """A failed REQUEST_CODE_CHANGE (live or resumed after approval): its error goes on the
    ticket. Bookkeeping never changes the intent's own outcome (savepoint, logged)."""
    if getattr(row, "intent_type", None) != "REQUEST_CODE_CHANGE" or not settings.company_cycle_enabled:
        return
    try:
        with db.begin_nested():
            record_design_failure(db, (row.payload or {}).get("proposal_id"), row.error or "", key=f"intent:{row.id}")
    except Exception:  # noqa: BLE001
        logger.exception("recording the ticket design failure of intent %s failed", row.id)


def sweep_stale_designs(db: Session, now: datetime) -> int:
    """An approved ticket with no candidate and no design activity (run) in its design story
    for ``DESIGN_STALE_AFTER`` counts one failed design attempt. The caller commits."""
    rows = db.execute(text(
        "SELECT DISTINCT ON (t.id) t.id, e.created_at AS approved_at, e.correlation_id AS corr FROM society_tickets t "
        "JOIN society_events e ON e.event_type = :ev AND e.payload ->> 'ticket_id' = t.id::text "
        "WHERE t.status = 'approved' AND t.candidate_id IS NULL ORDER BY t.id, e.created_at DESC"), {"ev": EventType.COMPANY_TICKET_APPROVED}).mappings().all()
    n = 0
    for r in rows:
        last = db.execute(text("SELECT MAX(created_at) FROM agent_runs WHERE correlation_id = :c"), {"c": r["corr"]}).scalar()
        quiet_since = max(x for x in (r["approved_at"], last) if x is not None)
        periods = int((now - quiet_since) / DESIGN_STALE_AFTER)  # one failed attempt per quiet period
        if periods >= 1:
            n += bool(record_design_failure(db, r["id"], f"no REQUEST_CODE_CHANGE within {DESIGN_STALE_AFTER.total_seconds() / 3600 * periods:g}h of approval",
                                            key=f"stale:{quiet_since.isoformat()}:{periods}"))
    return n


def decide_plan(db: Session, plan_id: uuid.UUID, *, approve: bool, user_id: Any) -> Dict[str, Any]:
    """Operator only (the API wires require_operator): no model path reaches here."""
    plan = db.execute(text("SELECT * FROM society_daily_plans WHERE id = :id FOR UPDATE"), {"id": plan_id}).mappings().first()
    if plan is None:
        raise KeyError("plan not found")
    if plan["status"] != "awaiting_owner":
        raise ValueError(f"plan is already {plan['status']}")
    status = "approved" if approve else "rejected"
    db.execute(text("UPDATE society_daily_plans SET status = :s, decided_by_user_id = :u, decided_at = NOW() WHERE id = :id"), {"s": status, "u": user_id, "id": plan_id})
    for t in db.execute(text("SELECT * FROM society_tickets WHERE plan_id = :p AND status = 'planned'"), {"p": plan_id}).mappings().all():
        if approve:
            _approved(db, dict(t), user_id)
        else:
            set_status(db, t["id"], "closed", "the owner rejected the daily plan")
    db.commit()
    return {**dict(plan), "status": status}


def set_objective_status(db: Session, objective_id: str, status: str, user_id: Any) -> None:
    if objective_id not in {o["id"] for o in charter.load()["objectives"]} or status not in charter.STATUSES:
        raise KeyError(f"unknown objective {objective_id!r} or status {status!r}")
    db.execute(text("INSERT INTO society_objective_status (objective_id, status, set_by_user_id) VALUES (:o, :s, :u) "
                    "ON CONFLICT (objective_id) DO UPDATE SET status = :s, set_by_user_id = :u, set_at = NOW()"), {"o": objective_id, "s": status, "u": user_id})
    db.commit()


def create_owner_ticket(db: Session, *, user_id: Any, title: str, problem: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """An owner task: a ticket (source owner) on an approved proposal; skips the plan, never the gate."""
    from ..models import ImprovementProposal  # noqa: PLC0415

    p = ImprovementProposal(id=uuid.uuid4(), source="human_feedback", title=title, problem=problem, proposed_change=problem, status="APPROVED",
                            target_scope="platform", importance=80)
    db.add(p)
    db.flush()
    tid = create(db, proposal_id=p.id, title=title, fields=fields, source="owner", role="governor", importance=80)
    _approved(db, for_proposal(db, p.id), user_id)
    db.commit()
    return {"ticket_id": str(tid), "proposal_id": str(p.id)}


def last_empty_reason(db: Session) -> Optional[str]:
    """Why the latest settled cycle produced no plan (None when it produced one)."""
    detail = db.execute(text("SELECT outcome_detail FROM society_company_cycles WHERE outcome IS NOT NULL ORDER BY created_at DESC LIMIT 1")).scalar()
    return (detail or {}).get("plan_empty_reason")


def plans_view(db: Session, limit: int = 14) -> List[Dict[str, Any]]:
    rows = db.execute(text("SELECT id, plan_date, status, ranking, decided_at, created_at FROM society_daily_plans ORDER BY created_at DESC LIMIT :n"), {"n": limit}).mappings().all()
    return [{k: (str(v) if isinstance(v, (uuid.UUID, date)) else v) for k, v in r.items()} for r in rows]


def _subject(item: Dict[str, Any]) -> Optional[str]:
    """The stable thing a backlog item is about (its key also carries the report/case/update)."""
    src = item.get("source")
    if src == "bench":
        return f"bench:{item['task_id']}"
    if src == "maintenance_incident" and "core_journey" in str(item.get("sli_ref") or ""):
        return f"probe:core_journey_success_rate:{item['incident_id']}"
    if src == "github_issue" and item.get("objectives"):
        return f"issue:{item['issue_number']}"
    return None  # not a ticket: no objective owns it


def supply(db: Session, items: List[Dict[str, Any]], now: datetime) -> List[str]:
    """Every open backlog item an objective owns becomes ONE proposed ticket (no model):
    proof = the failing bench task / probe / issue, expected effect = the KR gap shared
    by that KR's items, department from the charter. Idempotent per backlog key, and
    never a second open ticket for the same subject. The caller commits."""
    from ..models import ImprovementProposal  # noqa: PLC0415

    objs = charter.objectives(db)
    mapped = []
    for item in items:
        subject = _subject(item)
        dept, metric = SUPPLY.get(str(item.get("source")), (None, None))
        obj = next((o for o in objs if (metric and metric in [k["metric_id"] for k in o["key_results"]]) or o["id"] in (item.get("objectives") or [])), None)
        if subject and obj:
            kr = next(k for k in obj["key_results"] if k["metric_id"] == metric) if metric else obj["key_results"][0]
            mapped.append((item, subject, dept, obj, kr))
    created = []
    for item, subject, dept, obj, kr in mapped:
        tid = uuid.uuid5(_NS, f"ticket:{item['key']}")
        proof = [subject.rsplit(":", 1)[0] if subject.startswith("probe:") else subject]
        if db.execute(text("SELECT 1 FROM society_tickets WHERE id = :id OR (proof = CAST(:p AS JSONB) AND status IN :open)").bindparams(
                bindparam("open", expanding=True)), {"id": tid, "p": json.dumps(proof), "open": list(OPEN_TICKET)}).first():
            continue
        current = metrics.read(db, kr["metric_id"], now)
        start = current if current is not None else (kr.get("baseline") or 0)
        share = sum(1 for m in mapped if m[4]["metric_id"] == kr["metric_id"])
        effect = round(abs(float(kr["target"]) - float(start)) / share, 4)
        if effect <= 0:
            continue  # the key result is already met: nothing to buy
        title = {"bench": f"Builder harness: deliver dev task {item.get('task_id')} ({item.get('delivered')}/{item.get('runs')} runs)",
                 "maintenance_incident": f"Core journey: incident {str(item.get('incident_id'))[:8]} ({item.get('failure_class')})",
                 "github_issue": f"Owner issue #{item.get('issue_number')}"}[item["source"]]
        pid = uuid.uuid5(_NS, f"proposal:{item['key']}")
        db.add(ImprovementProposal(id=pid, source="audit", title=title[:255], problem=f"backlog {item['key']} ({item.get('failure_class')})",
                                   proposed_change=f"move {kr['metric_id']} {kr['direction']} by {effect:g}; proof {proof[0]}", status="PROPOSED",
                                   target_scope="platform", importance=60))
        db.flush()
        db.execute(text("INSERT INTO society_tickets (id, proposal_id, title, objective_id, metric_id, expected_effect, direction, proof, source, priority, department) "
                        "VALUES (:id, :p, :t, :o, :m, :e, :d, CAST(:proof AS JSONB), 'backlog', :pr, :dep)"),
                   {"id": tid, "p": pid, "t": title[:255], "o": obj["id"], "m": kr["metric_id"], "e": effect, "d": kr["direction"],
                    "proof": json.dumps(proof), "pr": 6 - int(obj["owner_priority"]), "dep": dept})
        created.append(str(tid))
    return created
