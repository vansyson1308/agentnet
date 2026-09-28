"""Trusted maintenance knowledge, postmortem facts and the detector question.

When a case reaches a terminal outcome, one ``maintenance_knowledge`` row is
derived from durable rows -- never from chain-of-thought and never from
memory. The accepted plan's root cause is kept, labelled as the hypothesis it
is. Every row answers the two "fix the detector" questions deterministically:

* why did pre-release gates not catch this?  -- are the contract's
  verification tests collected by required CI (scripts/ci/check_test_discovery.py)?
* why did runtime detection not catch it earlier? -- which collector found
  it, and how long after the defect's first observation did a case open?

This knowledge is context for future diagnoses of the same fingerprint. It
can never suppress, cover or resolve an incident (ADR-0010 D5).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..society.events import utcnow
from .contracts import load_registry
from .orm import MaintenanceIncident, MaintenanceKnowledge, MaintenanceRelease, RepairArtifact, RepairCase, RepairPlanRevision, RepairTransition

#: Test files deliberately held out of required CI (mirrors the sentinel's
#: HELD classification). A contract whose verification tests are here had no
#: pre-release gate -- that is a detector gap, recorded as such.
def _held_test_files() -> List[str]:
    try:
        import importlib.util
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[4]
        spec = importlib.util.spec_from_file_location("check_test_discovery", root / "scripts" / "ci" / "check_test_discovery.py")
        if spec is None or spec.loader is None:
            return []
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return list(getattr(mod, "HELD", {}).keys())
    except Exception:  # noqa: BLE001 -- images do not ship scripts/; the gap is then "unknown"
        return []


def detector_gap(db: Session, case: RepairCase, incident: Optional[MaintenanceIncident]) -> Dict[str, Any]:
    if incident is None:
        return {}
    contract_id = (incident.provenance or {}).get("contract") or "public_surface"
    try:
        contract = load_registry().get(contract_id)
        tests = list(contract.verification_tests)
    except KeyError:
        tests = []
    held = _held_test_files()
    import fnmatch

    not_gated = [t for t in tests if any(fnmatch.fnmatch(t, h) for h in held)]
    opened = case.started_at
    lag = (opened - incident.first_observed_at).total_seconds() if (opened and incident.first_observed_at) else None
    return {
        "pre_release": ("verification tests not in required CI: " + ", ".join(not_gated)) if not_gated else ("contract verification tests are required CI gates" if tests else "no verification test named for the contract"),
        "runtime": {"collector": incident.source, "first_observed_at": incident.first_observed_at.isoformat() if incident.first_observed_at else None, "case_open_lag_seconds": lag},
        "prevention_required": bool(not_gated) or not tests,
    }


def record_terminal(db: Session, case: RepairCase, *, now: Optional[datetime] = None) -> Optional[MaintenanceKnowledge]:
    if db.query(MaintenanceKnowledge).filter(MaintenanceKnowledge.case_id == case.id).first() is not None:
        return None
    incident = db.get(MaintenanceIncident, case.incident_id)
    plan = db.query(RepairPlanRevision).filter(RepairPlanRevision.case_id == case.id).order_by(RepairPlanRevision.revision.desc()).first()
    patch = db.query(RepairArtifact).filter(RepairArtifact.case_id == case.id, RepairArtifact.kind == "patchset").order_by(RepairArtifact.created_at.desc()).first()
    rel = db.get(MaintenanceRelease, case.release_id) if case.release_id else None
    changed = ((patch.content or {}).get("changed") if patch else []) or []
    row = MaintenanceKnowledge(
        id=uuid.uuid4(),
        case_id=case.id,
        incident_fingerprint=incident.fingerprint if incident else "",
        incident_class=case.repair_class,
        outcome=case.state,
        root_cause=(f"[hypothesis, plan r{plan.revision}] " + plan.root_cause)[:4000] if plan else "",
        repair_digest=(patch.content or {}).get("diff_digest") if patch else None,
        tests_added=[f for f in changed if "/tests/" in f"/{f}" or f.startswith("tests/")][:20],
        monitors=[],
        release_result=rel.status if rel else None,
        rollback_result=((rel.rollback or {}).get("result") if rel else None),
        detector_gap=detector_gap(db, case, incident),
        created_at=now or utcnow(),
    )
    try:
        contract = load_registry().get((incident.provenance or {}).get("contract") or "public_surface") if incident else None
        row.monitors = list(contract.detectors) if contract else []
    except KeyError:
        pass
    db.add(row)
    db.flush()
    return row


def prior_knowledge(db: Session, fingerprint: str, limit: int = 3) -> List[Dict[str, Any]]:
    rows = (
        db.query(MaintenanceKnowledge)
        .filter(MaintenanceKnowledge.incident_fingerprint == fingerprint)
        .order_by(MaintenanceKnowledge.created_at.desc())
        .limit(limit)
        .all()
    )
    return [{"outcome": r.outcome, "root_cause": {"trust": "model_hypothesis", "text": (r.root_cause or "")[:600]}, "tests_added": r.tests_added, "release_result": r.release_result} for r in rows]


def postmortem_facts(db: Session, case: RepairCase) -> Dict[str, Any]:
    """Deterministic postmortem facts (impact, timeline, detection, repair,
    detector gap). A DraftPostmortem activity may explain them; it never
    edits them."""
    incident = db.get(MaintenanceIncident, case.incident_id)
    transitions = db.query(RepairTransition).filter(RepairTransition.case_id == case.id).order_by(RepairTransition.id).all()
    k = db.query(MaintenanceKnowledge).filter(MaintenanceKnowledge.case_id == case.id).first()
    end = case.terminal_at or utcnow()
    return {
        "impact": None if incident is None else {
            "class": incident.incident_class, "priority": incident.priority, "surfaces": incident.affected_surfaces, "observations": incident.observation_count,
            "duration_seconds": ((incident.resolved_at or end) - incident.first_observed_at).total_seconds() if incident.first_observed_at else None,
        },
        "timeline": [{"at": t.created_at.isoformat() if t.created_at else None, "from": t.from_state, "to": t.to_state, "actor": t.actor_type, "reason": t.reason_code} for t in transitions][:200],
        "detection": None if incident is None else {"source": incident.source, "first_observed_at": incident.first_observed_at.isoformat() if incident.first_observed_at else None},
        "root_cause": {"trust": "model_hypothesis", "text": k.root_cause if k else None},
        "repair": None if k is None else {"digest": k.repair_digest, "tests_added": k.tests_added, "release": k.release_result, "rollback": k.rollback_result},
        "why_gates_missed": (k.detector_gap if k else {}),
        "outcome": case.state,
        "blameless": True,
    }


__all__ = ["record_terminal", "prior_knowledge", "postmortem_facts", "detector_gap"]
