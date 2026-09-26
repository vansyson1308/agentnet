"""The Society's eyes on the public product: a trusted synthetic monitor.

Every ``SOCIETY_PUBLIC_SURFACE_MONITOR_INTERVAL_SECONDS`` the staging
society-worker checks the PUBLIC product (``PUBLIC_PRODUCT_UI_ORIGIN`` /
``PUBLIC_PRODUCT_API_ORIGIN``, normally production) against the ONE
public-surface contract, with deterministic HTTP only (``surface.py``). No
model is ever asked whether a page looks healthy; no credential, database or
Cloudflare access is involved; it only GETs anonymous public URLs.

What turns an observation into Society work:

* a failure must be ``major`` or ``critical`` (a ``minor`` asset issue never
  wakes anyone) and must repeat on ``SOCIETY_PUBLIC_SURFACE_FAILURE_THRESHOLD``
  consecutive checks (>= 2: one timeout is noise);
* one typed, system-authored ``public.surface.anomaly`` event is emitted per
  distinct failure set per ``SOCIETY_PUBLIC_SURFACE_COOLDOWN_SECONDS`` bucket
  (idempotency key), and never more than
  ``SOCIETY_PUBLIC_SURFACE_MAX_EVENTS_PER_DAY`` per UTC day;
* once every major/critical item has passed ``threshold`` checks in a row
  after an anomaly, exactly one ``public.surface.recovered`` event is emitted
  for that anomaly.

The payload is STRUCTURAL: contract item, path, statuses, final path, missing
contract markers, failure class, latency, consecutive count. Page content is
never read into it -- the website is untrusted input and must not become a
prompt. Link paths are included only if they match ``surface.SAFE_PATH``.

Availability failures (the apex or the API not answering, 5xx) on a critical
item also open an incident freeze (operator-lifted) so no autonomous merge
lands while the product is down. A wrong or missing page does NOT freeze:
its repair is itself a merge, and every normal gate still applies to it.

A monitor failure is logged and counted; it never raises into the worker.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import IncidentFreeze, SocietyEvent
from . import surface as surface_mod
from .config import SocietySettings
from .events import EventType, emit_event, utcnow

logger = logging.getLogger(__name__)

SOURCE = "public_surface_monitor"
INCIDENT_SOURCE = "public_surface"
MAX_FAILING_IN_EVENT = 20
_BLOCKING = ("major", "critical")
_SAFE_NAME = re.compile(r"[A-Za-z0-9_:./~\-]{1,160}")


def _metric(name: str, doc: str, kind: str = "counter", labels=()):
    try:
        from prometheus_client import Counter, Gauge  # noqa: PLC0415

        cls = Counter if kind == "counter" else Gauge
        return cls(name, doc, list(labels))
    except Exception:  # noqa: BLE001 -- metrics are optional (tests, duplicate registration)
        return None


M_CHECKS = _metric("society_public_surface_checks_total", "Public-surface checks run, by result", labels=("result",))
M_FAILING = _metric("society_public_surface_failing", "Failing public-surface observations in the last check, by severity", kind="gauge", labels=("severity",))
M_ERRORS = _metric("society_public_surface_monitor_errors_total", "Public-surface monitor runs that raised (bounded, never fatal)")


def _inc(m, **labels) -> None:
    if m is None:
        return
    (m.labels(**labels) if labels else m).inc()


@dataclass
class MonitorOutcome:
    ran: bool = False
    failing: int = 0
    durable: int = 0
    anomaly_event_id: Optional[str] = None
    recovered_event_id: Optional[str] = None
    incident_opened: bool = False
    skipped_reason: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SurfaceMonitor:
    """In-memory debounce state + event emission. The consecutive counts live
    in memory on purpose: after a restart the monitor must see a failure
    ``threshold`` times again before it speaks, which errs on the quiet side.
    Whether an anomaly is still open is read from the persisted events."""

    runner: Callable[..., "surface_mod.SurfaceReport"] = surface_mod.run_contract
    consecutive: Dict[str, int] = field(default_factory=dict)
    healthy_streak: int = 0
    first_failed_at: Dict[str, str] = field(default_factory=dict)
    last_run_at: Optional[datetime] = None
    last_summary: Optional[Dict[str, Any]] = None

    # ── scheduling ───────────────────────────────────────────────────
    def due(self, settings: SocietySettings, now: Optional[datetime] = None) -> bool:
        if not settings.public_surface_monitor_enabled:
            return False
        now = now or utcnow()
        return self.last_run_at is None or (now - self.last_run_at).total_seconds() >= settings.public_surface_monitor_interval_seconds

    def probe(self, settings: SocietySettings) -> "surface_mod.SurfaceReport":
        """The network part (run in a thread by the worker). Deterministic HTTP only."""
        origins = {"ui": settings.public_product_ui_origin, "api": settings.public_product_api_origin}
        return self.runner(origins, only_monitored=True, timeout=float(settings.public_surface_timeout_seconds))

    # ── evaluation (DB) ──────────────────────────────────────────────
    def observe(self, db: Session, settings: SocietySettings, report: "surface_mod.SurfaceReport", *, now: Optional[datetime] = None) -> MonitorOutcome:
        """Fold one report into the debounce state and emit at most one
        anomaly and at most one recovery event. Commits when it emitted."""
        now = now or utcnow()
        self.last_run_at = now
        out = MonitorOutcome(ran=True)
        threshold = settings.public_surface_failure_threshold
        blocking = [o for o in report.observations if not o.ok and o.severity in _BLOCKING]
        out.failing = len(blocking)
        current = {o.name for o in blocking}
        for name in list(self.consecutive):
            if name not in current:
                self.consecutive.pop(name, None)
                self.first_failed_at.pop(name, None)
        for o in blocking:
            self.consecutive[o.name] = self.consecutive.get(o.name, 0) + 1
            self.first_failed_at.setdefault(o.name, report.checked_at)
        self.healthy_streak = self.healthy_streak + 1 if not blocking else 0
        self.last_summary = {**report.summary(), "checked_at": report.checked_at}
        _inc(M_CHECKS, result="fail" if blocking else "ok")
        if M_FAILING is not None:
            for sev in surface_mod.SEVERITIES:
                M_FAILING.labels(severity=sev).set(sum(1 for o in report.failures() if o.severity == sev))

        durable = [o for o in blocking if self.consecutive.get(o.name, 0) >= threshold]
        out.durable = len(durable)
        open_anomaly = latest_open_anomaly(db)
        if durable:
            ev = self._emit_anomaly(db, settings, report, durable, now=now, out=out)
            if ev is not None:
                out.anomaly_event_id = str(ev.id)
        elif open_anomaly is not None and self.healthy_streak >= threshold:
            ev = emit_event(
                db,
                event_type=EventType.PUBLIC_SURFACE_RECOVERED,
                payload={
                    "source": SOURCE,
                    "anomaly_event_id": str(open_anomaly.id),
                    "previously_failing": [f.get("name") for f in (open_anomaly.payload or {}).get("failing", [])][:MAX_FAILING_IN_EVENT],
                    "anomaly_observed_at": (open_anomaly.payload or {}).get("observed_at"),
                    "recovered_at": report.checked_at,
                    "healthy_checks": self.healthy_streak,
                    "checked": report.summary()["checked"],
                    "target": settings.public_surface_target_label,
                },
                actor_type="system",
                subject_type="society_event",
                subject_id=open_anomaly.id,
                correlation_id=open_anomaly.correlation_id,
                idempotency_key=f"public-surface-recovered:{open_anomaly.id}",
            )
            if not getattr(ev, "deduplicated", False):
                out.recovered_event_id = str(ev.id)
                db.commit()
        return out

    def _emit_anomaly(self, db: Session, settings: SocietySettings, report, durable, *, now: datetime, out: MonitorOutcome) -> Optional[SocietyEvent]:
        failing = sorted(durable, key=lambda o: (-_rank(o.severity), o.name))[:MAX_FAILING_IN_EVENT]
        signature = hashlib.sha256("|".join(sorted(f"{o.name}:{o.failure}" for o in durable)).encode()).hexdigest()[:16]
        bucket = int(now.timestamp()) // max(1, settings.public_surface_cooldown_seconds)
        key = f"public-surface:{signature}:{bucket}"
        existing = db.query(SocietyEvent).filter(SocietyEvent.idempotency_key == key).first()
        if existing is not None:
            out.skipped_reason = "cooldown"
            return None
        if anomalies_today(db, now) >= settings.public_surface_max_events_per_day:
            out.skipped_reason = "daily_event_cap"
            logger.warning("public surface: daily anomaly event cap reached; evidence kept in metrics only")
            return None
        severity = surface_mod.worst_severity(failing) or "major"
        availability = any(o.failure in surface_mod.AVAILABILITY_FAILURES and o.severity == "critical" for o in failing)
        contract = surface_mod.load_contract()
        payload: Dict[str, Any] = {
            "source": SOURCE,
            "target": settings.public_surface_target_label,
            "origins": dict(report.origins),
            "contract_version": contract.version,
            "contract_file": "services/registry/app/society/public_surface_contract.json",
            "verification_tests": list(contract.verification_tests),
            "product_source": contract.product_source,
            "severity": severity,
            "availability_failure": availability,
            "failing_count": len(durable),
            "checked": report.summary()["checked"],
            "consecutive_failure_threshold": settings.public_surface_failure_threshold,
            "observed_at": report.checked_at,
            "failing": [_structural(o, self.consecutive.get(o.name, 0), self.first_failed_at.get(o.name), contract) for o in failing],
            "evidence_note": "Structural HTTP evidence only (no page content). Expectations come from the trusted contract; a repair changes the product, never the contract or its tests.",
        }
        payload = fit_to_context(payload)
        ev = emit_event(db, event_type=EventType.PUBLIC_SURFACE_ANOMALY, payload=payload, actor_type="system", idempotency_key=key)
        if availability and not _open_surface_incident(db):
            try:
                from .company import open_incident  # noqa: PLC0415 -- avoid an import cycle at module load

                open_incident(db, reason=f"public surface unavailable: {', '.join(o.name for o in failing if o.failure in surface_mod.AVAILABILITY_FAILURES)[:200]}", source=INCIDENT_SOURCE, evidence={"anomaly_event_id": str(ev.id), "signature": signature})
                out.incident_opened = True
            except Exception:  # noqa: BLE001 -- the event is the primary evidence
                logger.exception("public surface: could not open the incident freeze")
        db.commit()
        return ev


#: Per-item fields dropped first when the payload must shrink (the model
#: keeps what failed, where it ended and what the contract expected).
_VERBOSE_FIELDS = ("detail", "intent", "latency_ms", "first_failed_at", "linked_from", "kind")


def _canonical_size(obj: Any) -> int:
    """Length of the canonical JSON the context builder measures."""
    return len(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False))


def fit_to_context(payload: Dict[str, Any], limit: Optional[int] = None) -> Dict[str, Any]:
    """Keep the payload an OBJECT for the model. context.py shows an event
    payload as structured JSON only while its canonical form fits ``TXT_LONG``;
    past that it becomes a string preview cut mid-structure. Shrink in order:
    drop empty values, then verbose per-item fields, then the least severe
    items (the payload says how many were omitted; ``failing_count`` keeps
    the true total)."""
    from .context import TXT_LONG  # noqa: PLC0415 -- context imports heavy modules

    limit = limit or TXT_LONG
    p = dict(payload)
    p["failing"] = [{k: v for k, v in f.items() if v not in (None, [], "")} for f in p.get("failing", [])]
    if _canonical_size(p) <= limit:
        return p
    p["failing"] = [{k: v for k, v in f.items() if k not in _VERBOSE_FIELDS} for f in p["failing"]]
    p["evidence_note"] = "Structural HTTP evidence only; expectations come from the trusted contract."
    while _canonical_size(p) > limit and len(p["failing"]) > 1:
        p["failing"] = p["failing"][:-1]
        p["omitted_failures"] = int(p.get("failing_count", 0)) - len(p["failing"])
    return p


def _rank(sev: str) -> int:
    return surface_mod.SEVERITIES.index(sev) if sev in surface_mod.SEVERITIES else 0


def _structural(o: "surface_mod.Observation", consecutive: int, first_failed_at: Optional[str], contract) -> Dict[str, Any]:
    safe = surface_mod.SAFE_PATH
    item_intent = ""
    try:
        item_intent = contract.item(o.name).intent
    except KeyError:
        pass
    return {
        "name": o.name if _SAFE_NAME.fullmatch(o.name) else o.kind,
        "kind": o.kind,
        "path": o.path if (o.path and safe.match(o.path)) else None,
        "severity": o.severity,
        "failure": o.failure,
        "initial_status": o.initial_status,
        "final_status": o.final_status,
        "final_path": o.final_path if (o.final_path and safe.match(o.final_path)) else None,
        "expected_final_path": o.expected_final_path,
        "markers_missing": list(o.markers_missing),
        "latency_ms": o.latency_ms,
        "linked_from": o.source,
        "consecutive_failures": consecutive,
        "first_failed_at": first_failed_at,
        "detail": o.detail[:160],
        "intent": item_intent,
    }


def latest_open_anomaly(db: Session) -> Optional[SocietyEvent]:
    """The newest anomaly event, if no recovery event was emitted after it."""
    last_anomaly = (
        db.query(SocietyEvent)
        .filter(SocietyEvent.event_type == EventType.PUBLIC_SURFACE_ANOMALY)
        .order_by(SocietyEvent.created_at.desc())
        .first()
    )
    if last_anomaly is None:
        return None
    last_recovered = (
        db.query(SocietyEvent)
        .filter(SocietyEvent.event_type == EventType.PUBLIC_SURFACE_RECOVERED, SocietyEvent.created_at >= last_anomaly.created_at)
        .first()
    )
    return None if last_recovered is not None else last_anomaly


def anomalies_today(db: Session, now: Optional[datetime] = None) -> int:
    now = now or utcnow()
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        db.query(SocietyEvent)
        .filter(SocietyEvent.event_type == EventType.PUBLIC_SURFACE_ANOMALY, SocietyEvent.created_at >= start)
        .count()
    )


def _open_surface_incident(db: Session) -> bool:
    return db.query(IncidentFreeze).filter(IncidentFreeze.lifted_at.is_(None), IncidentFreeze.source == INCIDENT_SOURCE).first() is not None


def surface_status(db: Session, monitor: Optional[SurfaceMonitor] = None, *, recent: int = 5) -> Dict[str, Any]:
    """Operator view: open anomaly, recent anomaly/recovery events (structural
    payloads only) and, when called in the worker, the last check summary."""
    rows = (
        db.query(SocietyEvent)
        .filter(SocietyEvent.event_type.in_([EventType.PUBLIC_SURFACE_ANOMALY, EventType.PUBLIC_SURFACE_RECOVERED]))
        .order_by(SocietyEvent.created_at.desc())
        .limit(max(1, min(recent, 20)))
        .all()
    )
    open_ev = latest_open_anomaly(db)
    return {
        "open_anomaly": None if open_ev is None else {
            "event_id": str(open_ev.id),
            "severity": (open_ev.payload or {}).get("severity"),
            "failing": [{k: f.get(k) for k in ("name", "failure", "severity", "path")} for f in (open_ev.payload or {}).get("failing", [])],
            "observed_at": (open_ev.payload or {}).get("observed_at"),
            "correlation_id": str(open_ev.correlation_id) if open_ev.correlation_id else None,
        },
        "recent_events": [
            {"event_id": str(r.id), "type": r.event_type, "created_at": r.created_at.isoformat() if r.created_at else None,
             "severity": (r.payload or {}).get("severity"), "failing_count": (r.payload or {}).get("failing_count")}
            for r in rows
        ],
        "last_check": monitor.last_summary if monitor else None,
    }


def self_healing_workstream(db: Session, correlation_id) -> Dict[str, Any]:
    """What the Society is doing about one anomaly (its correlation): runs by
    role and status, candidates with QA/Security verdicts and risk tier, and
    their promotions. Facts from durable rows only; no model prose."""
    from sqlalchemy import func  # noqa: PLC0415

    from ..models import Agent, AgentRun, CodeCandidate, CodePromotion  # noqa: PLC0415

    if correlation_id is None:
        return {}
    runs = (
        db.query(Agent.name, AgentRun.status, func.count(AgentRun.id))
        .join(Agent, Agent.id == AgentRun.agent_id)
        .filter(AgentRun.correlation_id == correlation_id)
        .group_by(Agent.name, AgentRun.status)
        .all()
    )
    cands = db.query(CodeCandidate).filter(CodeCandidate.correlation_id == correlation_id).order_by(CodeCandidate.created_at).limit(10).all()
    out_c = []
    for c in cands:
        promos = db.query(CodePromotion).filter(CodePromotion.candidate_id == c.id).order_by(CodePromotion.created_at).all()
        out_c.append({
            "candidate_id": str(c.id),
            "status": getattr(c.status, "value", c.status),
            "risk_tier": c.risk_tier,
            "qa": (c.qa_report or {}).get("verdict"),
            "security": (c.security_report or {}).get("verdict"),
            "promotions": [{"status": getattr(p.status, "value", p.status), "ci": p.ci_state, "risk_tier": p.risk_tier, "pr": p.external_pr_number} for p in promos],
        })
    return {
        "correlation_id": str(correlation_id),
        "runs": [{"agent": n, "status": getattr(st, "value", st), "count": int(k)} for n, st, k in runs],
        "candidates": out_c,
    }


__all__ = ["SurfaceMonitor", "self_healing_workstream", "MonitorOutcome", "latest_open_anomaly", "anomalies_today", "surface_status", "SOURCE", "INCIDENT_SOURCE"]
