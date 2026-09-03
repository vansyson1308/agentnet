"""Society runtime API — a server-enforced split between PUBLIC and OPERATOR surfaces.

PUBLIC (no auth) returns only sanitised aggregates and structure:
  GET /society/status, /society/metrics, /society/story/{correlation}, /society/candidates
  — never payloads, context summaries, decision text, memory, messages,
    task input, candidate content, wallet details, paths, provider endpoints
    or error bodies.

OPERATOR (``users.society_role = operator``, user JWT only — see
``society/operator_auth.py``): everything detailed —
  GET  /society/config, /events, /runs, /runs/{id}, /intents, /candidates/{id},
       /story/{correlation}/detail, /budget, /approvals, /operators, /ask
  POST /society/intents/{id}/approve | /reject, /operators

WORLD-EVENT INGRESS (``operator`` or ``event_producer``):
  POST /society/events — allow-listed event types only, bounded payload,
  per-actor + global rate limits, idempotency key support.

Every answer is read from persisted state; nothing is synthesised.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...database import get_db
from ...models import (
    Agent,
    AgentCapabilityGrant,
    AgentIntent,
    AgentRun,
    AgentRunStatus,
    CodeCandidate,
    CodeCandidateStatus,
    Goal,
    GoalStatus,
    ImprovementProposal,
    IntentApproval,
    IntentExecutionStatus,
    PolicyDecision,
    ProposalStatus,
    SocietyEvent,
    SocietyEventStatus,
    SocietyUserRole,
    User,
    Wallet,
    WalletOwnerType,
)
from ...society import approvals as approvals_mod
from ...society.config import get_settings
from ...society.events import EventType, emit_event
from ...society.operator_auth import assign_role, require_event_producer, require_operator, user_society_role
from ...society.roles import load_role_definitions, role_as_dict

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/society", tags=["society"])

_RESERVED_INJECT_PREFIXES = ("intent.", "run.", "loop_breaker.", "proposal.", "code_candidate.", "code_change.", "task.", "offer.", "goal.", "memory.", "agent.", "society.", "staging_deploy.")


def _ev(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# ── serialisers: PUBLIC (sanitised) ──────────────────────────────────


def _event_public(e: SocietyEvent) -> Dict[str, Any]:
    return {
        "id": str(e.id),
        "event_type": e.event_type,
        "status": _ev(e.status),
        "actor_type": e.actor_type,
        "causation_depth": e.causation_depth,
        "causation_id": str(e.causation_id) if e.causation_id else None,
        "source_run_id": str(e.source_run_id) if e.source_run_id else None,
        "created_at": _iso(e.created_at),
        "processed_at": _iso(e.processed_at),
    }


def _intent_public(i: AgentIntent) -> Dict[str, Any]:
    return {
        "id": str(i.id),
        "seq": i.seq,
        "intent_type": i.intent_type,
        "risk_class": _ev(i.risk_class),
        "policy_decision": _ev(i.policy_decision),
        "execution_status": _ev(i.execution_status),
    }


def _run_public(r: AgentRun, event_type: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": str(r.id),
        "role": r.role,
        "event_type": event_type,
        "status": _ev(r.status),
        "attempt": r.attempt,
        "model_provider": r.model_provider,
        "intents_count": r.intents_count,
        "started_at": _iso(r.started_at),
        "completed_at": _iso(r.completed_at),
    }


def _candidate_public(c: CodeCandidate) -> Dict[str, Any]:
    return {
        "id": str(c.id),
        "status": _ev(c.status),
        "branch_name": c.branch_name,
        "qa_verdict": (c.qa_report or {}).get("verdict"),
        "security_verdict": (c.security_report or {}).get("verdict"),
        "requires_security_review": bool(c.requires_security_review),
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
    }


# ── serialisers: OPERATOR (full) ─────────────────────────────────────


def _event_out(e: SocietyEvent) -> Dict[str, Any]:
    d = _event_public(e)
    d.update(
        {
            "actor_id": str(e.actor_id) if e.actor_id else None,
            "subject_type": e.subject_type,
            "subject_id": str(e.subject_id) if e.subject_id else None,
            "correlation_id": str(e.correlation_id),
            "payload": e.payload,
            "dispatch_note": e.dispatch_note,
        }
    )
    return d


def _run_out(r: AgentRun, agent_name: Optional[str] = None, event_type: Optional[str] = None) -> Dict[str, Any]:
    d = _run_public(r, event_type)
    d.update(
        {
            "agent_id": str(r.agent_id),
            "agent_name": agent_name,
            "event_id": str(r.event_id),
            "worker_id": r.worker_id,
            "model_name": r.model_name,
            "decision_summary": r.decision_summary,
            "cost_usd": str(r.cost_usd or 0),
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "model_requests": r.model_requests,
            "model_retries": r.model_retries,
            "model_timeouts": r.model_timeouts,
            "error": r.error,
            "correlation_id": str(r.correlation_id),
            "trace_id": str(r.trace_id) if r.trace_id else None,
            "context_digest": r.context_digest,
            "sleep_until": _iso(r.sleep_until),
        }
    )
    return d


def _intent_out(i: AgentIntent) -> Dict[str, Any]:
    d = _intent_public(i)
    d.update(
        {
            "run_id": str(i.run_id),
            "agent_id": str(i.agent_id),
            "policy_reason": i.policy_reason,
            "result": i.result,
            "error": i.error,
            "payload": i.payload,
            "created_at": _iso(i.created_at),
            "executed_at": _iso(i.executed_at),
            "resume_attempt": i.resume_attempt,
        }
    )
    return d


def _candidate_out(c: CodeCandidate) -> Dict[str, Any]:
    d = _candidate_public(c)
    d.update(
        {
            "title": c.title,
            "head_sha": c.head_sha,
            "base_sha": c.base_sha,
            "changed_files": c.changed_files,
            "diff_stat": c.diff_stat,
            "proposal_id": str(c.proposal_id) if c.proposal_id else None,
            "task_id": str(c.task_id) if c.task_id else None,
            "correlation_id": str(c.correlation_id),
            "qa": {"verdict": (c.qa_report or {}).get("verdict"), "summary": (c.qa_report or {}).get("summary"), "attempts": (c.qa_report or {}).get("attempts"), "failures": (c.qa_report or {}).get("failures")},
            "security": {"verdict": (c.security_report or {}).get("verdict"), "static_findings": (c.security_report or {}).get("static_findings")},
            "builder_agent_id": str(c.builder_agent_id) if c.builder_agent_id else None,
            "qa_agent_id": str(c.qa_agent_id) if c.qa_agent_id else None,
            "error": c.error,
        }
    )
    return d


def _approval_out(a: IntentApproval) -> Dict[str, Any]:
    return {
        "id": str(a.id),
        "intent_id": str(a.intent_id),
        "run_id": str(a.run_id),
        "agent_id": str(a.agent_id),
        "decided_by_user_id": str(a.decided_by_user_id) if a.decided_by_user_id else None,
        "decision": _ev(a.decision),
        "reason": a.reason,
        "original_policy_reason": a.original_policy_reason,
        "decided_at": _iso(a.decided_at),
        "resumed_at": _iso(a.resumed_at),
        "executed_at": _iso(a.executed_at),
        "final_state": a.final_state,
        "resume_error": a.resume_error,
    }


# ── PUBLIC ────────────────────────────────────────────────────────────


@router.get("/status")
def society_status(db: Session = Depends(get_db)):
    """Sanitised runtime status: flags, fleet roles, aggregate counts."""
    settings = get_settings()
    now = _now()
    hour_ago = now - timedelta(hours=1)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    fleet = db.query(AgentCapabilityGrant.role, AgentCapabilityGrant.enabled, AgentCapabilityGrant.paused_until).all()
    active = db.query(func.count(AgentRun.id)).filter(AgentRun.status.in_([AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING])).scalar() or 0
    last_run_at = db.query(func.max(AgentRun.completed_at)).scalar()
    return {
        **settings.public_flags(),
        "fleet": [{"role": role, "enabled": bool(enabled), "paused": bool(paused and paused > now)} for role, enabled, paused in fleet],
        "fleet_size": len(fleet),
        "pending_events": db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.status == SocietyEventStatus.PENDING).scalar() or 0,
        "queued_runs": db.query(func.count(AgentRun.id)).filter(AgentRun.status == AgentRunStatus.QUEUED).scalar() or 0,
        "active_runs": active,
        "runs_last_hour": db.query(func.count(AgentRun.id)).filter(AgentRun.started_at >= hour_ago).scalar() or 0,
        "runs_today": {
            s: (db.query(func.count(AgentRun.id)).filter(AgentRun.created_at >= day_start, AgentRun.status == s).scalar() or 0)
            for s in ("completed", "skipped", "dead", "queued")
        },
        "intents_awaiting_approval": db.query(func.count(AgentIntent.id)).filter(AgentIntent.execution_status == IntentExecutionStatus.AWAITING_APPROVAL).scalar() or 0,
        "candidates_by_status": {_ev(s): n for s, n in db.query(CodeCandidate.status, func.count(CodeCandidate.id)).group_by(CodeCandidate.status).all()},
        "last_run_completed_at": _iso(last_run_at),
    }


@router.get("/metrics")
def society_metrics(db: Session = Depends(get_db), hours: int = Query(24, ge=1, le=24 * 30)):
    """Aggregate counters only (no cost/token detail — see operator /budget)."""
    since = _now() - timedelta(hours=hours)
    runs = dict(db.query(AgentRun.status, func.count(AgentRun.id)).filter(AgentRun.created_at >= since).group_by(AgentRun.status).all())
    intents = dict(db.query(AgentIntent.policy_decision, func.count(AgentIntent.id)).filter(AgentIntent.created_at >= since).group_by(AgentIntent.policy_decision).all())
    executions = dict(db.query(AgentIntent.execution_status, func.count(AgentIntent.id)).filter(AgentIntent.created_at >= since).group_by(AgentIntent.execution_status).all())
    events = dict(db.query(SocietyEvent.status, func.count(SocietyEvent.id)).filter(SocietyEvent.created_at >= since).group_by(SocietyEvent.status).all())
    loop_breaks = db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED, SocietyEvent.created_at >= since).scalar() or 0
    suppressed = db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.created_at >= since, SocietyEvent.dispatch_note.like("loop breaker%")).scalar() or 0
    return {
        "window_hours": hours,
        "events": {_ev(k): v for k, v in events.items()},
        "pending_events": db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.status == SocietyEventStatus.PENDING).scalar() or 0,
        "runs": {_ev(k): v for k, v in runs.items()},
        "intents_by_policy": {_ev(k): v for k, v in intents.items()},
        "intents_by_execution": {_ev(k): v for k, v in executions.items()},
        "policy_denials": (intents.get(PolicyDecision.DENY, 0) or 0) + (intents.get(PolicyDecision.INVALID, 0) or 0),
        "loop_breaker_activations": loop_breaks,
        "events_suppressed_by_loop_breaker": suppressed,
        "pending_code_candidates": db.query(func.count(CodeCandidate.id)).filter(CodeCandidate.status.in_([CodeCandidateStatus.REQUESTED, CodeCandidateStatus.BUILDING, CodeCandidateStatus.BUILT, CodeCandidateStatus.QA_RUNNING, CodeCandidateStatus.SECURITY_REVIEW])).scalar() or 0,
    }


@router.get("/story/{correlation_id}")
def correlation_story_public(correlation_id: uuid.UUID, db: Session = Depends(get_db)):
    """Structural story: event types, roles, intent types + decisions. No content."""
    events = db.query(SocietyEvent).filter(SocietyEvent.correlation_id == correlation_id).order_by(SocietyEvent.created_at).all()
    runs = db.query(AgentRun, SocietyEvent.event_type).join(SocietyEvent, SocietyEvent.id == AgentRun.event_id).filter(AgentRun.correlation_id == correlation_id).order_by(AgentRun.created_at).all()
    run_ids = [r.id for r, _ in runs]
    intents = db.query(AgentIntent).filter(AgentIntent.run_id.in_(run_ids)).order_by(AgentIntent.created_at, AgentIntent.seq).all() if run_ids else []
    by_run: Dict[str, List[Dict[str, Any]]] = {}
    for i in intents:
        by_run.setdefault(str(i.run_id), []).append(_intent_public(i))
    candidates = db.query(CodeCandidate).filter(CodeCandidate.correlation_id == correlation_id).all()
    return {
        "correlation_id": str(correlation_id),
        "events": [_event_public(e) for e in events],
        "runs": [{**_run_public(r, et), "intents": by_run.get(str(r.id), [])} for r, et in runs],
        "candidates": [_candidate_public(c) for c in candidates],
    }


@router.get("/candidates")
def list_candidates_public(db: Session = Depends(get_db), status_filter: Optional[str] = Query(None, alias="status"), limit: int = Query(50, ge=1, le=200)):
    q = db.query(CodeCandidate)
    if status_filter:
        q = q.filter(CodeCandidate.status == status_filter)
    return [_candidate_public(c) for c in q.order_by(CodeCandidate.created_at.desc()).limit(limit).all()]


# ── OPERATOR ──────────────────────────────────────────────────────────


@router.get("/config")
def society_config(operator: User = Depends(require_operator)):
    settings = get_settings()
    return {"settings": settings.public_dict(), "roles": {k: role_as_dict(v) for k, v in load_role_definitions().items()}}


@router.get("/events")
def list_events(
    db: Session = Depends(get_db),
    operator: User = Depends(require_operator),
    correlation_id: Optional[uuid.UUID] = None,
    event_type: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
):
    q = db.query(SocietyEvent)
    if correlation_id:
        q = q.filter(SocietyEvent.correlation_id == correlation_id)
    if event_type:
        q = q.filter(SocietyEvent.event_type == event_type)
    if status_filter:
        q = q.filter(SocietyEvent.status == status_filter)
    rows = q.order_by(SocietyEvent.created_at.desc() if not correlation_id else SocietyEvent.created_at.asc()).limit(limit).all()
    return [_event_out(e) for e in rows]


@router.get("/runs")
def list_runs(
    db: Session = Depends(get_db),
    operator: User = Depends(require_operator),
    correlation_id: Optional[uuid.UUID] = None,
    agent_id: Optional[uuid.UUID] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
):
    q = db.query(AgentRun, Agent.name, SocietyEvent.event_type).join(Agent, Agent.id == AgentRun.agent_id).join(SocietyEvent, SocietyEvent.id == AgentRun.event_id)
    if correlation_id:
        q = q.filter(AgentRun.correlation_id == correlation_id)
    if agent_id:
        q = q.filter(AgentRun.agent_id == agent_id)
    if status_filter:
        q = q.filter(AgentRun.status == status_filter)
    rows = q.order_by(AgentRun.created_at.desc() if not correlation_id else AgentRun.created_at.asc()).limit(limit).all()
    return [_run_out(r, name, et) for r, name, et in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    row = db.query(AgentRun, Agent.name, SocietyEvent.event_type).join(Agent, Agent.id == AgentRun.agent_id).join(SocietyEvent, SocietyEvent.id == AgentRun.event_id).filter(AgentRun.id == run_id).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    r, name, et = row
    out = _run_out(r, name, et)
    out["context_summary"] = r.context_summary
    out["intents"] = [_intent_out(i) for i in db.query(AgentIntent).filter(AgentIntent.run_id == r.id).order_by(AgentIntent.seq).all()]
    return out


@router.get("/intents")
def list_intents(
    db: Session = Depends(get_db),
    operator: User = Depends(require_operator),
    run_id: Optional[uuid.UUID] = None,
    agent_id: Optional[uuid.UUID] = None,
    policy_decision: Optional[str] = None,
    execution_status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
):
    q = db.query(AgentIntent)
    if run_id:
        q = q.filter(AgentIntent.run_id == run_id)
    if agent_id:
        q = q.filter(AgentIntent.agent_id == agent_id)
    if policy_decision:
        q = q.filter(AgentIntent.policy_decision == policy_decision)
    if execution_status:
        q = q.filter(AgentIntent.execution_status == execution_status)
    return [_intent_out(i) for i in q.order_by(AgentIntent.created_at.desc()).limit(limit).all()]


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: uuid.UUID, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    c = db.query(CodeCandidate).filter(CodeCandidate.id == candidate_id).first()
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="candidate not found")
    out = _candidate_out(c)
    out["spec"] = c.spec
    out["qa_report"] = c.qa_report
    out["security_report"] = c.security_report
    out["workspace_path"] = c.workspace_path
    return out


@router.get("/story/{correlation_id}/detail")
def correlation_story_detail(correlation_id: uuid.UUID, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    events = db.query(SocietyEvent).filter(SocietyEvent.correlation_id == correlation_id).order_by(SocietyEvent.created_at).all()
    runs = db.query(AgentRun, Agent.name, SocietyEvent.event_type).join(Agent, Agent.id == AgentRun.agent_id).join(SocietyEvent, SocietyEvent.id == AgentRun.event_id).filter(AgentRun.correlation_id == correlation_id).order_by(AgentRun.created_at).all()
    run_ids = [r.id for r, _, _ in runs]
    intents = db.query(AgentIntent).filter(AgentIntent.run_id.in_(run_ids)).order_by(AgentIntent.created_at, AgentIntent.seq).all() if run_ids else []
    approvals = {str(a.intent_id): _approval_out(a) for a in db.query(IntentApproval).filter(IntentApproval.run_id.in_(run_ids)).all()} if run_ids else {}
    by_run: Dict[str, List[Dict[str, Any]]] = {}
    for i in intents:
        d = _intent_out(i)
        d["approval"] = approvals.get(str(i.id))
        by_run.setdefault(str(i.run_id), []).append(d)
    candidates = db.query(CodeCandidate).filter(CodeCandidate.correlation_id == correlation_id).all()
    return {
        "correlation_id": str(correlation_id),
        "events": [_event_out(e) for e in events],
        "runs": [{**_run_out(r, name, et), "intents": by_run.get(str(r.id), [])} for r, name, et in runs],
        "candidates": [_candidate_out(c) for c in candidates],
    }


@router.get("/budget")
def society_budget(db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    settings = get_settings()
    day_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    spend = db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0)).filter(AgentRun.created_at >= day_start).scalar() or 0
    per_agent = db.query(Agent.name, func.coalesce(func.sum(AgentRun.cost_usd), 0), func.count(AgentRun.id), func.coalesce(func.sum(AgentRun.model_requests), 0), func.coalesce(func.sum(AgentRun.model_retries), 0), func.coalesce(func.sum(AgentRun.model_timeouts), 0)).join(AgentRun, AgentRun.agent_id == Agent.id).filter(AgentRun.created_at >= day_start).group_by(Agent.name).all()
    tokens = db.query(func.coalesce(func.sum(AgentRun.tokens_in), 0), func.coalesce(func.sum(AgentRun.tokens_out), 0)).filter(AgentRun.created_at >= day_start).first()
    wallets = db.query(Agent.name, Wallet.balance_credits, Wallet.reserved_credits, Wallet.spending_cap).join(Wallet, (Wallet.owner_id == Agent.id) & (Wallet.owner_type == WalletOwnerType.AGENT)).join(AgentCapabilityGrant, AgentCapabilityGrant.agent_id == Agent.id).all()
    return {
        "model_spend_today_usd": str(spend),
        "daily_model_budget_usd": str(settings.daily_model_budget_usd),
        "remaining_usd": str(max(0, settings.daily_model_budget_usd - spend)),
        "tokens_in_today": int(tokens[0] or 0),
        "tokens_out_today": int(tokens[1] or 0),
        "by_agent": [{"agent": n, "spend_usd": str(c), "runs": r, "model_requests": int(q), "model_retries": int(rt), "model_timeouts": int(t)} for n, c, r, q, rt, t in per_agent],
        "wallets": [{"agent": n, "balance_credits": b, "reserved_credits": r, "spending_cap": cap} for n, b, r, cap in wallets],
        "max_task_escrow_credits": settings.max_task_escrow_credits,
    }


# ── approvals (operator) ──────────────────────────────────────────────


class DecisionBody(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


@router.get("/approvals")
def list_approvals(db: Session = Depends(get_db), operator: User = Depends(require_operator), include_decided: bool = False, limit: int = Query(100, ge=1, le=500)):
    pending = approvals_mod.pending_approvals(db, limit=limit)
    out = {
        "pending": [
            {**_intent_out(i), "agent_name": name, "correlation_id": str(corr)}
            for i, name, corr in pending
            if _ev(i.execution_status) == IntentExecutionStatus.AWAITING_APPROVAL.value
        ],
        "approved_waiting_resume": [
            {**_intent_out(i), "agent_name": name, "correlation_id": str(corr)}
            for i, name, corr in pending
            if _ev(i.execution_status) == IntentExecutionStatus.APPROVED.value
        ],
    }
    if include_decided:
        out["decided"] = [_approval_out(a) for a in db.query(IntentApproval).order_by(IntentApproval.decided_at.desc()).limit(limit).all()]
    return out


def _decide(db: Session, intent_id: uuid.UUID, operator: User, decision: str, body: Optional[DecisionBody]):
    try:
        res = approvals_mod.decide(db, intent_id=intent_id, user=operator, decision=decision, reason=body.reason if body else None)
    except approvals_mod.ApprovalNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")
    except approvals_mod.ApprovalConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return {"approval": _approval_out(res.approval), "intent": _intent_out(res.intent), "already_decided": res.already_decided}


@router.post("/intents/{intent_id}/approve")
def approve_intent(intent_id: uuid.UUID, body: Optional[DecisionBody] = None, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    return _decide(db, intent_id, operator, "approved", body)


@router.post("/intents/{intent_id}/reject")
def reject_intent(intent_id: uuid.UUID, body: Optional[DecisionBody] = None, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    return _decide(db, intent_id, operator, "rejected", body)


# ── operator management (operator) ────────────────────────────────────


class RoleBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    role: Optional[str] = Field(None, pattern=r"^(operator|event_producer)$")


@router.get("/operators")
def list_operators(db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    rows = db.query(User).filter(User.society_role.isnot(None)).all()
    return [{"user_id": str(u.id), "email": u.email, "role": u.society_role} for u in rows] + [
        {"user_id": None, "email": e, "role": SocietyUserRole.OPERATOR.value, "source": "bootstrap_env"}
        for e in sorted(__import__("services.registry.app.society.operator_auth", fromlist=["bootstrap_operator_emails"]).bootstrap_operator_emails())
        if not db.query(User.id).filter(User.email == e, User.society_role.isnot(None)).first()
    ]


@router.post("/operators")
def set_operator_role(body: RoleBody, db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    try:
        user = assign_role(db, email=body.email, role=body.role, actor=operator)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return {"user_id": str(user.id), "email": user.email, "role": user_society_role(user)}


# ── JARVIS (operator) ─────────────────────────────────────────────────


def _answer_goals(db: Session) -> Dict[str, Any]:
    goals = db.query(Goal).filter(Goal.status.in_([GoalStatus.ACTIVE, GoalStatus.PAUSED])).order_by(Goal.priority.desc(), Goal.created_at).limit(20).all()
    return {"active_goals": [{"id": str(g.id), "title": g.title, "owner": _ev(g.owner_type), "priority": _ev(g.priority), "status": _ev(g.status)} for g in goals]}


def _answer_working(db: Session) -> Dict[str, Any]:
    rows = db.query(AgentRun, Agent.name, SocietyEvent.event_type).join(Agent, Agent.id == AgentRun.agent_id).join(SocietyEvent, SocietyEvent.id == AgentRun.event_id).filter(AgentRun.status.in_([AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING])).all()
    return {"working_agents": [{"agent": name, "role": r.role, "event_type": et, "run_id": str(r.id), "since": _iso(r.started_at)} for r, name, et in rows]}


def _answer_recent(db: Session) -> Dict[str, Any]:
    rows = db.query(AgentRun, Agent.name, SocietyEvent.event_type).join(Agent, Agent.id == AgentRun.agent_id).join(SocietyEvent, SocietyEvent.id == AgentRun.event_id).filter(AgentRun.status.in_([AgentRunStatus.COMPLETED, AgentRunStatus.DEAD])).order_by(AgentRun.completed_at.desc()).limit(10).all()
    return {"recent_runs": [{"agent": name, "role": r.role, "event_type": et, "status": _ev(r.status), "decision": r.decision_summary, "at": _iso(r.completed_at), "run_id": str(r.id), "correlation_id": str(r.correlation_id)} for r, name, et in rows]}


def _answer_proposals(db: Session) -> Dict[str, Any]:
    rows = db.query(ImprovementProposal).filter(ImprovementProposal.status.in_([ProposalStatus.PROPOSED, ProposalStatus.UNDER_REVIEW, ProposalStatus.APPROVED])).order_by(ImprovementProposal.importance.desc()).limit(20).all()
    return {"pending_proposals": [{"id": str(p.id), "title": p.title, "status": _ev(p.status), "importance": p.importance} for p in rows]}


def _answer_blocked(db: Session) -> Dict[str, Any]:
    now = _now()
    paused = db.query(Agent.name, AgentCapabilityGrant.paused_until, AgentCapabilityGrant.consecutive_failures).join(AgentCapabilityGrant, AgentCapabilityGrant.agent_id == Agent.id).filter(AgentCapabilityGrant.paused_until > now).all()
    awaiting = db.query(AgentIntent, Agent.name).join(Agent, Agent.id == AgentIntent.agent_id).filter(AgentIntent.execution_status.in_([IntentExecutionStatus.AWAITING_APPROVAL, IntentExecutionStatus.APPROVED])).limit(20).all()
    dead = db.query(AgentRun, Agent.name).join(Agent, Agent.id == AgentRun.agent_id).filter(AgentRun.status == AgentRunStatus.DEAD).order_by(AgentRun.completed_at.desc()).limit(10).all()
    rejected = db.query(CodeCandidate).filter(CodeCandidate.status.in_([CodeCandidateStatus.REJECTED, CodeCandidateStatus.QA_FAILED, CodeCandidateStatus.FAILED])).order_by(CodeCandidate.updated_at.desc()).limit(10).all()
    return {
        "paused_agents": [{"agent": n, "paused_until": _iso(p), "consecutive_failures": f} for n, p, f in paused],
        "intents_awaiting_human_approval": [{"intent_id": str(i.id), "agent": n, "intent_type": i.intent_type, "run_id": str(i.run_id), "status": _ev(i.execution_status)} for i, n in awaiting],
        "dead_runs": [{"run_id": str(r.id), "agent": n, "error": r.error} for r, n in dead],
        "candidates_not_ready": [{"id": str(c.id), "title": c.title, "status": _ev(c.status), "qa_summary": (c.qa_report or {}).get("summary")} for c in rejected],
    }


def _answer_candidates(db: Session) -> Dict[str, Any]:
    rows = db.query(CodeCandidate).filter(CodeCandidate.status.in_([CodeCandidateStatus.BUILT, CodeCandidateStatus.QA_RUNNING, CodeCandidateStatus.SECURITY_REVIEW, CodeCandidateStatus.READY])).order_by(CodeCandidate.updated_at.desc()).limit(20).all()
    return {"candidates": [{"id": str(c.id), "title": c.title, "status": _ev(c.status), "branch": c.branch_name, "qa_verdict": (c.qa_report or {}).get("verdict")} for c in rows]}


def _answer_why_denied(db: Session, question: str) -> Dict[str, Any]:
    ids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", question.lower())
    out: Dict[str, Any] = {"denied_or_failed": []}
    q = db.query(AgentIntent, Agent.name).join(Agent, Agent.id == AgentIntent.agent_id).filter(AgentIntent.execution_status.in_([IntentExecutionStatus.FAILED, IntentExecutionStatus.DENIED, IntentExecutionStatus.REJECTED]))
    if ids:
        uid = uuid.UUID(ids[0])
        q = q.filter((AgentIntent.run_id == uid) | (AgentIntent.id == uid) | (AgentIntent.agent_id == uid))
        cand = db.query(CodeCandidate).filter(CodeCandidate.id == uid).first()
        if cand is not None:
            out["candidate"] = {"id": str(cand.id), "status": _ev(cand.status), "qa_failures": (cand.qa_report or {}).get("failures"), "error": cand.error}
    for i, n in q.order_by(AgentIntent.created_at.desc()).limit(10).all():
        out["denied_or_failed"].append({"intent_id": str(i.id), "agent": n, "intent_type": i.intent_type, "execution_status": _ev(i.execution_status), "policy_decision": _ev(i.policy_decision), "reason": i.error or i.policy_reason})
    return out


_QUESTION_ROUTES = [
    (re.compile(r"\bgoal", re.I), "goals", _answer_goals),
    (re.compile(r"\b(working|busy|active agents?|who is)", re.I), "working", _answer_working),
    (re.compile(r"\b(recent|happened|lately|history|last story)", re.I), "recent", _answer_recent),
    (re.compile(r"\bproposal", re.I), "proposals", _answer_proposals),
    (re.compile(r"\b(blocked|stuck|paused|approval|awaiting)", re.I), "blocked", _answer_blocked),
    (re.compile(r"\b(candidate|qa|code)", re.I), "candidates", _answer_candidates),
    (re.compile(r"\b(budget|spend|cost|wallet|credits?)", re.I), "budget", None),
    (re.compile(r"\b(why|fail|error|denied|deny)", re.I), "why_denied", None),
]


@router.get("/ask")
def ask(q: str = Query(..., min_length=2, max_length=500), db: Session = Depends(get_db), operator: User = Depends(require_operator)):
    """JARVIS v1 (operator): keyword-routed answers built only from persisted state."""
    answers: Dict[str, Any] = {"question": q, "answers": {}, "source": "persisted_state"}
    for pattern, key, fn in _QUESTION_ROUTES:
        if pattern.search(q):
            if key == "budget":
                answers["answers"]["budget"] = society_budget(db, operator)
            elif key == "why_denied":
                answers["answers"]["why_denied"] = _answer_why_denied(db, q)
            else:
                answers["answers"][key] = fn(db)
    if not answers["answers"]:
        answers["answers"]["status"] = society_status(db)
        answers["note"] = "question not recognised; returning status summary"
    return answers


# ── WORLD-EVENT INGRESS (operator | event_producer) ───────────────────

_MAX_DEPTH = 4
_MAX_STRING = 2000
_MAX_ITEMS = 50


def _check_payload_shape(value: Any, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise ValueError(f"payload nesting deeper than {_MAX_DEPTH}")
    if isinstance(value, dict):
        if len(value) > _MAX_ITEMS:
            raise ValueError(f"payload object has more than {_MAX_ITEMS} keys")
        for k, v in value.items():
            if not isinstance(k, str) or len(k) > 128:
                raise ValueError("payload keys must be strings of at most 128 chars")
            _check_payload_shape(v, depth + 1)
    elif isinstance(value, list):
        if len(value) > _MAX_ITEMS:
            raise ValueError(f"payload list longer than {_MAX_ITEMS}")
        for v in value:
            _check_payload_shape(v, depth + 1)
    elif isinstance(value, str):
        if len(value) > _MAX_STRING:
            raise ValueError(f"payload string longer than {_MAX_STRING} chars")
        if "\x00" in value:
            raise ValueError("payload contains a NUL byte")
    elif value is None or isinstance(value, (bool, int, float)):
        return
    else:
        raise ValueError(f"unsupported payload value type {type(value).__name__}")


class EventInject(BaseModel):
    event_type: str = Field(..., min_length=3, max_length=128, pattern=r"^[a-z0-9_.]+$")
    payload: Dict[str, Any] = Field(default_factory=dict)
    subject_type: Optional[str] = Field(None, max_length=64)
    subject_id: Optional[uuid.UUID] = None
    correlation_id: Optional[uuid.UUID] = None
    idempotency_key: Optional[str] = Field(None, min_length=1, max_length=160)

    @field_validator("payload")
    @classmethod
    def _bounded(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        _check_payload_shape(v)
        if "target_agent_id" in v:
            raise ValueError("target_agent_id cannot be set from outside the runtime")
        return v


@router.post("/events", status_code=status.HTTP_201_CREATED)
def inject_event(body: EventInject, db: Session = Depends(get_db), producer: User = Depends(require_event_producer)):
    """Inject an allow-listed WORLD event. Society-internal families are
    reserved; payloads are bounded; per-actor and global hourly limits apply;
    an idempotency key makes redelivery harmless."""
    settings = get_settings()
    if body.event_type.startswith(_RESERVED_INJECT_PREFIXES) or body.event_type not in settings.ingress_event_allowlist:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"event type not in the world-event allowlist: {sorted(settings.ingress_event_allowlist)}")
    raw_size = len(json.dumps(body.payload, ensure_ascii=False, default=str).encode("utf-8"))
    if raw_size > settings.ingress_max_payload_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"payload {raw_size} bytes exceeds {settings.ingress_max_payload_bytes}")

    # Idempotent redelivery short-circuits before any limit is consumed.
    if body.idempotency_key:
        existing = db.query(SocietyEvent).filter(SocietyEvent.idempotency_key == body.idempotency_key).first()
        if existing is not None:
            return {**_event_out(existing), "duplicate": True}

    hour_ago = _now() - timedelta(hours=1)
    mine = db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.actor_type == "user", SocietyEvent.actor_id == producer.id, SocietyEvent.created_at >= hour_ago).scalar() or 0
    if mine >= settings.ingress_max_events_per_actor_per_hour:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="per-actor hourly world-event limit reached")
    total = db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.actor_type == "user", SocietyEvent.created_at >= hour_ago).scalar() or 0
    if total >= settings.ingress_max_events_per_hour:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="global hourly world-event limit reached")

    ev = emit_event(
        db,
        event_type=body.event_type,
        payload=body.payload,
        actor_type="user",
        actor_id=producer.id,
        subject_type=body.subject_type,
        subject_id=body.subject_id,
        correlation_id=body.correlation_id,
        idempotency_key=body.idempotency_key,
    )
    db.commit()
    return {**_event_out(ev), "duplicate": False}
