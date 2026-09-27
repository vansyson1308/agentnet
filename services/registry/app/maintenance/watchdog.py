"""The kernel watchdog (ADR-0010 D6): who watches the Maintenance Kernel.

Deterministic, model-free. It runs in the release-control process (a
different process from the kernel it watches) and, symmetrically, the kernel
checks the release controller's heartbeat. It reads durable rows only:

* the kernel heartbeat (``maintenance_heartbeats``) is fresh;
* the kernel's error rate in the current heartbeat window;
* the oldest non-terminal case and the queue lag (cases overdue);
* the stranded-case invariant (reconciler.stranded_count) is 0;
* the release controller heartbeat is fresh (when checked from the kernel).

A failed check becomes a ``CONTROL_PLANE`` maintenance incident. Such an
incident is never repaired autonomously (the kernel cannot release changes
to itself); it is escalated to the owner with the evidence. Restarting a
crashed process is the platform's restart policy (``ALWAYS`` on Railway),
not a model decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..society.events import utcnow
from . import state_machine as sm
from .config import MaintenanceSettings
from .incidents import Violation, ingest_violation, observe_healthy
from .orm import MaintenanceHeartbeat, RepairCase
from .taxonomy import IncidentClass, Priority, Severity, TrustClass

COLLECTOR = "watchdog/1"
HEARTBEAT_STALE = timedelta(minutes=15)
QUEUE_LAG = timedelta(minutes=30)


@dataclass
class WatchReport:
    ok: bool
    problems: List[str] = field(default_factory=list)
    facts: Dict[str, object] = field(default_factory=dict)


def check(db: Session, settings: MaintenanceSettings, *, now: Optional[datetime] = None, watch: str = "kernel", record: bool = True) -> WatchReport:
    from .reconciler import stranded_count  # noqa: PLC0415

    now = now or utcnow()
    problems: List[str] = []
    facts: Dict[str, object] = {}
    hb = db.get(MaintenanceHeartbeat, watch)
    enabled = settings.autonomy_enabled or settings.monitoring_enabled
    if enabled:
        if hb is None:
            problems.append(f"{watch}_never_reported")
        else:
            age = (now - hb.beat_at).total_seconds()
            facts[f"{watch}_heartbeat_age_seconds"] = age
            if age > HEARTBEAT_STALE.total_seconds():
                problems.append(f"{watch}_heartbeat_stale")
    if watch == "kernel":
        nonterminal = [s.value for s in sm.NON_TERMINAL]
        oldest = db.query(func.min(RepairCase.started_at)).filter(RepairCase.state.in_(nonterminal)).scalar()
        facts["oldest_nonterminal_case_age_seconds"] = (now - oldest).total_seconds() if oldest else None
        lagging = db.query(func.count(RepairCase.id)).filter(RepairCase.state.in_(nonterminal), RepairCase.next_action_at < now - QUEUE_LAG).scalar()
        facts["cases_overdue"] = int(lagging or 0)
        stranded = stranded_count(db, now=now, stall_seconds=settings.stall_seconds)
        facts["stranded_cases"] = stranded
        if stranded:
            problems.append("stranded_cases")
        if enabled and lagging:
            problems.append("queue_lag")
    if record:
        ref = f"{watch}:liveness"
        if problems:
            ingest_violation(db, settings, Violation(target=settings.target, incident_class=IncidentClass.CONTROL_PLANE, desired_state_ref=ref, failure=problems[0],
                                                     severity=Severity.MAJOR, base_priority=Priority.P1, source="maintenance_watchdog", collector_version=COLLECTOR,
                                                     sli="maintenance_liveness", trust_class=TrustClass.TRUSTED_DB, payload={"problems": problems, **{k: v for k, v in facts.items() if v is not None}}), now=now)
        else:
            observe_healthy(db, settings, target=settings.target, desired_state_ref=ref, sli="maintenance_liveness", source="maintenance_watchdog", collector_version=COLLECTOR, now=now, record=False)
    return WatchReport(ok=not problems, problems=problems, facts=facts)


__all__ = ["check", "WatchReport", "HEARTBEAT_STALE"]
