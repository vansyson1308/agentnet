"""Safe escalation: one complete, concise review package for the owner.

The package is assembled from durable rows (facts). A model-written
explanation may be attached, labelled ``model_hypothesis``; it never replaces
a fact. One durable notification per escalated case goes to each operator
(``users.society_role = 'operator'``) -- deduplicated by case, no spam. For a
PR-backed escalation GitHub's own review notification is the primary channel.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ..models import CodePromotion, Notification, User
from .orm import MaintenanceIncident, MaintenanceKnownGood, RepairArtifact, RepairCase, RepairPlanRevision

DECISIONS = {
    "owner_approval_required": "Review and merge the maintenance PR on GitHub. Merging is the approval: the kernel resumes this case automatically.",
    "release_requires_owner": "The repair is on main. Release it to production through the trusted release gate (docs/PRODUCTION_RUNBOOK.md).",
    "promotion_disabled": "Enable MAINTENANCE_GREEN_PROMOTION_ENABLED and resume the case, or take the prepared branch yourself.",
    "release_disabled": "Enable MAINTENANCE_GREEN_RELEASE_ENABLED on release-control and resume the case, or release through the gate.",
    "rollback_failed": "P0: production may be unhealthy and automatic rollback failed. Restore a known-good deployment; releases are frozen.",
    "runtime_unavailable": "The product is not answering. No code repair is attempted for a runtime outage; check the provider and restore service.",
    "data_invariant": "A money/data invariant is violated. Mutations are frozen; data repair is owner-controlled.",
}


def package(db: Session, case: RepairCase, *, reason: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    inc = db.get(MaintenanceIncident, case.incident_id)
    plan = (
        db.query(RepairPlanRevision)
        .filter(RepairPlanRevision.case_id == case.id)
        .order_by(RepairPlanRevision.revision.desc())
        .first()
    )
    verification = (
        db.query(RepairArtifact)
        .filter(RepairArtifact.case_id == case.id, RepairArtifact.kind == "verification")
        .order_by(RepairArtifact.created_at.desc())
        .first()
    )
    security = (
        db.query(RepairArtifact)
        .filter(RepairArtifact.case_id == case.id, RepairArtifact.kind == "security_review")
        .order_by(RepairArtifact.created_at.desc())
        .first()
    )
    promo = db.get(CodePromotion, case.promotion_id) if case.promotion_id else None
    known_good = db.query(MaintenanceKnownGood).filter(MaintenanceKnownGood.retired_at.is_(None)).order_by(MaintenanceKnownGood.recorded_at.desc()).first()
    v = (verification.content if verification else {}) or {}
    pkg: Dict[str, Any] = {
        "reason": reason,
        "decision": DECISIONS.get(reason.split(":")[0], "Review the case and decide; the system will not act further on its own."),
        "incident": None if inc is None else {
            "id": str(inc.id), "class": inc.incident_class, "priority": inc.priority, "severity": inc.severity,
            "desired_state_ref": inc.desired_state_ref, "target": inc.target, "observations": inc.observation_count,
            "first_observed_at": inc.first_observed_at.isoformat() if inc.first_observed_at else None,
        },
        "root_cause": None if plan is None else {"text": plan.root_cause[:1500], "trust": "model_hypothesis (accepted plan)"},
        "changes": None if plan is None else {"plan_revision": plan.revision, "files_allowed": plan.files_allowed, "changed": v.get("changed"), "diff_digest": v.get("diff_digest"), "head_sha": v.get("head_sha")},
        "tests": None if plan is None else plan.acceptance_tests,
        "qa": (v.get("qa") or {}).get("verdict"),
        "security": ((security.content or {}).get("verdict") if security else None),
        "risk": {"class": case.risk_class, "reasons": ((v.get("risk") or {}).get("reasons") or [])[:10]},
        "staging": (case.facts or {}).get("staging_evidence") or "not run (no release-preview proof for this case)",
        "pr": None if promo is None else {"number": promo.external_pr_number, "url": promo.external_pr_url, "status": getattr(promo.status, "value", promo.status)},
        "rollback_plan": None if known_good is None else {"known_good_production_sha": known_good.production_sha, "deployments": known_good.deployments},
        "attempts": case.attempt_count,
        "plan_revisions": case.current_plan_revision,
        "model_cost_usd": str(case.model_cost_usd),
    }
    if extra:
        pkg.update(extra)
    return pkg


def notify_operators(db: Session, case: RepairCase, pkg: Dict[str, Any]) -> int:
    """One notification per operator per escalated case (idempotent by URL)."""
    url = f"/v1/maintenance/cases/{case.id}"
    inc = pkg.get("incident") or {}
    title = f"Maintenance {inc.get('priority', '')} escalation: {inc.get('desired_state_ref', case.repair_class)}"[:250]
    n = 0
    for user in db.query(User).filter(User.society_role == "operator").all():
        if db.query(Notification).filter(Notification.user_id == user.id, Notification.url == url).first() is not None:
            continue
        db.add(Notification(id=uuid.uuid4(), user_id=user.id, type="maintenance_escalation", title=title, message=str(pkg.get("decision"))[:2000], url=url))
        n += 1
    return n


__all__ = ["package", "notify_operators", "DECISIONS"]
