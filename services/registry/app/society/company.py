"""Autonomous company mode on the ONE control plane (ADR-0009 D15).

There is no second loop and no new authority:

* **Cadence** — at most one scheduled ``company.cycle`` event per UTC date
  (UNIQUE partial index), plus operator-invoked immediate cycles. The event
  carries the **Observe** step: a bounded, aggregate evidence bundle (counts
  and rates only, never private content).
* **Roles** — the event wakes the existing Governor (Product/Strategy) and
  Scout (Research / Customer insight / Growth). Engineering, QA, Security,
  Evaluation (SRE, Finance) follow through the existing event chain:
  Diagnose -> Prioritize -> Build -> QA/Security -> Promote -> Evaluate -> Learn.
* **Portfolio caps** — enforced by the executor while company mode is on
  (``SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES`` /
  ``SOCIETY_COMPANY_MAX_HIGH_RISK_INVESTIGATIONS``); no quota of commits,
  PRs or features exists, and ``no_high_value_change`` is a recorded,
  valid outcome.
* **Incident freeze** — an open incident freezes autonomous MERGE
  authority (``promotion.merge_freeze_reasons``). Only an operator lifts it.
* **Kill switch** — ``SOCIETY_RUNTIME_ENABLED=false`` stops cycles with
  everything else; the marketplace and A2A API keep running.

Production authority is untouched: nothing here deploys, merges on its own
authority, holds a Railway/Cloudflare/GitHub-bypass credential or bills.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    AgentIntent,
    AgentRun,
    AgentRunStatus,
    CodeCandidate,
    CodeCandidateStatus,
    CodePromotion,
    CompanyCycle,
    ImprovementProposal,
    IncidentFreeze,
    IntentExecutionStatus,
    RiskTier,
    SocietyEvent,
    TaskSession,
    User,
)
from .config import SocietySettings
from .context import TXT_LONG
from .events import emit_event, utcnow

COMPANY_CYCLE_EVENT = "company.cycle"
INCIDENT_OPENED_EVENT = "incident.opened"
INCIDENT_LIFTED_EVENT = "incident.lifted"
SETTLE_AFTER = timedelta(minutes=30)

#: Company functions mapped onto the EXISTING roles (no new agents, no new authority).
FUNCTION_ROLE_MAP = {
    "product_strategy": "governor",
    "research": "scout",
    "customer_insight": "scout",
    "growth": "scout",
    "engineering": "architect+builder",
    "qa": "qa",
    "security": "security",
    "sre_reliability": "evaluator",
    "finance_economics": "evaluator",
}
#: Intent types whose execution means a cycle produced a real change proposal.
_CHANGE_INTENTS = {"CREATE_IMPROVEMENT", "REQUEST_CODE_CHANGE", "CREATE_GOAL", "REQUEST_PR_PROMOTION"}


def _counts(rows) -> Dict[str, int]:
    return {str(getattr(k, "value", k)): int(v) for k, v in rows}


def _since(now: datetime, hours: int = 24) -> datetime:
    return now - timedelta(hours=hours)


def evidence_bundle(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The Observe step: aggregates only (no ids of users, no content)."""
    from ..a2a.orm import A2AAuditLog, A2AOutboundCall, A2ARemoteAgent, A2ATask

    now = now or utcnow()
    day = _since(now)
    naive_day = day.replace(tzinfo=None)  # legacy tables use naive UTC timestamps
    tasks = _counts(db.query(TaskSession.status, func.count(TaskSession.id)).filter(TaskSession.created_at >= naive_day).group_by(TaskSession.status).all())
    a2a_tasks = _counts(db.query(A2ATask.state, func.count(A2ATask.id)).filter(A2ATask.created_at >= day).group_by(A2ATask.state).all())
    a2a_audit = _counts(db.query(A2AAuditLog.result, func.count(A2AAuditLog.id)).filter(A2AAuditLog.created_at >= day).group_by(A2AAuditLog.result).all())
    remote = _counts(db.query(A2ARemoteAgent.state, func.count(A2ARemoteAgent.id)).group_by(A2ARemoteAgent.state).all())
    outbound = _counts(db.query(A2AOutboundCall.status, func.count(A2AOutboundCall.id)).filter(A2AOutboundCall.created_at >= day).group_by(A2AOutboundCall.status).all())
    agents = _counts(db.query(Agent.status, func.count(Agent.id)).group_by(Agent.status).all())
    new_agents = db.query(func.count(Agent.id)).filter(Agent.created_at >= naive_day).scalar() or 0
    signups = db.query(func.count(User.id)).filter(User.created_at >= naive_day).scalar() or 0
    verified = db.query(func.count(User.id)).filter(User.created_at >= naive_day, User.is_email_verified.is_(True)).scalar() or 0
    denied = db.query(func.count(AgentIntent.id)).filter(AgentIntent.created_at >= day, AgentIntent.execution_status == IntentExecutionStatus.DENIED).scalar() or 0
    finished = sum(v for k, v in tasks.items() if k in ("completed", "failed", "timeout", "refunded"))
    a2a_done = sum(a2a_tasks.get(k, 0) for k in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"))
    return {
        "window": {"start": day.isoformat(), "end": now.isoformat()},
        "marketplace": {"tasks_by_status": tasks, "agents_by_status": agents, "new_agents": int(new_agents)},
        "task_success_rate": round(tasks.get("completed", 0) / finished, 4) if finished else None,
        "a2a": {
            "inbound_tasks_by_state": a2a_tasks,
            "inbound_completion_rate": round(a2a_tasks.get("TASK_STATE_COMPLETED", 0) / a2a_done, 4) if a2a_done else None,
            "inbound_audit_results": a2a_audit,
            "remote_agents_by_state": remote,
            "outbound_calls_by_status": outbound,
        },
        "signup_funnel": {"signups": int(signups), "verified": int(verified)},
        "security": {"society_intents_denied": int(denied), "open_incidents": open_incident_count(db)},
    }


def a2a_fitness(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Product/A2A fitness dimensions read by the Evaluator. Never optimizes
    raw network size or spend -- only success and health ratios."""
    a2a = evidence.get("a2a", {})
    remote = a2a.get("remote_agents_by_state", {})
    total_remote = sum(remote.values())
    outbound = a2a.get("outbound_calls_by_status", {})
    ob_done = sum(outbound.get(k, 0) for k in ("succeeded", "failed", "timeout", "refused"))
    return {
        "inbound_interop_success": a2a.get("inbound_completion_rate"),
        "federated_task_success": round(outbound.get("succeeded", 0) / ob_done, 4) if ob_done else None,
        "healthy_remote_agent_ratio": round((remote.get("verified", 0) + remote.get("discovered", 0)) / total_remote, 4) if total_remote else None,
        "quarantined_or_blocked": remote.get("quarantined", 0) + remote.get("blocked", 0),
        "inbound_refusals": sum(v for k, v in a2a.get("inbound_audit_results", {}).items() if k in ("rejected",)),
        "marketplace_task_success": evidence.get("task_success_rate"),
    }


def _portfolio(db: Session, settings: SocietySettings) -> Dict[str, Any]:
    from .executor import _OPEN_PROPOSAL_STATUSES

    active = db.query(func.count(ImprovementProposal.id)).filter(ImprovementProposal.proposed_by_agent_id.isnot(None), ImprovementProposal.status.in_(_OPEN_PROPOSAL_STATUSES)).scalar() or 0
    closed = [CodeCandidateStatus.READY, CodeCandidateStatus.REJECTED, CodeCandidateStatus.FAILED, CodeCandidateStatus.ABANDONED]
    red = db.query(func.count(CodeCandidate.id)).filter(CodeCandidate.risk_tier == RiskTier.RED.value, CodeCandidate.status.notin_(closed)).scalar() or 0
    return {
        "active_hypotheses": int(active),
        "max_active_hypotheses": settings.company_max_active_hypotheses,
        "high_risk_investigations": int(red),
        "max_high_risk_investigations": settings.company_max_high_risk_investigations,
        "full": active >= settings.company_max_active_hypotheses,
    }


#: What the Governor and Scout are told a cycle is for.
CYCLE_INSTRUCTIONS = (
    "Observe the evidence, diagnose, and prioritize at most one high-value change. "
    "'No high-value change' is a valid outcome: do not create work to look busy."
)


def cycle_event_payload(cycle_id: uuid.UUID, trigger: str, evidence: Dict[str, Any], portfolio: Dict[str, Any]) -> Dict[str, Any]:
    """The ``company.cycle`` event payload: the Observe step, as ONE object.

    The context builder shows an event payload to the model only while its
    canonical JSON fits ``context.TXT_LONG``; past that it becomes a string
    preview that is cut mid-structure and drops the keys that sort last
    (``instructions``, ``portfolio``, ``trigger``). Everything here is
    therefore bounded: counts over closed status sets, two timestamps and
    fixed text. The static function->role map is NOT repeated per cycle (the
    roles already know their missions; the operator status still shows it).
    If the counts ever grow past the limit, ``fitness`` goes first: it is
    derived from ``evidence`` (the Evaluator recomputes it), and the payload
    says it was omitted. ``tests/society/test_company_cycle_context.py`` pins
    the worst case."""
    payload: Dict[str, Any] = {
        "cycle_id": str(cycle_id),
        "trigger": trigger,
        "evidence": evidence,
        "fitness": a2a_fitness(evidence),
        "portfolio": portfolio,
        "instructions": CYCLE_INSTRUCTIONS,
    }
    if _canonical_size(payload) > TXT_LONG:
        del payload["fitness"]
        payload["omitted"] = ["fitness"]
    return payload


def _canonical_size(obj: Any) -> int:
    """Length of the canonical JSON the context builder measures."""
    return len(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False))


def start_cycle(db: Session, settings: SocietySettings, *, trigger: str, now: Optional[datetime] = None, operator_id: Optional[uuid.UUID] = None) -> Optional[CompanyCycle]:
    """Create a cycle + its ``company.cycle`` event. A scheduled cycle is
    idempotent per UTC date (returns None when today's already exists)."""
    if trigger not in ("scheduled", "operator"):
        raise ValueError("trigger must be scheduled or operator")
    now = now or utcnow()
    cycle_day: date = now.date()
    evidence = evidence_bundle(db, now)
    cycle = CompanyCycle(id=uuid.uuid4(), cycle_date=cycle_day, trigger=trigger, evidence=evidence)
    try:
        with db.begin_nested():
            db.add(cycle)
            db.flush()
    except IntegrityError:
        return None  # today's scheduled cycle already exists
    event = emit_event(
        db,
        event_type=COMPANY_CYCLE_EVENT,
        payload=cycle_event_payload(cycle.id, trigger, evidence, _portfolio(db, settings)),
        actor_type="operator" if trigger == "operator" else "system",
        actor_id=operator_id,
        subject_type="company_cycle",
        subject_id=cycle.id,
        idempotency_key=f"company.cycle:{cycle.id}",
    )
    cycle.event_id = event.id
    db.commit()
    return cycle


def maybe_start_scheduled_cycle(db: Session, settings: SocietySettings, now: Optional[datetime] = None) -> Optional[CompanyCycle]:
    now = now or utcnow()
    if not (settings.runtime_enabled and settings.company_cycle_enabled):
        return None
    if now.hour < settings.company_cycle_hour_utc:
        return None
    exists = db.query(CompanyCycle.id).filter(CompanyCycle.cycle_date == now.date(), CompanyCycle.trigger == "scheduled").first()
    if exists:
        return None
    return start_cycle(db, settings, trigger="scheduled", now=now)


def settle_cycles(db: Session, now: Optional[datetime] = None) -> int:
    """Record outcomes of cycles whose work has finished."""
    now = now or utcnow()
    settled = 0
    for cycle in db.query(CompanyCycle).filter(CompanyCycle.outcome.is_(None), CompanyCycle.created_at <= now - SETTLE_AFTER).limit(20).all():
        event = db.query(SocietyEvent).filter(SocietyEvent.id == cycle.event_id).first() if cycle.event_id else None
        if event is None:
            cycle.outcome, cycle.outcome_detail = "no_high_value_change", {"reason": "no event"}
            settled += 1
            continue
        open_runs = db.query(func.count(AgentRun.id)).filter(
            AgentRun.correlation_id == event.correlation_id,
            AgentRun.status.in_([AgentRunStatus.QUEUED, AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING, AgentRunStatus.FAILED]),
        ).scalar() or 0
        if open_runs:
            continue
        executed = _counts(
            db.query(AgentIntent.intent_type, func.count(AgentIntent.id))
            .join(AgentRun, AgentIntent.run_id == AgentRun.id)
            .filter(AgentRun.correlation_id == event.correlation_id, AgentIntent.execution_status == IntentExecutionStatus.EXECUTED)
            .group_by(AgentIntent.intent_type)
            .all()
        )
        runs = db.query(func.count(AgentRun.id)).filter(AgentRun.correlation_id == event.correlation_id).scalar() or 0
        changed = sum(v for k, v in executed.items() if k in _CHANGE_INTENTS)
        cycle.outcome = "changes_proposed" if changed else "no_high_value_change"
        cycle.outcome_detail = {"runs": int(runs), "intents_executed": executed}
        settled += 1
    if settled:
        db.commit()
    return settled


# ── incident freeze (operator-only lift) ───────────────────────────────────


def open_incident_count(db: Session) -> int:
    return int(db.query(func.count(IncidentFreeze.id)).filter(IncidentFreeze.lifted_at.is_(None)).scalar() or 0)


def open_incident(db: Session, *, reason: str, source: str, evidence: Optional[Dict[str, Any]] = None, operator_id: Optional[uuid.UUID] = None) -> IncidentFreeze:
    inc = IncidentFreeze(id=uuid.uuid4(), reason=reason[:255], source=source[:64], evidence=evidence or {}, opened_by_user_id=operator_id)
    db.add(inc)
    db.flush()
    emit_event(db, event_type=INCIDENT_OPENED_EVENT, payload={"incident_id": str(inc.id), "reason": inc.reason, "source": inc.source}, actor_type="operator" if operator_id else "system", actor_id=operator_id, subject_type="incident", subject_id=inc.id, idempotency_key=f"incident.opened:{inc.id}")
    db.commit()
    return inc


def lift_incident(db: Session, incident_id: uuid.UUID, *, operator: User, reason: str) -> IncidentFreeze:
    """Only an operator lifts a freeze (the API wires ``require_operator``);
    no model, intent or external agent has a path here."""
    inc = db.query(IncidentFreeze).filter(IncidentFreeze.id == incident_id).with_for_update().first()
    if inc is None:
        db.rollback()
        raise KeyError("incident not found")
    if inc.lifted_at is None:
        inc.lifted_at = datetime.now(timezone.utc)
        inc.lifted_by_user_id = operator.id
        inc.lift_reason = reason[:255]
        emit_event(db, event_type=INCIDENT_LIFTED_EVENT, payload={"incident_id": str(inc.id), "reason": inc.lift_reason}, actor_type="operator", actor_id=operator.id, subject_type="incident", subject_id=inc.id, idempotency_key=f"incident.lifted:{inc.id}")
    db.commit()
    return inc


def incident_view(inc: IncidentFreeze) -> Dict[str, Any]:
    return {
        "id": str(inc.id),
        "reason": inc.reason,
        "source": inc.source,
        "openedAt": inc.opened_at.isoformat() if inc.opened_at else None,
        "liftedAt": inc.lifted_at.isoformat() if inc.lifted_at else None,
        "liftReason": inc.lift_reason,
    }


def cycle_view(cycle: CompanyCycle) -> Dict[str, Any]:
    return {
        "id": str(cycle.id),
        "date": cycle.cycle_date.isoformat(),
        "trigger": cycle.trigger,
        "eventId": str(cycle.event_id) if cycle.event_id else None,
        "outcome": cycle.outcome,
        "outcomeDetail": cycle.outcome_detail,
        "createdAt": cycle.created_at.isoformat() if cycle.created_at else None,
    }


# ── operator status report ─────────────────────────────────────────────────


def status_report(db: Session, settings: SocietySettings) -> Dict[str, Any]:
    """Everything an operator needs in one view. No secrets, no payloads."""
    from ..a2a import config as a2a_config
    from ..a2a.orm import A2AOutboundCall
    from .policy import spend_today_usd

    evidence = evidence_bundle(db)
    cycles = db.query(CompanyCycle).order_by(CompanyCycle.created_at.desc()).limit(7).all()
    candidates = _counts(db.query(CodeCandidate.status, func.count(CodeCandidate.id)).group_by(CodeCandidate.status).all())
    promotions = _counts(db.query(CodePromotion.status, func.count(CodePromotion.id)).group_by(CodePromotion.status).all())
    incidents = db.query(IncidentFreeze).filter(IncidentFreeze.lifted_at.is_(None)).order_by(IncidentFreeze.opened_at.desc()).limit(20).all()
    society_calls = db.query(func.count(A2AOutboundCall.id)).filter(A2AOutboundCall.initiator_class == "society", A2AOutboundCall.created_at >= _since(utcnow())).scalar() or 0
    spend = spend_today_usd(db)
    return {
        "mode": {
            "society_runtime_enabled": settings.runtime_enabled,
            "company_cycle_enabled": settings.company_cycle_enabled,
            "autonomous_code_enabled": settings.autonomous_code_enabled,
            "auto_merge_enabled": settings.auto_merge_enabled,
            "production_deploy_enabled": False,  # hard OFF; production releases are the trusted operator boundary
            "a2a_server_enabled": a2a_config.server_enabled(),
            "a2a_federation_enabled": a2a_config.federation_enabled(),
            "a2a_society_client_enabled": a2a_config.society_client_enabled(),
        },
        "cycles": [cycle_view(c) for c in cycles],
        "portfolio": _portfolio(db, settings),
        "evidence": evidence,
        "fitness": a2a_fitness(evidence),
        "candidates_by_status": candidates,
        "release_ready_candidates": candidates.get("ready", 0),
        "promotions_by_status": promotions,
        "budgets": {
            "model_spend_today_usd": str(spend),
            "daily_model_budget_usd": str(settings.daily_model_budget_usd),
            "society_a2a_calls_24h": int(society_calls),
            "society_a2a_calls_per_day_cap": settings.a2a_max_calls_per_day,
        },
        "incidents": [incident_view(i) for i in incidents],
        "function_roles": FUNCTION_ROLE_MAP,
        "public_surface": _public_surface_view(db, settings),
    }


def _public_surface_view(db: Session, settings: SocietySettings) -> Dict[str, Any]:
    """Public-surface health as the Society sees it: the monitor's settings,
    the open anomaly (structural), recent anomaly/recovery events and the
    Society's workstream on the newest anomaly. No page content, no secrets."""
    from .surface_monitor import self_healing_workstream, surface_status

    view = surface_status(db)
    latest = (view.get("open_anomaly") or {}).get("correlation_id")
    if latest is None:
        recent = [e for e in view.get("recent_events", []) if e.get("type") == "public.surface.anomaly"]
        if recent:
            ev = db.query(SocietyEvent).filter(SocietyEvent.id == uuid.UUID(recent[0]["event_id"])).first()
            latest = str(ev.correlation_id) if ev is not None and ev.correlation_id else None
    view["monitor"] = {
        "enabled": settings.public_surface_monitor_enabled,
        "interval_seconds": settings.public_surface_monitor_interval_seconds,
        "failure_threshold": settings.public_surface_failure_threshold,
        "cooldown_seconds": settings.public_surface_cooldown_seconds,
        "target": settings.public_surface_target_label,
        "ui_origin": settings.public_product_ui_origin,
        "api_origin": settings.public_product_api_origin,
    }
    view["workstream"] = self_healing_workstream(db, uuid.UUID(latest)) if latest else {}
    return view


def main() -> None:  # pragma: no cover - CLI
    from ..database import SessionLocal
    from .config import get_settings

    parser = argparse.ArgumentParser(prog="python -m app.society.company")
    parser.add_argument("command", choices=["status", "cycle"])
    args = parser.parse_args()
    db = SessionLocal()
    try:
        settings = get_settings()
        if args.command == "status":
            sys.stdout.write(json.dumps(status_report(db, settings), indent=2, default=str) + "\n")
        else:
            if not settings.runtime_enabled:
                raise SystemExit("SOCIETY_RUNTIME_ENABLED is off: the kill switch is engaged")
            cycle = start_cycle(db, settings, trigger="operator")
            sys.stdout.write((json.dumps(cycle_view(cycle), indent=2) if cycle else "no cycle created") + "\n")
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "COMPANY_CYCLE_EVENT",
    "FUNCTION_ROLE_MAP",
    "evidence_bundle",
    "a2a_fitness",
    "cycle_event_payload",
    "start_cycle",
    "maybe_start_scheduled_cycle",
    "settle_cycles",
    "open_incident",
    "lift_incident",
    "open_incident_count",
    "status_report",
]
