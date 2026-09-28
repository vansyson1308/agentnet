"""Product SLIs/SLOs and deterministic error-budget accounting (ADR-0010 D13).

Samples are the trusted ``maintenance_observations`` rows (one per contract
item per probe). The error budget of an SLO over its window is
``(1 - target) * total``; ``consumed`` is the number of bad samples. A budget
is only judged once the window holds ``min_samples`` samples, so one noisy
probe on a quiet day cannot freeze the company.

Targets are chosen for the product's stage (a single-region early product on
Railway Hobby behind Cloudflare Free) and documented in
docs/MAINTENANCE_OBSERVABILITY.md. They are trusted constants: no intent,
activity or model output can change them.

Policy (SRE error-budget policy, adapted):

* every availability-class SLO has budget left -> normal GREEN autonomous
  maintenance and normal innovation promotion;
* any availability-class budget exhausted -> innovation/feature promotion is
  frozen and only P0/P1, security repairs and rollbacks may release; P2/P3
  maintenance waits (it is not dropped: its case keeps its deadline).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import case as sa_case
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..society.events import utcnow
from .orm import MaintenanceObservation, RepairCase
from .taxonomy import Priority


@dataclass(frozen=True)
class SLO:
    sli: str
    target: float          # fraction of good samples
    window: timedelta
    min_samples: int
    availability_class: bool  # counts toward the release/innovation freeze
    description: str


SLOS: Dict[str, SLO] = {
    s.sli: s
    for s in (
        SLO("availability", 0.995, timedelta(days=7), 50, True, "apex UI and API health answer as contracted"),
        SLO("api_readiness", 0.995, timedelta(days=7), 50, True, "API readiness probe answers 200"),
        SLO("a2a_discovery", 0.99, timedelta(days=7), 50, True, "A2A Agent Card served and valid"),
        SLO("auth_journey", 0.99, timedelta(days=7), 50, True, "anonymous login/register journeys render and post"),
        SLO("public_pages", 0.98, timedelta(days=7), 100, False, "every other public page matches its contract item"),
        SLO("browser_journey", 0.95, timedelta(days=7), 20, False, "deep-tier browser journeys pass every experience rule"),
    )
}

#: Maintenance repair latency objective: fraction of P0/P1 cases reaching a
#: terminal outcome within the bound (measured, reported; not a release gate).
REPAIR_LATENCY_TARGET = 0.9
REPAIR_LATENCY_BOUND = {Priority.P0.value: timedelta(hours=6), Priority.P1.value: timedelta(hours=24)}


@dataclass
class BudgetState:
    sli: str
    target: float
    total: int
    bad: int
    allowed_bad: float
    remaining_fraction: Optional[float]   # None while below min_samples
    exhausted: bool
    judged: bool
    availability_class: bool

    def as_dict(self) -> dict:
        return {
            "sli": self.sli,
            "target": self.target,
            "total": self.total,
            "bad": self.bad,
            "allowed_bad": round(self.allowed_bad, 3),
            "remaining_fraction": None if self.remaining_fraction is None else round(self.remaining_fraction, 4),
            "exhausted": self.exhausted,
            "judged": self.judged,
            "availability_class": self.availability_class,
        }


def budget_for(db: Session, slo: SLO, *, target: str, now: Optional[datetime] = None) -> BudgetState:
    now = now or utcnow()
    since = now - slo.window
    row = (
        db.query(func.count(MaintenanceObservation.id), func.sum(sa_case((MaintenanceObservation.ok.is_(False), 1), else_=0)))
        .filter(MaintenanceObservation.target == target, MaintenanceObservation.sli == slo.sli, MaintenanceObservation.observed_at >= since, MaintenanceObservation.observed_at <= now)
        .one()
    )
    total, bad = int(row[0] or 0), int(row[1] or 0)
    allowed = (1.0 - slo.target) * total
    judged = total >= slo.min_samples
    if not judged:
        return BudgetState(slo.sli, slo.target, total, bad, allowed, None, False, False, slo.availability_class)
    remaining = 1.0 - (bad / allowed) if allowed > 0 else (1.0 if bad == 0 else -1.0)
    return BudgetState(slo.sli, slo.target, total, bad, allowed, remaining, remaining <= 0.0, True, slo.availability_class)


def budgets(db: Session, *, target: str, now: Optional[datetime] = None) -> List[BudgetState]:
    return [budget_for(db, s, target=target, now=now) for s in SLOS.values()]


def error_budget_exhausted(db: Session, *, target: str, now: Optional[datetime] = None) -> List[str]:
    """Availability-class SLIs whose budget is exhausted (empty = healthy)."""
    return [b.sli for b in budgets(db, target=target, now=now) if b.availability_class and b.exhausted]


def release_allowed_by_budget(db: Session, *, target: str, priority: str, security: bool = False, now: Optional[datetime] = None) -> bool:
    """Deterministic freeze rule: with budget exhausted only P0/P1, security
    repairs (and rollbacks, which never ask) may release."""
    if not error_budget_exhausted(db, target=target, now=now):
        return True
    return security or priority in (Priority.P0.value, Priority.P1.value)


def repair_latency(db: Session, *, now: Optional[datetime] = None, window: timedelta = timedelta(days=30)) -> dict:
    now = now or utcnow()
    rows = (
        db.query(RepairCase.priority, RepairCase.started_at, RepairCase.terminal_at)
        .filter(RepairCase.priority.in_(list(REPAIR_LATENCY_BOUND)), RepairCase.started_at >= now - window)
        .all()
    )
    within = total = 0
    for prio, started, ended in rows:
        total += 1
        if ended is not None and (ended - started) <= REPAIR_LATENCY_BOUND[prio]:
            within += 1
    return {"target": REPAIR_LATENCY_TARGET, "cases": total, "within_bound": within, "ratio": (within / total) if total else None}


__all__ = ["SLO", "SLOS", "BudgetState", "budget_for", "budgets", "error_budget_exhausted", "release_allowed_by_budget", "repair_latency"]
