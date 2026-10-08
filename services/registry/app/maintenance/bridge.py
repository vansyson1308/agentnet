"""Carry a verified repair to ``main`` through the EXISTING trusted path.

The Maintenance OS adds no second merge path. A verified attempt becomes a
READY ``CodeCandidate`` whose worktree is the attempt's worktree (the
candidate id IS the attempt workspace id), and a ``CodePromotion`` request.
From there the non-LLM promotion controller does what it always does:
trusted-base validation and risk re-classification, branch, PR, required CI,
fitness pre-check, and -- for GREEN only, with auto-merge on and no freeze --
the autonomous merge. AMBER/RED PRs wait for the owner.

Maintenance promotions are NOT counted against the innovation portfolio or
its PR budgets (a proven outage never waits for a feature slot); the
autonomous-merge daily cap and every merge gate still apply.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ..models import Agent, CodeCandidate, CodeCandidateStatus, CodePromotion, PromotionStatus, RiskTier
from ..society.config import SocietySettings
from ..society.engineering import workspace as ws_mod
from ..society.events import EventType, emit_event
from .orm import MaintenanceIncident, RepairCase, RepairPlanRevision
from .taxonomy import MaintenanceRiskClass as MRC

#: Fleet agents credited with maintenance activities (the Society's cognitive
#: workers); absent in a bare database, which is allowed.
ROLE_AGENT = {
    "diagnostician": "Society_Scout",
    "architect": "Society_Architect",
    "builder": "Society_Builder",
    "qa_reviewer": "Society_QA",
    "security_reviewer": "Society_Security",
    "governor": "Society_Governor",
    "evaluator": "Society_Evaluator",
}


def fleet_agent(db: Session, role: str) -> Optional[Agent]:
    name = ROLE_AGENT.get(role)
    return db.query(Agent).filter(Agent.name == name).first() if name else None


def create_candidate(
    db: Session,
    *,
    society_settings: SocietySettings,
    case: RepairCase,
    incident: MaintenanceIncident,
    plan: RepairPlanRevision,
    ws: ws_mod.Workspace,
    verification: Dict[str, Any],
    security: Dict[str, Any],
) -> CodeCandidate:
    """Idempotent: the candidate id is the attempt workspace id."""
    existing = db.get(CodeCandidate, ws.candidate_id)
    if existing is not None:
        return existing
    builder, qa, sec, arch = (fleet_agent(db, r) for r in ("builder", "qa_reviewer", "security_reviewer", "architect"))
    risk_class = MRC(case.risk_class or MRC.AMBER.value)
    base_tier = {MRC.MAINTENANCE_GREEN: RiskTier.GREEN, MRC.AMBER: RiskTier.AMBER}.get(risk_class, RiskTier.RED).value
    cand = CodeCandidate(
        id=ws.candidate_id,
        correlation_id=case.id,
        requested_by_agent_id=arch.id if arch else None,
        builder_agent_id=builder.id if builder else None,
        qa_agent_id=qa.id if qa else None,
        security_agent_id=sec.id if sec else None,
        title=f"maintenance {incident.priority} {incident.incident_class}: {incident.desired_state_ref}"[:255],
        spec={
            "kind": "maintenance",
            "files_allowed": list(plan.files_allowed or []),
            "acceptance_tests": list(plan.acceptance_tests or []),
            "maintenance": {
                "case_id": str(case.id),
                "incident_id": str(incident.id),
                "fingerprint": incident.fingerprint,
                "plan_revision": plan.revision,
                "attempt": case.attempt_count,
                "risk_class": risk_class.value,
            },
        },
        branch_name=ws.branch,
        workspace_path=str(ws.path),
        base_sha=ws.base_sha,
        head_sha=verification["head_sha"],
        diff_stat=ws_mod.diff_stat(ws),
        patch_summary=f"maintenance repair for incident {incident.id} (plan r{plan.revision}, attempt {case.attempt_count})",
        changed_files=list(verification["changed"]),
        status=CodeCandidateStatus.READY,
        qa_report=verification["qa"],
        security_report=security,
        requires_security_review=risk_class is not MRC.MAINTENANCE_GREEN,
        risk_tier=base_tier,
        diff_hash=verification["diff_digest"],
        diff_lines=int(verification["diff_lines"]),
        engineering_turns=0,
        repo_reads=0,
    )
    db.add(cand)
    db.flush()
    return cand


def request_promotion(db: Session, *, society_settings: SocietySettings, case: RepairCase, candidate: CodeCandidate) -> CodePromotion:
    """REQUESTED promotion for the candidate (idempotent per candidate)."""
    existing = (
        db.query(CodePromotion)
        .filter(CodePromotion.candidate_id == candidate.id, CodePromotion.status.notin_([PromotionStatus.REJECTED.value, PromotionStatus.SUPERSEDED.value]))
        .first()
    )
    if existing is not None:
        return existing
    promo = CodePromotion(
        id=uuid.uuid4(),
        candidate_id=candidate.id,
        correlation_id=candidate.correlation_id,
        risk_tier=candidate.risk_tier or RiskTier.AMBER.value,
        base_sha=candidate.base_sha,
        candidate_sha=candidate.head_sha,
        provider=society_settings.promotion_provider,
        status=PromotionStatus.REQUESTED,
        requested_by_agent_id=None,
        requested_by_run_id=None,
        eligibility={},
        evidence={"requested_by": "maintenance-kernel", "case_id": str(case.id)},
    )
    db.add(promo)
    db.flush()
    emit_event(
        db,
        event_type=EventType.PROMOTION_REQUESTED,
        payload={"promotion_id": str(promo.id), "candidate_id": str(candidate.id), "title": candidate.title, "status": "requested", "risk_tier": promo.risk_tier, "provider": promo.provider, "maintenance_case_id": str(case.id)},
        actor_type="system",
        subject_type="code_promotion",
        subject_id=promo.id,
        correlation_id=candidate.correlation_id,
        idempotency_key=f"promotion:{promo.id}:requested",
        notify=True,
    )
    return promo


def ensure_experiment(db: Session, *, society_settings: SocietySettings, promotion: CodePromotion, candidate: CodeCandidate) -> None:
    """The fitness pre-check the merge gate needs, requested by the system
    (deterministically) once the PR exists -- no Evaluator run required."""
    from ..society.fitness import request_experiment  # noqa: PLC0415

    status = getattr(promotion.status, "value", promotion.status)
    if status in ("pr_open", "ci_pending", "ci_passed", "awaiting_approval", "merge_eligible", "blocked_external"):
        request_experiment(db, settings=society_settings, promotion=promotion, candidate=candidate, agent=None, causation=None, source_run_id=None)


__all__ = ["ROLE_AGENT", "fleet_agent", "create_candidate", "request_promotion", "ensure_experiment"]
