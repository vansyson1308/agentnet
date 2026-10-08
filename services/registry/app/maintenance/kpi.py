"""Maintenance KPIs and toil (ADR-0010 D20). Computed from durable rows only.

Not optimised for PR volume: the numbers that matter are how fast a real
violation is detected and ends, how often it ends without a human, and how
much human toil normal GREEN maintenance still costs (target: zero).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..society.events import utcnow
from . import state_machine as sm
from .orm import MaintenanceIncident, MaintenanceToilEvent, RepairCase, RepairTransition

TOIL_KINDS = ("manual_merge", "manual_release", "manual_candidate_abandon", "manual_memory_correction", "manual_repair", "owner_approval", "owner_refusal", "freeze_lift")


def record_toil(db: Session, kind: str, *, actor: str, case_id=None, detail: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> None:
    if kind not in TOIL_KINDS:
        raise ValueError(f"unknown toil kind {kind!r}")
    db.add(MaintenanceToilEvent(id=uuid.uuid4(), kind=kind, case_id=case_id, actor=str(actor)[:128], detail=detail or {}, created_at=now or utcnow()))


def _avg(seconds) -> Optional[float]:
    seconds = [s for s in seconds if s is not None]
    return round(sum(seconds) / len(seconds), 1) if seconds else None


def kpis(db: Session, *, now: Optional[datetime] = None, window: timedelta = timedelta(days=30)) -> Dict[str, Any]:
    now = now or utcnow()
    since = now - window
    cases = db.query(RepairCase).filter(RepairCase.started_at >= since).all()
    terminal = [c for c in cases if c.state in {s.value for s in sm.TERMINAL}]
    by_outcome: Dict[str, int] = {}
    for c in terminal:
        by_outcome[c.state] = by_outcome.get(c.state, 0) + 1
    n = len(terminal) or None
    incidents = {i.id: i for i in db.query(MaintenanceIncident).filter(MaintenanceIncident.opened_at >= since).all()}
    confirmed = dict(
        db.query(RepairTransition.case_id, func.min(RepairTransition.created_at))
        .filter(RepairTransition.to_state == sm.CaseState.CONFIRMED.value, RepairTransition.created_at >= since)
        .group_by(RepairTransition.case_id)
        .all()
    )
    mttd, mttr = [], []
    for c in cases:
        inc = incidents.get(c.incident_id)
        if inc is None:
            continue
        if c.id in confirmed:
            mttd.append((confirmed[c.id] - inc.first_observed_at).total_seconds())
        if c.state in (sm.CaseState.AUTO_REPAIRED.value, sm.CaseState.AUTO_ROLLED_BACK.value) and c.terminal_at:
            mttr.append((c.terminal_at - inc.first_observed_at).total_seconds())
    repaired = by_outcome.get(sm.CaseState.AUTO_REPAIRED.value, 0)
    cost = sum((Decimal(str(c.model_cost_usd or 0)) for c in cases), Decimal("0"))
    toil = dict(db.query(MaintenanceToilEvent.kind, func.count(MaintenanceToilEvent.id)).filter(MaintenanceToilEvent.created_at >= since).group_by(MaintenanceToilEvent.kind).all())
    all_inc = len(incidents)
    return {
        "window_days": window.days,
        "cases": len(cases),
        "terminal": len(terminal),
        "outcomes": by_outcome,
        "mttd_seconds": _avg(mttd),
        "mttr_seconds": _avg(mttr),
        "auto_repair_rate": (repaired / n) if n else None,
        "rollback_rate": (by_outcome.get(sm.CaseState.AUTO_ROLLED_BACK.value, 0) / n) if n else None,
        "escalation_rate": (by_outcome.get(sm.CaseState.SAFELY_ESCALATED.value, 0) / n) if n else None,
        "false_positive_rate": (by_outcome.get(sm.CaseState.CANNOT_REPRODUCE.value, 0) / n) if n else None,
        "repair_attempts_per_incident": _avg([c.attempt_count for c in cases]),
        "model_cost_usd": str(cost),
        "model_cost_per_resolved_incident": str((cost / repaired).quantize(Decimal("0.000001"))) if repaired else None,
        "recurrence_rate": (sum(1 for i in incidents.values() if i.recurrence_of) / all_inc) if all_inc else None,
        "toil": {k: int(toil.get(k, 0)) for k in TOIL_KINDS},
    }


__all__ = ["kpis", "record_toil", "TOIL_KINDS"]
