"""Maintenance incidents: trusted observations -> durable violations.

Deduplication is ``fingerprint`` + the partial unique index on OPEN incidents
(and, for repair work, the one-active-case index). Model memory plays no part
in whether an incident exists, is covered, or is resolved (ADR-0010 D5).

Lifecycle:

* a failing observation opens the incident (or bumps the open one);
* a recurrence of a fingerprint that was ``recovered`` opens a NEW incident
  linked to the previous one (``recurrence_of``), and a fingerprint that keeps
  coming back is escalated one priority level;
* healthy observations of the same desired-state reference grow a
  ``healthy_streak``; ``recovery_streak`` of them in a row -- and no release of
  its own case still settling -- mark it ``recovered``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import IncidentFreeze
from ..society.events import utcnow
from . import state_machine as sm
from .config import MaintenanceSettings
from .fingerprint import fingerprint as make_fingerprint, normalize_path
from .ledger import digest
from .orm import MaintenanceIncident, MaintenanceObservation, RepairCase
from .taxonomy import IncidentClass, IncidentStatus, Priority, PRIORITY_RANK, Severity, TrustClass, TRUSTED_EVIDENCE

#: Classes whose incident freezes autonomous PRODUCT promotion (the repair
#: itself still passes through the narrow repair exception, policy.py).
FREEZING_CLASSES = frozenset({IncidentClass.AVAILABILITY, IncidentClass.SECURITY, IncidentClass.ECONOMIC_INVARIANT, IncidentClass.DATA_INVARIANT})
FREEZE_SOURCE = "maintenance"
RECURRENCE_WINDOW = timedelta(days=7)

#: Case states in which a release of the incident's own repair is settling:
#: recovery is not declared from the monitor alone while the release is in flight.
_SETTLING = frozenset({sm.CaseState.READY_FOR_RELEASE.value, sm.CaseState.RELEASING.value, sm.CaseState.RECOVERY_PENDING.value})


@dataclass
class Violation:
    """One structural observation of a violated desired state."""

    target: str
    incident_class: IncidentClass
    desired_state_ref: str
    failure: str
    severity: Severity
    base_priority: Priority
    source: str
    collector_version: str
    sli: str
    path: Optional[str] = None
    trust_class: TrustClass = TrustClass.TRUSTED_PROBE
    payload: Optional[Dict[str, Any]] = None

    @property
    def fingerprint(self) -> str:
        return make_fingerprint(target=self.target, incident_class=self.incident_class, desired_state_ref=self.desired_state_ref, failure=self.failure, path=self.path)


def _bump(p: Priority) -> Priority:
    rank = max(0, PRIORITY_RANK[p] - 1)
    return [q for q, r in PRIORITY_RANK.items() if r == rank][0]


def record_observation(
    db: Session,
    *,
    sli: str,
    target: str,
    source: str,
    collector_version: str,
    trust_class: TrustClass,
    ok: bool,
    payload: Dict[str, Any],
    fingerprint: Optional[str] = None,
    incident_id: Optional[uuid.UUID] = None,
    observed_at: Optional[datetime] = None,
) -> MaintenanceObservation:
    if trust_class not in TRUSTED_EVIDENCE:
        raise ValueError("only trusted collectors record observations")
    obs = MaintenanceObservation(
        sli=sli[:64],
        target=target[:32],
        source=source[:64],
        collector_version=collector_version[:32],
        trust_class=trust_class.value,
        ok=bool(ok),
        fingerprint=fingerprint,
        incident_id=incident_id,
        digest=digest(payload),
        payload=payload,
        observed_at=observed_at or utcnow(),
    )
    db.add(obs)
    return obs


def ingest_violation(db: Session, settings: MaintenanceSettings, v: Violation, *, now: Optional[datetime] = None) -> Tuple[MaintenanceIncident, bool]:
    """Fold one failing observation into incident state. Returns (incident,
    created). Flushes, never commits."""
    now = now or utcnow()
    fp = v.fingerprint
    payload = dict(v.payload or {})
    payload.setdefault("failure", v.failure)
    payload.setdefault("path", normalize_path(v.path) if v.path else None)
    ev_digest = digest(payload)
    inc = db.query(MaintenanceIncident).filter(MaintenanceIncident.fingerprint == fp, MaintenanceIncident.status == IncidentStatus.OPEN.value).with_for_update().first()
    created = False
    if inc is None:
        previous = (
            db.query(MaintenanceIncident)
            .filter(MaintenanceIncident.fingerprint == fp, MaintenanceIncident.status != IncidentStatus.OPEN.value)
            .order_by(MaintenanceIncident.opened_at.desc())
            .first()
        )
        priority = v.base_priority
        recurrence_count = 0
        recurrence_of = None
        if previous is not None:
            recurrence_of = previous.id
            recurrence_count = int(previous.recurrence_count or 0) + 1
            recent = previous.resolved_at is not None and (now - previous.resolved_at) <= RECURRENCE_WINDOW
            if recent and recurrence_count >= 2:
                priority = _bump(Priority(previous.priority))  # it keeps coming back: fix the systemic cause first
            elif PRIORITY_RANK[Priority(previous.priority)] < PRIORITY_RANK[priority]:
                priority = Priority(previous.priority)
        inc = MaintenanceIncident(
            id=uuid.uuid4(),
            fingerprint=fp,
            incident_class=v.incident_class.value,
            priority=priority.value,
            severity=v.severity.value,
            source=v.source[:64],
            target=v.target[:32],
            desired_state_ref=v.desired_state_ref[:160],
            affected_surfaces=[s for s in [normalize_path(v.path) if v.path else None, v.desired_state_ref] if s],
            status=IncidentStatus.OPEN.value,
            first_observed_at=now,
            last_observed_at=now,
            observation_count=1,
            healthy_streak=0,
            current_evidence_digest=ev_digest,
            recurrence_of=recurrence_of,
            recurrence_count=recurrence_count,
            case_count=0,
            provenance={},
            opened_at=now,
        )
        sp = db.begin_nested()
        try:
            db.add(inc)
            db.flush()
            sp.commit()
            created = True
        except IntegrityError:
            # a concurrent collector opened it first: fold into theirs
            sp.rollback()
            inc = db.query(MaintenanceIncident).filter(MaintenanceIncident.fingerprint == fp, MaintenanceIncident.status == IncidentStatus.OPEN.value).with_for_update().one()
    if not created:
        inc.last_observed_at = now
        inc.observation_count = int(inc.observation_count or 0) + 1
        inc.healthy_streak = 0
        inc.current_evidence_digest = ev_digest
        if Severity(v.severity) is Severity.CRITICAL and inc.severity != Severity.CRITICAL.value:
            inc.severity = Severity.CRITICAL.value
    record_observation(
        db, sli=v.sli, target=v.target, source=v.source, collector_version=v.collector_version, trust_class=v.trust_class,
        ok=False, payload=payload, fingerprint=fp, incident_id=inc.id, observed_at=now,
    )
    if IncidentClass(inc.incident_class) in FREEZING_CLASSES and Severity(inc.severity) is not Severity.MINOR:
        ensure_incident_freeze(db, inc, now=now)
    db.flush()
    return inc, created


def ensure_incident_freeze(db: Session, inc: MaintenanceIncident, *, now: Optional[datetime] = None) -> Optional[IncidentFreeze]:
    """Availability/security/money incidents freeze autonomous product
    promotion (operator-lifted, ADR-0009 D15). The repair for THIS incident
    passes through policy.repair_exception; nothing else does."""
    for fr in db.query(IncidentFreeze).filter(IncidentFreeze.lifted_at.is_(None), IncidentFreeze.source == FREEZE_SOURCE).all():
        if (fr.evidence or {}).get("incident_id") == str(inc.id):
            return fr
    fr = IncidentFreeze(
        id=uuid.uuid4(),
        reason=f"maintenance incident {inc.incident_class} {inc.priority}: {inc.desired_state_ref}"[:255],
        source=FREEZE_SOURCE,
        evidence={"incident_id": str(inc.id), "fingerprint": inc.fingerprint, "incident_class": inc.incident_class},
        opened_at=now or utcnow(),
    )
    db.add(fr)
    return fr


def observe_healthy(db: Session, settings: MaintenanceSettings, *, target: str, desired_state_ref: str, sli: str, source: str, collector_version: str, now: Optional[datetime] = None, record: bool = True) -> List[MaintenanceIncident]:
    """A healthy observation of one desired-state reference. Grows the streak
    of every open incident on that reference; returns incidents that just
    recovered."""
    now = now or utcnow()
    if record:
        record_observation(db, sli=sli, target=target, source=source, collector_version=collector_version, trust_class=TrustClass.TRUSTED_PROBE, ok=True, payload={"ref": desired_state_ref}, observed_at=now)
    recovered: List[MaintenanceIncident] = []
    rows = (
        db.query(MaintenanceIncident)
        .filter(MaintenanceIncident.target == target, MaintenanceIncident.desired_state_ref == desired_state_ref, MaintenanceIncident.status == IncidentStatus.OPEN.value)
        .with_for_update()
        .all()
    )
    for inc in rows:
        if _release_settling(db, inc):
            # the repair is still being released: an observation now proves nothing about it
            inc.healthy_streak = 0
            continue
        inc.healthy_streak = int(inc.healthy_streak or 0) + 1
        if inc.healthy_streak >= settings.recovery_streak:
            inc.status = IncidentStatus.RECOVERED.value
            inc.resolved_at = now
            recovered.append(inc)
    db.flush()
    return recovered


def _release_settling(db: Session, inc: MaintenanceIncident) -> bool:
    return db.query(RepairCase).filter(RepairCase.incident_id == inc.id, RepairCase.state.in_(list(_SETTLING))).first() is not None


def open_incidents_without_case(db: Session, settings: MaintenanceSettings, *, now: Optional[datetime] = None, limit: int = 20) -> List[MaintenanceIncident]:
    """Open incidents that no active case covers and that the reopen policy
    allows another case for (cooldown after a terminal case, bounded count)."""
    now = now or utcnow()
    active_states = [s.value for s in sm.NON_TERMINAL]
    covered = db.query(RepairCase.incident_id).filter(
        (RepairCase.state.in_(active_states)) | ((RepairCase.state == sm.CaseState.SAFELY_ESCALATED.value) & (RepairCase.resumable.is_(True)))
    )
    cooldown = now - timedelta(hours=settings.case_reopen_cooldown_hours)
    q = (
        db.query(MaintenanceIncident)
        .filter(MaintenanceIncident.status == IncidentStatus.OPEN.value)
        .filter(~MaintenanceIncident.id.in_(covered))
        .filter(MaintenanceIncident.case_count < settings.max_cases_per_incident)
        .filter((MaintenanceIncident.last_case_terminal_at.is_(None)) | (MaintenanceIncident.last_case_terminal_at <= cooldown))
        .order_by(MaintenanceIncident.priority, MaintenanceIncident.first_observed_at)
        .limit(limit)
    )
    return list(q.all())


def active_case_for(db: Session, incident_id) -> Optional[RepairCase]:
    active_states = [s.value for s in sm.NON_TERMINAL]
    return (
        db.query(RepairCase)
        .filter(RepairCase.incident_id == incident_id)
        .filter((RepairCase.state.in_(active_states)) | ((RepairCase.state == sm.CaseState.SAFELY_ESCALATED.value) & (RepairCase.resumable.is_(True))))
        .first()
    )


def is_covered(db: Session, fingerprint: str) -> bool:
    """An incident is covered ONLY when an active repair case exists for its
    open incident. No memory row, proposal or chat message can say otherwise."""
    inc = db.query(MaintenanceIncident).filter(MaintenanceIncident.fingerprint == fingerprint, MaintenanceIncident.status == IncidentStatus.OPEN.value).first()
    return inc is not None and active_case_for(db, inc.id) is not None


def ingest_many(db: Session, settings: MaintenanceSettings, violations: Iterable[Violation], *, now: Optional[datetime] = None) -> List[Tuple[MaintenanceIncident, bool]]:
    return [ingest_violation(db, settings, v, now=now) for v in violations]


__all__ = [
    "Violation",
    "FREEZING_CLASSES",
    "FREEZE_SOURCE",
    "record_observation",
    "ingest_violation",
    "ensure_incident_freeze",
    "observe_healthy",
    "open_incidents_without_case",
    "active_case_for",
    "is_covered",
    "ingest_many",
]
