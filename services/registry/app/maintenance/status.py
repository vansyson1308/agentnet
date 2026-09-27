"""Read models for the operator status page and the public summary.

Operator views show case state, next action, deadline, risk, activities
(kind/outcome/cost -- never model text), CI/release/rollback facts, freezes,
heartbeats, SLO budgets, toil and KPIs: no database spelunking required.

The PUBLIC summary is aggregate and structural only: counts per outcome
class, whether availability budgets are healthy, whether automation is on.
No ids, paths, payloads, plans or decision text (ADR-0010 D19).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..society.events import utcnow
from . import kpi as kpi_mod
from . import slo as slo_mod
from . import state_machine as sm
from .config import MaintenanceSettings
from .orm import (
    MaintenanceHeartbeat,
    MaintenanceIncident,
    MaintenanceRelease,
    MaintenanceReleaseFreeze,
    RepairActivity,
    RepairArtifact,
    RepairAttempt,
    RepairCase,
    RepairEvidence,
    RepairPlanRevision,
    RepairTransition,
)
from .taxonomy import RELEASE_IN_FLIGHT


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() if d else None


def case_row(c: RepairCase) -> Dict[str, Any]:
    return {
        "id": str(c.id), "incident_id": str(c.incident_id), "state": c.state, "priority": c.priority, "risk_class": c.risk_class,
        "repair_class": c.repair_class, "plan_revision": c.current_plan_revision, "attempts": c.attempt_count, "rescopes": c.rescope_count,
        "next_action_at": _iso(c.next_action_at), "deadline_at": _iso(c.deadline_at), "case_deadline_at": _iso(c.case_deadline_at),
        "lease_owner": c.lease_owner, "model_cost_usd": str(c.model_cost_usd), "terminal_reason": c.terminal_reason, "resumable": bool(c.resumable),
        "started_at": _iso(c.started_at), "terminal_at": _iso(c.terminal_at), "promotion_id": str(c.promotion_id) if c.promotion_id else None,
        "release_id": str(c.release_id) if c.release_id else None, "merged_sha": c.merged_sha,
    }


def incident_row(i: MaintenanceIncident) -> Dict[str, Any]:
    return {
        "id": str(i.id), "fingerprint": i.fingerprint, "class": i.incident_class, "priority": i.priority, "severity": i.severity, "status": i.status,
        "desired_state_ref": i.desired_state_ref, "target": i.target, "source": i.source, "observations": i.observation_count, "healthy_streak": i.healthy_streak,
        "first_observed_at": _iso(i.first_observed_at), "last_observed_at": _iso(i.last_observed_at), "recurrence_of": str(i.recurrence_of) if i.recurrence_of else None,
        "cases": i.case_count, "resolved_at": _iso(i.resolved_at),
    }


def release_row(r: MaintenanceRelease) -> Dict[str, Any]:
    return {
        "id": str(r.id), "case_id": str(r.case_id), "status": r.status, "risk_class": r.risk_class, "head_sha": r.head_sha, "pr": r.pr_number, "pr_url": r.pr_url,
        "production_merge_sha": r.production_merge_sha, "services": r.services, "healthy_streak": r.healthy_streak, "failure_reason": r.failure_reason,
        "rollback": {k: v for k, v in (r.rollback or {}).items() if k in ("reason", "result", "services", "reconciliation")}, "created_at": _iso(r.created_at), "completed_at": _iso(r.completed_at),
    }


def operator_status(db: Session, settings: MaintenanceSettings, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    from .reconciler import stranded_count  # noqa: PLC0415

    now = now or utcnow()
    nonterminal = [s.value for s in sm.NON_TERMINAL]
    active = db.query(RepairCase).filter((RepairCase.state.in_(nonterminal)) | ((RepairCase.state == sm.CaseState.SAFELY_ESCALATED.value) & (RepairCase.resumable.is_(True)))).order_by(RepairCase.priority, RepairCase.started_at).limit(50).all()
    recent = db.query(RepairCase).filter(RepairCase.state.notin_(nonterminal)).order_by(RepairCase.terminal_at.desc()).limit(20).all()
    return {
        "generated_at": now.isoformat(),
        "flags": settings.public_flags(),
        "bounds": settings.bounds(),
        "nothing_stranded": {"stranded_cases": stranded_count(db, now=now, stall_seconds=settings.stall_seconds)},
        "error_budget": [b.as_dict() for b in slo_mod.budgets(db, target=settings.target, now=now)],
        "repair_latency": slo_mod.repair_latency(db, now=now),
        "open_incidents": [incident_row(i) for i in db.query(MaintenanceIncident).filter(MaintenanceIncident.status == "open").order_by(MaintenanceIncident.priority, MaintenanceIncident.first_observed_at).limit(50).all()],
        "active_cases": [case_row(c) for c in active],
        "awaiting_owner": [case_row(c) for c in active if c.state == sm.CaseState.SAFELY_ESCALATED.value],
        "recent_outcomes": [case_row(c) for c in recent],
        "releases_in_flight": [release_row(r) for r in db.query(MaintenanceRelease).filter(MaintenanceRelease.status.in_([s.value for s in RELEASE_IN_FLIGHT])).all()],
        "recent_releases": [release_row(r) for r in db.query(MaintenanceRelease).order_by(MaintenanceRelease.created_at.desc()).limit(10).all()],
        "release_freezes": [{"id": str(f.id), "reason_code": f.reason_code, "owner_only": f.owner_only, "opened_at": _iso(f.opened_at), "detail": f.detail} for f in db.query(MaintenanceReleaseFreeze).filter(MaintenanceReleaseFreeze.lifted_at.is_(None)).all()],
        "heartbeats": [{"component": h.component, "beat_at": _iso(h.beat_at), "age_seconds": (now - h.beat_at).total_seconds() if h.beat_at else None, "cycles": h.cycles, "errors": h.errors, "last_error_class": h.last_error_class} for h in db.query(MaintenanceHeartbeat).all()],
        "kpis": kpi_mod.kpis(db, now=now),
    }


def case_detail(db: Session, case: RepairCase) -> Dict[str, Any]:
    return {
        "case": case_row(case),
        "incident": incident_row(db.get(MaintenanceIncident, case.incident_id)),
        "escalation": case.escalation or None,
        "facts": case.facts or {},
        "transitions": [{"from": t.from_state, "to": t.to_state, "actor": t.actor_type, "actor_id": t.actor_id, "reason": t.reason_code, "evidence_digest": t.evidence_digest, "at": _iso(t.created_at)} for t in db.query(RepairTransition).filter(RepairTransition.case_id == case.id).order_by(RepairTransition.id).all()],
        "plans": [{"revision": p.revision, "parent": p.parent_revision, "files_allowed": p.files_allowed, "acceptance_tests": p.acceptance_tests, "risk_class": p.risk_class, "risk_reasons": p.risk_reasons, "rescope_reason": p.rescope_reason, "digest": p.digest} for p in db.query(RepairPlanRevision).filter(RepairPlanRevision.case_id == case.id).order_by(RepairPlanRevision.revision).all()],
        "attempts": [{"attempt": a.attempt, "plan_revision": a.plan_revision, "outcome": a.outcome, "turns": a.turns, "test_runs": a.test_runs, "head_sha": a.head_sha, "patch_digest": a.patch_digest} for a in db.query(RepairAttempt).filter(RepairAttempt.case_id == case.id).order_by(RepairAttempt.attempt).all()],
        "activities": [{"kind": a.kind, "role": a.role, "try": a.try_number, "status": a.status, "error_class": a.error_class, "provider": a.model_provider, "turns": a.turns, "cost_usd": str(a.cost_usd), "started_at": _iso(a.started_at)} for a in db.query(RepairActivity).filter(RepairActivity.case_id == case.id).order_by(RepairActivity.started_at).all()],
        "artifacts": [{"kind": a.kind, "trust": a.trust_class, "digest": a.digest, "plan_revision": a.plan_revision, "attempt": a.attempt, "at": _iso(a.created_at)} for a in db.query(RepairArtifact).filter(RepairArtifact.case_id == case.id).order_by(RepairArtifact.created_at).all()],
        "evidence": [{"kind": e.kind, "source": e.source, "collector": e.collector_version, "trust": e.trust_class, "digest": e.digest, "at": _iso(e.collected_at)} for e in db.query(RepairEvidence).filter(RepairEvidence.case_id == case.id).order_by(RepairEvidence.collected_at).all()],
        "release": release_row(db.get(MaintenanceRelease, case.release_id)) if case.release_id else None,
    }


def public_summary(db: Session, settings: MaintenanceSettings, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or utcnow()
    nonterminal = [s.value for s in sm.NON_TERMINAL]
    by_state = dict(db.query(RepairCase.state, func.count(RepairCase.id)).group_by(RepairCase.state).all())
    budgets = slo_mod.budgets(db, target=settings.target, now=now)
    return {
        "maintenance_automation": bool(settings.autonomy_enabled),
        "open_incidents": int(db.query(func.count(MaintenanceIncident.id)).filter(MaintenanceIncident.status == "open").scalar() or 0),
        "active_repairs": int(sum(v for k, v in by_state.items() if k in nonterminal)),
        "outcomes": {k: int(v) for k, v in by_state.items() if k not in nonterminal},
        "availability_budget_healthy": not any(b.exhausted for b in budgets if b.availability_class),
    }


__all__ = ["operator_status", "case_detail", "public_summary", "case_row", "incident_row", "release_row"]
