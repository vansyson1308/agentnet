"""Public-surface observations -> maintenance incidents + SLI samples.

The existing deterministic monitor (``society/surface_monitor.py``) keeps its
job and its events. When ``MAINTENANCE_MONITORING_ENABLED`` is on, every probe
report is ALSO folded into the Maintenance OS here:

* each monitored contract item contributes one SLI sample (ok / not ok);
* each blocking (major/critical) failure becomes a structural Violation
  (class + priority from taxonomy.SURFACE_FAILURE_CLASS, fingerprint from
  target/class/item/failure/path) -- page text never enters;
* each healthy item grows the healthy streak of open incidents on it.

A maintenance incident is NOT a hypothesis and never enters the innovation
portfolio: no Scout, Governor or proposal is involved in covering it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..society import surface as surface_mod
from .config import MaintenanceSettings
from .contracts import load_registry
from .incidents import Violation, ingest_violation, observe_healthy, record_observation
from .taxonomy import CRITICAL_JOURNEY_ITEMS, IncidentClass, Priority, Severity, SURFACE_FAILURE_CLASS, TrustClass, priority_for

COLLECTOR = "surface/1"
SOURCE = "public_surface_monitor"
_BLOCKING = ("major", "critical")


def classify(o: "surface_mod.Observation") -> tuple:
    cls, base = SURFACE_FAILURE_CLASS.get(o.failure or "", (IncidentClass.FUNCTIONAL_CONTRACT, Priority.P2))
    journey = CRITICAL_JOURNEY_ITEMS.get(o.name) if o.kind == "item" else None
    if journey is not None and cls not in (IncidentClass.AVAILABILITY, IncidentClass.SECURITY):
        cls = journey
    sev = Severity(o.severity) if o.severity in ("minor", "major", "critical") else Severity.MAJOR
    return cls, priority_for(cls, sev, base), sev


def ingest_report(db: Session, settings: MaintenanceSettings, report: "surface_mod.SurfaceReport", *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Fold one monitor report into the Maintenance OS. Flushes; the caller commits."""
    contract = load_registry().get("public_surface")
    target = settings.target
    opened: List[str] = []
    recovered: List[str] = []
    for o in report.observations:
        sli = contract.sli_for_item(o.name)
        if o.ok:
            for inc in observe_healthy(db, settings, target=target, desired_state_ref=o.name, sli=sli, source=SOURCE, collector_version=COLLECTOR, now=now):
                recovered.append(str(inc.id))
            continue
        if o.severity not in _BLOCKING:
            record_observation(db, sli=sli, target=target, source=SOURCE, collector_version=COLLECTOR, trust_class=TrustClass.TRUSTED_PROBE, ok=False,
                               payload={"ref": o.name, "failure": o.failure, "severity": o.severity}, observed_at=now)
            continue
        cls, prio, sev = classify(o)
        safe_path = o.path if (o.path and surface_mod.SAFE_PATH.match(o.path)) else None
        payload = {
            "ref": o.name,
            "kind": o.kind,
            "failure": o.failure,
            "severity": o.severity,
            "initial_status": o.initial_status,
            "final_status": o.final_status,
            "final_path": o.final_path if (o.final_path and surface_mod.SAFE_PATH.match(o.final_path)) else None,
            "expected_final_path": o.expected_final_path,
            "markers_missing": list(o.markers_missing)[:10],
        }
        inc, created = ingest_violation(
            db, settings,
            Violation(target=target, incident_class=cls, desired_state_ref=o.name, failure=o.failure or "unknown", severity=sev, base_priority=prio,
                      source=SOURCE, collector_version=COLLECTOR, sli=sli, path=safe_path, payload=payload),
            now=now,
        )
        if created:
            inc.provenance = {"contract": "public_surface", "failure": o.failure, "item": o.name}
            opened.append(str(inc.id))
    db.flush()
    return {"opened": opened, "recovered": recovered}


__all__ = ["ingest_report", "classify", "COLLECTOR", "SOURCE"]
