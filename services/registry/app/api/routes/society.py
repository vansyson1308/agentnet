"""Society runtime observability + JARVIS-style operational queries.

Every answer here is read from persisted state (society_events, agent_runs,
agent_intents, code_candidates, goals, proposals, wallets). Nothing is
synthesised. Read endpoints are public like /fleet/activity; the only
mutating endpoint (``POST /society/events``) requires a logged-in user and
can inject *world* events (e.g. a metric anomaly) — never intents, grants
or budgets.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...auth import get_current_user
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
    IntentExecutionStatus,
    PolicyDecision,
    ProposalStatus,
    SocietyEvent,
    SocietyEventStatus,
    User,
    Wallet,
    WalletOwnerType,
)
from ...society.config import get_settings
from ...society.events import EventType, emit_event
from ...society.roles import load_role_definitions, role_as_dict

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/society", tags=["society"])

_FORBIDDEN_INJECT_PREFIXES = ("intent.", "run.", "loop_breaker.", "proposal.", "code_candidate.", "code_change.", "task.", "offer.", "goal.", "memory.", "agent.message")


def _ev(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_out(e: SocietyEvent) -> Dict[str, Any]:
    return {
        "id": str(e.id),
        "event_type": e.event_type,
        "status": _ev(e.status),
        "actor_type": e.actor_type,
        "actor_id": str(e.actor_id) if e.actor_id else None,
        "subject_type": e.subject_type,
        "subject_id": str(e.subject_id) if e.subject_id else None,
        "correlation_id": str(e.correlation_id),
        "causation_id": str(e.causation_id) if e.causation_id else None,
        "causation_depth": e.causation_depth,
        "source_run_id": str(e.source_run_id) if e.source_run_id else None,
        "payload": e.payload,
        "dispatch_note": e.dispatch_note,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "processed_at": e.processed_at.isoformat() if e.processed_at else None,
    }


def _run_out(r: AgentRun, agent_name: Optional[str] = None, event_type: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": str(r.id),
        "agent_id": str(r.agent_id),
        "agent_name": agent_name,
        "role": r.role,
        "event_id": str(r.event_id),
        "event_type": event_type,
        "status": _ev(r.status),
        "attempt": r.attempt,
        "worker_id": r.worker_id,
        "model_provider": r.model_provider,
        "model_name": r.model_name,
        "decision_summary": r.decision_summary,
        "intents_count": r.intents_count,
        "cost_usd": str(r.cost_usd or 0),
        "tokens_in": r.tokens_in,
        "tokens_out": r.tokens_out,
        "error": r.error,
        "correlation_id": str(r.correlation_id),
        "trace_id": str(r.trace_id) if r.trace_id else None,
        "context_digest": r.context_digest,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "sleep_until": r.sleep_until.isoformat() if r.sleep_until else None,
    }


def _intent_out(i: AgentIntent) -> Dict[str, Any]:
    return {
        "id": str(i.id),
        "run_id": str(i.run_id),
        "agent_id": str(i.agent_id),
        "seq": i.seq,
        "intent_type": i.intent_type,
        "risk_class": _ev(i.risk_class),
        "policy_decision": _ev(i.policy_decision),
        "policy_reason": i.policy_reason,
        "execution_status": _ev(i.execution_status),
        "result": i.result,
        "error": i.error,
        "payload": i.payload,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "executed_at": i.executed_at.isoformat() if i.executed_at else None,
    }


def _candidate_out(c: CodeCandidate) -> Dict[str, Any]:
    return {
        "id": str(c.id),
        "title": c.title,
        "status": _ev(c.status),
        "branch_name": c.branch_name,
        "head_sha": c.head_sha,
        "base_sha": c.base_sha,
        "changed_files": c.changed_files,
        "diff_stat": c.diff_stat,
        "proposal_id": str(c.proposal_id) if c.proposal_id else None,
        "task_id": str(c.task_id) if c.task_id else None,
        "correlation_id": str(c.correlation_id),
        "requires_security_review": c.requires_security_review,
        "qa": {"verdict": (c.qa_report or {}).get("verdict"), "summary": (c.qa_report or {}).get("summary"), "attempts": (c.qa_report or {}).get("attempts"), "failures": (c.qa_report or {}).get("failures")},
        "security": {"verdict": (c.security_report or {}).get("verdict"), "static_findings": (c.security_report or {}).get("static_findings")},
        "builder_agent_id": str(c.builder_agent_id) if c.builder_agent_id else None,
        "qa_agent_id": str(c.qa_agent_id) if c.qa_agent_id else None,
        "error": c.error,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


# ── status / metrics ──────────────────────────────────────────────────


@router.get("/status")
def society_status(db: Session = Depends(get_db)):
    """Truthful runtime status: flags, fleet, pending work, last activity."""
    settings = get_settings()
    now = _now()
    hour_ago = now - timedelta(hours=1)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    fleet = (
        db.query(Agent.id, Agent.name, AgentCapabilityGrant.role, AgentCapabilityGrant.enabled, AgentCapabilityGrant.paused_until, AgentCapabilityGrant.consecutive_failures)
        .join(AgentCapabilityGrant, AgentCapabilityGrant.agent_id == Agent.id)
        .order_by(Agent.name)
        .all()
    )
    working = {
        str(r.agent_id): _ev(r.status)
        for r in db.query(AgentRun).filter(AgentRun.status.in_([AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING])).all()
    }
    last_run_at = db.query(func.max(AgentRun.completed_at)).scalar()
    return {
        "runtime_enabled": settings.runtime_enabled,
        "autonomous_code_enabled": settings.autonomous_code_enabled,
        "staging_deploy_enabled": settings.staging_deploy_enabled,
        "production_deploy_enabled": settings.production_deploy_enabled,
        "model_provider": settings.model_provider,
        "model_name": settings.model_name if settings.model_provider == "openai_compatible" else None,
        "fleet": [
            {
                "agent_id": str(aid),
                "name": name,
                "role": role,
                "enabled": enabled,
                "paused_until": paused.isoformat() if paused else None,
                "consecutive_failures": failures,
                "working": working.get(str(aid)),
            }
            for aid, name, role, enabled, paused, failures in fleet
        ],
        "pending_events": db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.status == SocietyEventStatus.PENDING).scalar() or 0,
        "queued_runs": db.query(func.count(AgentRun.id)).filter(AgentRun.status == AgentRunStatus.QUEUED).scalar() or 0,
        "active_runs": len(working),
        "runs_last_hour": db.query(func.count(AgentRun.id)).filter(AgentRun.started_at >= hour_ago).scalar() or 0,
        "runs_today": {
            s: (db.query(func.count(AgentRun.id)).filter(AgentRun.created_at >= day_start, AgentRun.status == s).scalar() or 0)
            for s in ("completed", "skipped", "dead", "queued")
        },
        "model_spend_today_usd": str(db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0)).filter(AgentRun.created_at >= day_start).scalar() or 0),
        "daily_model_budget_usd": str(settings.daily_model_budget_usd),
        "last_run_completed_at": last_run_at.isoformat() if last_run_at else None,
        "candidates_by_status": {
            s: n for s, n in db.query(CodeCandidate.status, func.count(CodeCandidate.id)).group_by(CodeCandidate.status).all()
        },
        "limits": {
            "max_runs_per_hour": settings.max_runs_per_hour,
            "max_causation_depth": settings.max_causation_depth,
            "max_runs_per_correlation": settings.max_runs_per_correlation,
            "max_intents_per_run": settings.max_intents_per_run,
            "max_task_escrow_credits": settings.max_task_escrow_credits,
        },
    }


@router.get("/metrics")
def society_metrics(db: Session = Depends(get_db), hours: int = Query(24, ge=1, le=24 * 30)):
    since = _now() - timedelta(hours=hours)
    runs = dict(db.query(AgentRun.status, func.count(AgentRun.id)).filter(AgentRun.created_at >= since).group_by(AgentRun.status).all())
    intents = dict(db.query(AgentIntent.policy_decision, func.count(AgentIntent.id)).filter(AgentIntent.created_at >= since).group_by(AgentIntent.policy_decision).all())
    executions = dict(db.query(AgentIntent.execution_status, func.count(AgentIntent.id)).filter(AgentIntent.created_at >= since).group_by(AgentIntent.execution_status).all())
    events = dict(db.query(SocietyEvent.status, func.count(SocietyEvent.id)).filter(SocietyEvent.created_at >= since).group_by(SocietyEvent.status).all())
    loop_breaks = db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED, SocietyEvent.created_at >= since).scalar() or 0
    dupes_prevented = db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.created_at >= since, SocietyEvent.dispatch_note.like("loop breaker%")).scalar() or 0
    cost = db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0)).filter(AgentRun.created_at >= since).scalar() or 0
    tokens = db.query(func.coalesce(func.sum(AgentRun.tokens_in), 0), func.coalesce(func.sum(AgentRun.tokens_out), 0)).filter(AgentRun.created_at >= since).first()
    return {
        "window_hours": hours,
        "events": {_ev(k): v for k, v in events.items()},
        "pending_events": db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.status == SocietyEventStatus.PENDING).scalar() or 0,
        "runs": {_ev(k): v for k, v in runs.items()},
        "intents_by_policy": {_ev(k): v for k, v in intents.items()},
        "intents_by_execution": {_ev(k): v for k, v in executions.items()},
        "policy_denials": (intents.get(PolicyDecision.DENY, 0) or 0) + (intents.get(PolicyDecision.INVALID, 0) or 0),
        "loop_breaker_activations": loop_breaks,
        "events_suppressed_by_loop_breaker": dupes_prevented,
        "model_cost_usd": str(cost),
        "tokens_in": int(tokens[0] or 0),
        "tokens_out": int(tokens[1] or 0),
        "pending_code_candidates": db.query(func.count(CodeCandidate.id)).filter(CodeCandidate.status.in_([CodeCandidateStatus.REQUESTED, CodeCandidateStatus.BUILDING, CodeCandidateStatus.BUILT, CodeCandidateStatus.QA_RUNNING, CodeCandidateStatus.SECURITY_REVIEW])).scalar() or 0,
    }


@router.get("/config")
def society_config(current_user: User = Depends(get_current_user)):
    """Runtime settings (API key redacted) + role definitions. Auth required:
    exposes repo/workspace paths and provider endpoints."""
    settings = get_settings()
    return {"settings": settings.public_dict(), "roles": {k: role_as_dict(v) for k, v in load_role_definitions().items()}}


# ── listings ──────────────────────────────────────────────────────────


@router.get("/events")
def list_events(
    db: Session = Depends(get_db),
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
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db)):
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


@router.get("/candidates")
def list_candidates(db: Session = Depends(get_db), status_filter: Optional[str] = Query(None, alias="status"), limit: int = Query(50, ge=1, le=500)):
    q = db.query(CodeCandidate)
    if status_filter:
        q = q.filter(CodeCandidate.status == status_filter)
    return [_candidate_out(c) for c in q.order_by(CodeCandidate.created_at.desc()).limit(limit).all()]


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: uuid.UUID, db: Session = Depends(get_db)):
    c = db.query(CodeCandidate).filter(CodeCandidate.id == candidate_id).first()
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="candidate not found")
    out = _candidate_out(c)
    out["spec"] = c.spec
    out["qa_report"] = c.qa_report
    out["security_report"] = c.security_report
    return out


@router.get("/story/{correlation_id}")
def correlation_story(correlation_id: uuid.UUID, db: Session = Depends(get_db)):
    """Everything that happened in one correlation, in order: the
    inspectable chain event -> run -> intents -> resulting events."""
    events = db.query(SocietyEvent).filter(SocietyEvent.correlation_id == correlation_id).order_by(SocietyEvent.created_at).all()
    runs = db.query(AgentRun, Agent.name, SocietyEvent.event_type).join(Agent, Agent.id == AgentRun.agent_id).join(SocietyEvent, SocietyEvent.id == AgentRun.event_id).filter(AgentRun.correlation_id == correlation_id).order_by(AgentRun.created_at).all()
    run_ids = [r.id for r, _, _ in runs]
    intents = db.query(AgentIntent).filter(AgentIntent.run_id.in_(run_ids)).order_by(AgentIntent.created_at, AgentIntent.seq).all() if run_ids else []
    candidates = db.query(CodeCandidate).filter(CodeCandidate.correlation_id == correlation_id).all()
    by_run: Dict[str, List[Dict[str, Any]]] = {}
    for i in intents:
        by_run.setdefault(str(i.run_id), []).append(_intent_out(i))
    return {
        "correlation_id": str(correlation_id),
        "events": [_event_out(e) for e in events],
        "runs": [{**_run_out(r, name, et), "intents": by_run.get(str(r.id), [])} for r, name, et in runs],
        "candidates": [_candidate_out(c) for c in candidates],
    }


# ── JARVIS: operational questions answered from persisted state ───────


def _answer_goals(db: Session) -> Dict[str, Any]:
    goals = db.query(Goal).filter(Goal.status.in_([GoalStatus.ACTIVE, GoalStatus.PAUSED])).order_by(Goal.priority.desc(), Goal.created_at).limit(20).all()
    return {"active_goals": [{"id": str(g.id), "title": g.title, "owner": _ev(g.owner_type), "priority": _ev(g.priority), "status": _ev(g.status)} for g in goals]}


def _answer_working(db: Session) -> Dict[str, Any]:
    rows = db.query(AgentRun, Agent.name, SocietyEvent.event_type).join(Agent, Agent.id == AgentRun.agent_id).join(SocietyEvent, SocietyEvent.id == AgentRun.event_id).filter(AgentRun.status.in_([AgentRunStatus.CLAIMED, AgentRunStatus.RUNNING])).all()
    return {"working_agents": [{"agent": name, "role": r.role, "event_type": et, "run_id": str(r.id), "since": r.started_at.isoformat() if r.started_at else None} for r, name, et in rows]}


def _answer_recent(db: Session) -> Dict[str, Any]:
    rows = db.query(AgentRun, Agent.name, SocietyEvent.event_type).join(Agent, Agent.id == AgentRun.agent_id).join(SocietyEvent, SocietyEvent.id == AgentRun.event_id).filter(AgentRun.status.in_([AgentRunStatus.COMPLETED, AgentRunStatus.DEAD])).order_by(AgentRun.completed_at.desc()).limit(10).all()
    return {"recent_runs": [{"agent": name, "role": r.role, "event_type": et, "status": _ev(r.status), "decision": r.decision_summary, "at": r.completed_at.isoformat() if r.completed_at else None, "run_id": str(r.id)} for r, name, et in rows]}


def _answer_proposals(db: Session) -> Dict[str, Any]:
    rows = db.query(ImprovementProposal).filter(ImprovementProposal.status.in_([ProposalStatus.PROPOSED, ProposalStatus.UNDER_REVIEW, ProposalStatus.APPROVED])).order_by(ImprovementProposal.importance.desc()).limit(20).all()
    return {"pending_proposals": [{"id": str(p.id), "title": p.title, "status": _ev(p.status), "importance": p.importance} for p in rows]}


def _answer_blocked(db: Session) -> Dict[str, Any]:
    now = _now()
    paused = db.query(Agent.name, AgentCapabilityGrant.paused_until, AgentCapabilityGrant.consecutive_failures).join(AgentCapabilityGrant, AgentCapabilityGrant.agent_id == Agent.id).filter(AgentCapabilityGrant.paused_until > now).all()
    awaiting = db.query(AgentIntent, Agent.name).join(Agent, Agent.id == AgentIntent.agent_id).filter(AgentIntent.execution_status == IntentExecutionStatus.AWAITING_APPROVAL).limit(20).all()
    dead = db.query(AgentRun, Agent.name).join(Agent, Agent.id == AgentRun.agent_id).filter(AgentRun.status == AgentRunStatus.DEAD).order_by(AgentRun.completed_at.desc()).limit(10).all()
    rejected = db.query(CodeCandidate).filter(CodeCandidate.status.in_([CodeCandidateStatus.REJECTED, CodeCandidateStatus.QA_FAILED, CodeCandidateStatus.FAILED])).order_by(CodeCandidate.updated_at.desc()).limit(10).all()
    return {
        "paused_agents": [{"agent": n, "paused_until": p.isoformat(), "consecutive_failures": f} for n, p, f in paused],
        "intents_awaiting_human_approval": [{"intent_id": str(i.id), "agent": n, "intent_type": i.intent_type, "run_id": str(i.run_id)} for i, n in awaiting],
        "dead_runs": [{"run_id": str(r.id), "agent": n, "error": r.error} for r, n in dead],
        "candidates_not_ready": [{"id": str(c.id), "title": c.title, "status": _ev(c.status), "qa_summary": (c.qa_report or {}).get("summary")} for c in rejected],
    }


def _answer_candidates(db: Session) -> Dict[str, Any]:
    rows = db.query(CodeCandidate).filter(CodeCandidate.status.in_([CodeCandidateStatus.BUILT, CodeCandidateStatus.QA_RUNNING, CodeCandidateStatus.SECURITY_REVIEW, CodeCandidateStatus.READY])).order_by(CodeCandidate.updated_at.desc()).limit(20).all()
    return {"candidates": [{"id": str(c.id), "title": c.title, "status": _ev(c.status), "branch": c.branch_name, "qa_verdict": (c.qa_report or {}).get("verdict")} for c in rows]}


def _answer_budget(db: Session) -> Dict[str, Any]:
    settings = get_settings()
    day_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    spend = db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0)).filter(AgentRun.created_at >= day_start).scalar() or 0
    per_agent = db.query(Agent.name, func.coalesce(func.sum(AgentRun.cost_usd), 0)).join(AgentRun, AgentRun.agent_id == Agent.id).filter(AgentRun.created_at >= day_start).group_by(Agent.name).all()
    wallets = db.query(Agent.name, Wallet.balance_credits, Wallet.reserved_credits).join(Wallet, (Wallet.owner_id == Agent.id) & (Wallet.owner_type == WalletOwnerType.AGENT)).join(AgentCapabilityGrant, AgentCapabilityGrant.agent_id == Agent.id).all()
    return {
        "model_spend_today_usd": str(spend),
        "daily_model_budget_usd": str(settings.daily_model_budget_usd),
        "remaining_usd": str(max(0, settings.daily_model_budget_usd - spend)),
        "spend_by_agent_usd": {n: str(v) for n, v in per_agent},
        "wallets": [{"agent": n, "balance_credits": b, "reserved_credits": r} for n, b, r in wallets],
    }


def _answer_why_failed(db: Session, question: str) -> Dict[str, Any]:
    ids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", question.lower())
    out: Dict[str, Any] = {"failures": []}
    q = db.query(AgentIntent, Agent.name).join(Agent, Agent.id == AgentIntent.agent_id).filter(AgentIntent.execution_status.in_([IntentExecutionStatus.FAILED, IntentExecutionStatus.DENIED]))
    if ids:
        uid = uuid.UUID(ids[0])
        q = q.filter((AgentIntent.run_id == uid) | (AgentIntent.id == uid) | (AgentIntent.agent_id == uid))
        from ...models import TaskSession

        task = db.query(TaskSession).filter(TaskSession.id == uid).first()
        if task is not None:
            out["task"] = {"id": str(task.id), "status": _ev(task.status), "error_message": task.error_message, "capability": task.capability}
        cand = db.query(CodeCandidate).filter(CodeCandidate.id == uid).first()
        if cand is not None:
            out["candidate"] = {"id": str(cand.id), "status": _ev(cand.status), "qa_failures": (cand.qa_report or {}).get("failures"), "error": cand.error}
    for i, n in q.order_by(AgentIntent.created_at.desc()).limit(10).all():
        out["failures"].append({"intent_id": str(i.id), "agent": n, "intent_type": i.intent_type, "execution_status": _ev(i.execution_status), "reason": i.error or i.policy_reason})
    return out


_QUESTION_ROUTES = [
    (re.compile(r"\bgoal", re.I), _answer_goals),
    (re.compile(r"\b(working|busy|active agents?|who is)", re.I), _answer_working),
    (re.compile(r"\b(recent|happened|lately|history)", re.I), _answer_recent),
    (re.compile(r"\bproposal", re.I), _answer_proposals),
    (re.compile(r"\b(blocked|stuck|paused|approval)", re.I), _answer_blocked),
    (re.compile(r"\b(candidate|qa|code)", re.I), _answer_candidates),
    (re.compile(r"\b(budget|spend|cost|wallet|credits?)", re.I), _answer_budget),
    (re.compile(r"\b(why|fail|error)", re.I), None),  # handled with the question text
]


@router.get("/ask")
def ask(q: str = Query(..., min_length=2, max_length=500), db: Session = Depends(get_db)):
    """JARVIS v1: keyword-routed answers built only from persisted state.
    Unknown questions return the status summary so nothing is invented."""
    answers: Dict[str, Any] = {"question": q, "answers": {}, "source": "persisted_state"}
    for pattern, fn in _QUESTION_ROUTES:
        if pattern.search(q):
            if fn is None:
                answers["answers"]["why_failed"] = _answer_why_failed(db, q)
            else:
                answers["answers"][fn.__name__.replace("_answer_", "")] = fn(db)
    if not answers["answers"]:
        answers["answers"]["status"] = society_status(db)
        answers["note"] = "question not recognised; returning status summary"
    return answers


# ── injection of WORLD events (auth required) ─────────────────────────


class EventInject(BaseModel):
    event_type: str = Field(..., min_length=3, max_length=128, pattern=r"^[a-z0-9_.]+$")
    payload: Dict[str, Any] = Field(default_factory=dict)
    subject_type: Optional[str] = Field(None, max_length=64)
    subject_id: Optional[uuid.UUID] = None
    correlation_id: Optional[uuid.UUID] = None
    idempotency_key: Optional[str] = Field(None, max_length=160)


@router.post("/events", status_code=status.HTTP_201_CREATED)
def inject_event(body: EventInject, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Inject a world/domain event (e.g. platform.metric.anomaly). Society-
    internal event types (proposal.*, code_candidate.*, intent.*, ...) are
    reserved for the runtime and cannot be injected from outside."""
    if body.event_type.startswith(_FORBIDDEN_INJECT_PREFIXES):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="reserved event type; only world events may be injected")
    if body.payload and "target_agent_id" in body.payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target_agent_id cannot be set from outside the runtime")
    ev = emit_event(
        db,
        event_type=body.event_type,
        payload=body.payload,
        actor_type="user",
        actor_id=current_user.id,
        subject_type=body.subject_type,
        subject_id=body.subject_id,
        correlation_id=body.correlation_id,
        idempotency_key=body.idempotency_key,
    )
    db.commit()
    return _event_out(ev)
