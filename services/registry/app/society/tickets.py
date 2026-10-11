"""Tickets: the unit of company work and the meaning gate (deterministic, no model).

A ticket rides on one ImprovementProposal and says why the work exists: an
ACTIVE charter objective, one of its key-result metrics (number + direction)
and a proof (acceptance test node ids, or ``probe:<metric_id>``). In company
mode no candidate is created without one that passes ``check``; a proof that
already passes on the base revision closes it ``already_satisfied``.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from . import charter, metrics
from .events import EventType, emit_event

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


def build_plan(db: Session, cycle_id: Optional[uuid.UUID], now: datetime) -> Optional[Dict[str, Any]]:
    """Rank the proposed tickets of ACTIVE objectives (<= 2 per department) into
    one plan of <= 3 that awaits the owner. Nothing in it builds until approved."""
    active = {o["id"]: o for o in charter.objectives(db) if o["status"] == "active"}
    rows = db.execute(text("SELECT * FROM society_tickets WHERE status = 'proposed' AND plan_id IS NULL AND source NOT IN :bypass "
                           "AND created_at >= :since ORDER BY created_at").bindparams(bindparam("bypass", expanding=True)),
                      {"bypass": list(BYPASS_SOURCES), "since": now - timedelta(days=7)}).mappings().all()
    ranked = sorted(((score(dict(r), active[r["objective_id"]]), dict(r)) for r in rows if r["objective_id"] in active), key=lambda x: -x[0])
    chosen, per_dep = [], {}
    for sc, t in ranked:
        if sc > 0 and per_dep.get(t["department"], 0) < PER_DEPARTMENT and len(chosen) < PLAN_MAX:
            per_dep[t["department"]] = per_dep.get(t["department"], 0) + 1
            chosen.append({"ticket_id": str(t["id"]), "title": t["title"], "objective_id": t["objective_id"], "metric_id": t["metric_id"],
                           "department": t["department"], "score": sc})
    if not chosen:
        if not active:
            why = "no active objective (activate one: company:activate:O<n>)"
        elif not ranked:
            why = f"no active objective has evidence: no proposed ticket for {sorted(active)} (backlog empty or mapped elsewhere)"
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
    emit_event(db, event_type=EventType.COMPANY_TICKET_APPROVED, actor_type="operator", actor_id=user_id, subject_type="proposal", subject_id=t["proposal_id"],
               payload={"ticket_id": str(t["id"]), "proposal_id": str(t["proposal_id"]), "title": t["title"], "objective_id": t["objective_id"],
                        "metric_id": t["metric_id"], "expected_effect": float(t["expected_effect"]), "direction": t["direction"], "proof": t["proof"]},
               idempotency_key=f"company.ticket_approved:{t['id']}")


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
