"""The Maintenance Kernel: reconciliation, not event chaining (ADR-0010 D6-D8).

Each cycle asks, for every due repair case: *what is the persisted state,
what should happen next, has it already happened?* Events are only wake-up
hints; a swallowed event, a crashed worker, a Redis/Postgres blip or a
restart cannot strand a case, because the next cycle reads the rows again.

Guarantees (tests/maintenance/):

* **Liveness.** Every non-terminal case has ``next_action_at`` and
  ``deadline_at`` (DB constraint). When ``now > deadline_at`` and no lease is
  valid, the state's declared recovery transition runs (watchdog). A case
  whose next action is overdue by ``MAINTENANCE_STALL_SECONDS`` without a
  lease raises a ``CONTROL_PLANE`` incident (stall detector).
* **Exactly one writer.** A case is claimed with ``FOR UPDATE SKIP LOCKED``
  and a lease; results of a long activity are applied only if the lease is
  still ours (fencing). Two kernels never both transition one case.
* **Crash safety.** Every side effect is idempotent: activity rows are keyed,
  attempt worktrees are reset to base on (re)start, candidates are keyed by
  the attempt workspace id, promotions per candidate, releases per
  (case, sha) plus one-in-flight globally. A crash between any two writes
  replays the same step without a duplicate patch, PR or release.
* **Bounded.** State tries, activity tries, repair attempts, plan revisions,
  model spend and wall clock are all bounded; exhaustion is a transition.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import CodeCandidate, CodePromotion, PromotionStatus
from ..society.config import SocietySettings, get_settings as get_society_settings
from ..society.engineering import workspace as ws_mod
from ..society.events import utcnow
from . import activities as act
from . import attestation as att_mod
from . import escalation as esc_mod
from . import harness as h
from . import incidents as inc_mod
from . import knowledge as know_mod
from . import policy as pol
from . import slo as slo_mod
from . import state_machine as sm
from .bridge import create_candidate, ensure_experiment, request_promotion
from .config import MaintenanceSettings, get_maintenance_settings
from .contracts import load_registry
from .ledger import digest, merge_facts, record_artifact, record_evidence, reschedule, state_of, transition
from .orm import (
    MaintenanceHeartbeat,
    MaintenanceIncident,
    MaintenanceKnownGood,
    MaintenanceObservation,
    MaintenanceRelease,
    MaintenanceToilEvent,
    RepairActivity,
    RepairArtifact,
    RepairAttempt,
    RepairCase,
    RepairPlanRevision,
)
from .taxonomy import ActivityKind, ActivityStatus, ActorType, IncidentClass, IncidentStatus, MaintenanceRiskClass as MRC, Priority, ReleaseStatus, Severity, TrustClass, risk_max

logger = logging.getLogger(__name__)

C, W, A, O = ActorType.CONTROLLER, ActorType.WATCHDOG, ActorType.ACTIVITY, ActorType.OWNER
S = sm.CaseState
KERNEL_VERSION = "kernel/1"
OBSERVATION_RETENTION = timedelta(days=30)
#: Heartbeat gap beyond which the kernel counts as having been down.
DOWNTIME_GRACE = timedelta(minutes=5)

#: Incident sources that are themselves trusted confirmation (no second probe).
SELF_CONFIRMING_SOURCES = frozenset({"maintenance_kernel", "maintenance_watchdog", "ci", "database_invariant", "release_controller"})
#: Contracts per incident class when the ingesting collector did not say.
DEFAULT_CONTRACT = {
    IncidentClass.ACCESSIBILITY.value: "browser_experience",
    IncidentClass.PERFORMANCE.value: "performance_budgets",
    IncidentClass.ECONOMIC_INVARIANT.value: "economic_invariants",
    IncidentClass.DATA_INVARIANT.value: "economic_invariants",
    IncidentClass.SECURITY.value: "security_invariants",
    IncidentClass.DEPENDENCY.value: "dependency_posture",
    IncidentClass.A2A.value: "a2a",
    IncidentClass.AUTH.value: "human_auth",
}


@dataclass
class KernelStats:
    opened: int = 0
    claimed: int = 0
    transitions: int = 0
    escalated: int = 0
    resumed: int = 0
    stalls: int = 0
    errors: int = 0
    activities: int = 0
    paused: int = 0
    notes: List[str] = field(default_factory=list)


class LeaseLost(Exception):
    """Another worker owns the case now; this worker's results are discarded."""


class MaintenanceKernel:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        settings: Optional[MaintenanceSettings] = None,
        society_settings: Optional[SocietySettings] = None,
        model: Any = "auto",
        worker_id: Optional[str] = None,
        activity_timeout: Optional[float] = None,
    ):
        self.session_factory = session_factory
        self._settings = settings
        self._society = society_settings
        self._model = model
        self.worker_id = worker_id or f"maint-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._activity_timeout = activity_timeout

    # ── configuration (re-read each cycle unless injected) ──────────────
    @property
    def settings(self) -> MaintenanceSettings:
        return self._settings or get_maintenance_settings()

    @property
    def society(self) -> SocietySettings:
        return self._society or get_society_settings()

    def model(self) -> Optional[act.ActivityModel]:
        if self._model == "auto":
            return act.get_activity_model(self.society)
        return self._model

    # ── the cycle ─────────────────────────────────────────────────────────
    def reconcile(self, *, now: Optional[datetime] = None, max_cases: Optional[int] = None) -> KernelStats:
        now = now or utcnow()
        st = KernelStats()
        s = self.settings
        previous_beat = self._heartbeat("kernel", now, st)
        self._shift_deadlines_for_downtime(previous_beat, now, st)
        if not s.autonomy_enabled:
            st.paused = self._pause_for_kill_switch(now, st)
            self._stall_check(now, st)
            return st
        self._open_cases(now, st)
        self._resume_sweep(now, st)
        for _ in range(max_cases or s.reconcile_batch):
            case_id = self._claim(now)
            if case_id is None:
                break
            st.claimed += 1
            self._process(case_id, now, st)
        self._stall_check(now, st)
        self._housekeeping(now)
        return st

    # ── heartbeat / housekeeping ──────────────────────────────────────────
    def _shift_deadlines_for_downtime(self, previous_beat: Optional[datetime], now: datetime, st: KernelStats) -> None:
        """Deadlines measure how long a state's WORK may take while the
        controller is alive. After controller downtime (no kernel heartbeat
        for longer than DOWNTIME_GRACE) every live case's deadlines move by the
        gap, so a restart continues cases instead of mass-escalating them. A
        kernel that stays dead is the watchdog's problem (CONTROL_PLANE)."""
        if previous_beat is None:
            return
        gap = now - previous_beat
        if gap <= DOWNTIME_GRACE:
            return
        db = self.session_factory()
        try:
            db.execute(
                text(
                    """
                    UPDATE repair_cases SET deadline_at = deadline_at + :gap, case_deadline_at = case_deadline_at + :gap
                     WHERE state NOT IN ('AUTO_REPAIRED','AUTO_ROLLED_BACK','SAFELY_ESCALATED','CANNOT_REPRODUCE','DUPLICATE_RESOLVED','POLICY_REFUSED')
                    """
                ),
                {"gap": gap},
            )
            db.commit()
            st.notes.append(f"controller downtime {int(gap.total_seconds())}s: live deadlines shifted")
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()

    def _heartbeat(self, component: str, now: datetime, st: KernelStats, *, error: Optional[str] = None) -> Optional[datetime]:
        previous: Optional[datetime] = None
        db = self.session_factory()
        try:
            hb = db.get(MaintenanceHeartbeat, component)
            previous = hb.beat_at if hb is not None else None
            if hb is None:
                hb = MaintenanceHeartbeat(component=component, worker_id=self.worker_id, beat_at=now, cycles=0, errors=0, details={})
                db.add(hb)
            hb.worker_id = self.worker_id
            hb.beat_at = now
            hb.cycles = int(hb.cycles or 0) + 1
            if error:
                hb.errors = int(hb.errors or 0) + 1
                hb.last_error_class = error[:64]
                hb.last_error_at = now
            hb.details = {"version": KERNEL_VERSION, "flags": self.settings.public_flags()}
            db.commit()
        except Exception:  # noqa: BLE001 -- liveness bookkeeping never breaks a cycle
            db.rollback()
        finally:
            db.close()
        return previous

    def _housekeeping(self, now: datetime) -> None:
        db = self.session_factory()
        try:
            cutoff = now - OBSERVATION_RETENTION
            db.execute(text("DELETE FROM maintenance_observations WHERE id IN (SELECT id FROM maintenance_observations WHERE observed_at < :c AND incident_id IS NULL LIMIT 5000)"), {"c": cutoff})
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()

    # ── incidents -> cases ─────────────────────────────────────────────────
    def _open_cases(self, now: datetime, st: KernelStats) -> None:
        from .ledger import open_case  # noqa: PLC0415

        db = self.session_factory()
        try:
            for inc in inc_mod.open_incidents_without_case(db, self.settings, now=now):
                if open_case(db, inc, self.settings, now=now) is not None:
                    st.opened += 1
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("maintenance: opening cases failed")
            st.errors += 1
        finally:
            db.close()

    # ── kill switch ────────────────────────────────────────────────────────
    def _pause_for_kill_switch(self, now: datetime, st: KernelStats) -> int:
        """MAINTENANCE_AUTONOMY_ENABLED=false: no new autonomous work. Cases
        that are not mid-release are handed to the owner (final escalation,
        no cooldown so a re-enable can start fresh). Mid-release cases keep
        being mirrored so a rollback can still complete."""
        db = self.session_factory()
        n = 0
        try:
            keep = {S.RELEASING.value, S.POST_RELEASE_VERIFYING.value, S.RECOVERY_PENDING.value}
            rows = db.query(RepairCase).filter(RepairCase.state.in_([x.value for x in sm.NON_TERMINAL])).with_for_update(skip_locked=True).all()
            for case in rows:
                if case.state in keep:
                    self._mirror_release(db, case, now, st)
                    continue
                if case.lease_expires_at is not None and case.lease_expires_at > now:
                    continue
                self._escalate(db, case, now, reason="autonomy_disabled", actor=C, resumable=False, st=st, cooldown=False)
                n += 1
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("maintenance: kill-switch pause failed")
        finally:
            db.close()
        return n

    # ── claim / fence ─────────────────────────────────────────────────────
    _CLAIM = text(
        """
        WITH c AS (
            SELECT id FROM repair_cases
             WHERE state NOT IN ('AUTO_REPAIRED','AUTO_ROLLED_BACK','SAFELY_ESCALATED','CANNOT_REPRODUCE','DUPLICATE_RESOLVED','POLICY_REFUSED')
               AND (next_action_at <= :now OR deadline_at <= :now)
               AND (lease_expires_at IS NULL OR lease_expires_at < :now)
             ORDER BY priority, next_action_at
             LIMIT 1
             FOR UPDATE SKIP LOCKED
        )
        UPDATE repair_cases r
           SET lease_owner = :w, lease_expires_at = :until
          FROM c
         WHERE r.id = c.id
        RETURNING r.id
        """
    )

    def _claim(self, now: datetime) -> Optional[uuid.UUID]:
        db = self.session_factory()
        try:
            row = db.execute(self._CLAIM, {"now": now, "w": self.worker_id, "until": now + timedelta(seconds=self.settings.lease_seconds)}).fetchone()
            db.commit()
            return row[0] if row else None
        finally:
            db.close()

    def _fence(self, db: Session, case_id: uuid.UUID) -> RepairCase:
        """Prove, under a row lock, that the lease is still ours. Pending
        changes are flushed first (never discarded)."""
        db.flush()
        owner = db.execute(text("SELECT lease_owner FROM repair_cases WHERE id = :id FOR UPDATE"), {"id": case_id}).scalar()
        if owner != self.worker_id:
            raise LeaseLost(str(case_id))
        return db.get(RepairCase, case_id)

    def _process(self, case_id: uuid.UUID, now: datetime, st: KernelStats) -> None:
        db = self.session_factory()
        try:
            case = self._fence(db, case_id)
            self._step(db, case, now, st)
            case = self._fence(db, case_id)
            case.lease_owner = None
            case.lease_expires_at = None
            db.commit()
        except LeaseLost:
            db.rollback()
            st.notes.append(f"lease lost on {case_id}; results discarded")
        except Exception as exc:  # noqa: BLE001 -- a step failure is bounded and visible, never fatal
            db.rollback()
            st.errors += 1
            logger.exception("maintenance: step failed for case %s", case_id)
            self._heartbeat("kernel", now, st, error=type(exc).__name__)
            self._backoff(case_id, now)
        finally:
            db.close()

    def _backoff(self, case_id: uuid.UUID, now: datetime) -> None:
        db = self.session_factory()
        try:
            case = db.query(RepairCase).filter(RepairCase.id == case_id).with_for_update().first()
            if case is not None and case.lease_owner == self.worker_id:
                case.state_tries = int(case.state_tries or 0) + 1
                case.lease_owner = None
                case.lease_expires_at = None
                if case.state not in {x.value for x in sm.TERMINAL}:
                    reschedule(case, now=now, delay=timedelta(seconds=min(900, 30 * (2 ** min(case.state_tries, 5)))))
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()

    # ── the step ──────────────────────────────────────────────────────────
    def _step(self, db: Session, case: RepairCase, now: datetime, st: KernelStats) -> None:
        state = state_of(case)
        spec = sm.spec(state)
        incident = db.get(MaintenanceIncident, case.incident_id)
        case.state_tries = int(case.state_tries or 0) + 1
        # 1. deadline passed and nobody holds a valid lease (we just claimed it): recovery transition
        if case.deadline_at is not None and now >= case.deadline_at:
            self._on_deadline(db, case, incident, state, now, st)
            return
        # 2. try budget for this state
        if case.state_tries > spec.max_tries:
            self._apply_target(db, case, spec.on_exhausted, now, st, reason=f"tries_exhausted:{state.value}", actor=W)
            return
        # 3. the product recovered by itself before any change was merged: no random patch
        if incident is not None and incident.status == IncidentStatus.RECOVERED.value and state in sm.PRE_MERGE:
            self._terminal(db, case, S.CANNOT_REPRODUCE, now, st, reason="recovered_before_repair", evidence=incident.current_evidence_digest)
            return
        # 4. hard wall clock for everything before main
        if state in sm.PRE_MERGE | {S.PROMOTING} and now >= case.case_deadline_at:
            self._escalate(db, case, now, reason="case_wall_clock_exhausted", actor=W, resumable=False, st=st)
            return
        handler = getattr(self, f"_h_{state.value.lower()}")
        handler(db, case, incident, now, st)

    def _on_deadline(self, db: Session, case: RepairCase, incident, state: sm.CaseState, now: datetime, st: KernelStats) -> None:
        target = sm.spec(state).on_deadline
        # recorded facts beat timeouts: a release outcome already persisted by
        # the release controller decides the case, not the kernel's clock
        if state in (S.RELEASING, S.POST_RELEASE_VERIFYING, S.RECOVERY_PENDING) and self._mirror_release(db, case, now, st):
            return
        if state is S.POST_RELEASE_VERIFYING:
            rel = db.get(MaintenanceRelease, case.release_id) if case.release_id else None
            if rel is not None and rel.status == ReleaseStatus.SUCCEEDED.value and incident is not None and incident.status == IncidentStatus.RECOVERED.value:
                self._terminal(db, case, S.AUTO_REPAIRED, now, st, reason="released_and_recovered", evidence=rel.attestation_digest)
                return
        if state is S.BUILDING:
            self._next_attempt_or_rescope(db, case, now, st, reason="deadline:BUILDING", feedback=["attempt exceeded its deadline"])
            return
        if state is S.POST_RELEASE_VERIFYING:
            self._fix_ineffective(db, case, incident, now, st)
            return
        if state is S.DETECTED and incident is not None and incident.status == IncidentStatus.OPEN.value and self._confirmed(incident, now):
            self._to(db, case, S.CONFIRMED, now, st, reason="confirmed_at_deadline", actor=C)
            return
        self._apply_target(db, case, target, now, st, reason=f"deadline:{state.value}", actor=W)

    def _apply_target(self, db: Session, case: RepairCase, target: sm.CaseState, now: datetime, st: KernelStats, *, reason: str, actor: ActorType) -> None:
        if target is S.SAFELY_ESCALATED:
            self._escalate(db, case, now, reason=reason, actor=actor, resumable=False, st=st)
        elif target is S.NEEDS_RESCOPE:
            self._to(db, case, S.NEEDS_RESCOPE, now, st, reason=reason, actor=actor)
        elif target in sm.TERMINAL:
            self._terminal(db, case, target, now, st, reason=reason, actor=actor)
        else:
            self._to(db, case, target, now, st, reason=reason, actor=actor)

    # ── transitions ───────────────────────────────────────────────────────
    def _to(self, db: Session, case: RepairCase, dst: sm.CaseState, now: datetime, st: KernelStats, *, reason: str, actor: ActorType = C, detail=None, evidence=None, delay=None) -> None:
        transition(db, case, dst, actor=actor, actor_id=self.worker_id, reason=reason, now=now, detail=detail, evidence_digest=evidence, delay=delay)
        st.transitions += 1

    def _terminal(self, db: Session, case: RepairCase, dst: sm.CaseState, now: datetime, st: KernelStats, *, reason: str, actor: ActorType = C, evidence=None, detail=None) -> None:
        transition(db, case, dst, actor=actor, actor_id=self.worker_id, reason=reason, now=now, evidence_digest=evidence, detail=detail)
        st.transitions += 1
        know_mod.record_terminal(db, case, now=now)

    def _escalate(self, db: Session, case: RepairCase, now: datetime, *, reason: str, actor: ActorType, resumable: bool, st: KernelStats, cooldown: bool = True, extra: Optional[Dict[str, Any]] = None) -> None:
        pkg = esc_mod.package(db, case, reason=reason, extra=extra)
        explanation = self._explain(db, case, pkg) if actor is not W and reason not in ("autonomy_disabled",) else None
        if explanation:
            pkg["explanation"] = {"trust": "model_hypothesis", **explanation}
        record_artifact(db, case, kind="escalation_package", content=pkg, trust_class=TrustClass.TRUSTED_DB, produced_by=self.worker_id, plan_revision=case.current_plan_revision, attempt=case.attempt_count, now=now)
        transition(db, case, S.SAFELY_ESCALATED, actor=actor, actor_id=self.worker_id, reason=reason, now=now, resumable=resumable, escalation=pkg)
        st.transitions += 1
        st.escalated += 1
        if not cooldown:
            inc = db.get(MaintenanceIncident, case.incident_id)
            if inc is not None:
                inc.last_case_terminal_at = None
        esc_mod.notify_operators(db, case, pkg)
        if not resumable:
            know_mod.record_terminal(db, case, now=now)

    def _explain(self, db: Session, case: RepairCase, pkg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Optional model-written owner summary. Failure is harmless: the
        package is complete without it."""
        model = self.model() if self.settings.cognition_enabled else None
        if model is None or case.lease_owner != self.worker_id:
            return None
        ok, _ = pol.budget_allows(db, self.settings, case)
        if not ok:
            return None
        res = self._run_activity_sync(db, case, ActivityKind.EXPLAIN_ESCALATION, {"package": pkg}, tools={}, model=model)
        return res.output if res and res.ok else None

    # ── state handlers ────────────────────────────────────────────────────
    def _confirmed(self, incident: MaintenanceIncident, now: datetime) -> bool:
        fresh = incident.last_observed_at is not None and (now - incident.last_observed_at).total_seconds() <= self.settings.confirm_window_seconds
        return fresh and (incident.observation_count >= 2 or incident.source in SELF_CONFIRMING_SOURCES)

    def _h_detected(self, db, case, incident, now, st):
        if incident is None or incident.status != IncidentStatus.OPEN.value:
            self._terminal(db, case, S.CANNOT_REPRODUCE, now, st, reason="incident_not_open")
            return
        if self._confirmed(incident, now):
            record_evidence(db, case, kind="confirmation", source=incident.source, collector_version=KERNEL_VERSION, trust_class=TrustClass.TRUSTED_PROBE,
                            payload={"observations": incident.observation_count, "last_observed_at": incident.last_observed_at.isoformat(), "evidence_digest": incident.current_evidence_digest}, now=now)
            self._to(db, case, S.CONFIRMED, now, st, reason="violation_confirmed", evidence=incident.current_evidence_digest)
            return
        reschedule(case, now=now)

    def _h_confirmed(self, db, case, incident, now, st):
        """Deterministic triage: lanes that must never produce a code patch
        end here; the rest wait for a maintenance slot (never a portfolio slot)."""
        cls = IncidentClass(incident.incident_class)
        contract_id = self._contract_id(incident)
        if cls in (IncidentClass.ECONOMIC_INVARIANT, IncidentClass.DATA_INVARIANT):
            inc_mod.ensure_incident_freeze(db, incident, now=now)
            self._escalate(db, case, now, reason="data_invariant", actor=C, resumable=False, st=st)
            return
        if cls is IncidentClass.EXTERNAL_DEPENDENCY:
            self._escalate(db, case, now, reason="external_dependency", actor=C, resumable=False, st=st)
            return
        if cls is IncidentClass.CONTROL_PLANE:
            # the kernel never repairs and releases itself (constitutional self-modification)
            self._escalate(db, case, now, reason="control_plane_defect", actor=C, resumable=False, st=st)
            return
        if cls is IncidentClass.AVAILABILITY and (incident.provenance or {}).get("failure") in ("unreachable", "timeout"):
            self._escalate(db, case, now, reason="runtime_unavailable", actor=C, resumable=False, st=st)
            return
        try:
            contract = load_registry().get(contract_id)
        except KeyError:
            contract = None
        if contract is not None and not contract.autonomous_repair:
            self._escalate(db, case, now, reason=f"contract_not_autonomous:{contract_id}", actor=C, resumable=False, st=st)
            return
        if not self.settings.cognition_enabled or self.model() is None:
            self._escalate(db, case, now, reason="cognition_unavailable", actor=C, resumable=False, st=st)
            return
        if not pol.has_capacity(db, self.settings, case.priority, exclude_case=case.id):
            merge_facts(case, queued_at=(case.facts or {}).get("queued_at") or now.isoformat())
            reschedule(case, now=now)
            return
        ok, why = pol.budget_allows(db, self.settings, case, now=now)
        if not ok:
            if why == "case_budget_exhausted":
                self._escalate(db, case, now, reason=why, actor=C, resumable=False, st=st)
                return
            merge_facts(case, budget_wait=why)
            reschedule(case, now=now, delay=timedelta(minutes=15))
            return
        self._to(db, case, S.TRIAGED, now, st, reason="triaged_repair_lane", detail={"contract": contract_id, "priority": case.priority})

    def _h_triaged(self, db, case, incident, now, st):
        self._to(db, case, S.DIAGNOSING, now, st, reason="diagnosis_scheduled")

    def _h_diagnosing(self, db, case, incident, now, st):
        """DiagnoseIncident then DesignRepair -> immutable plan revision N+1."""
        if not self._cognition_ok(db, case, now, st):
            return
        model = self.model()
        rev = case.current_plan_revision + 1
        diag = self._latest_artifact(db, case, "diagnosis", plan_revision=rev)
        if diag is None:
            res = self._run_activity_sync(db, case, ActivityKind.DIAGNOSE_INCIDENT, self._incident_input(db, case, incident), tools=h.read_tools(self.society.repo_root, page_bytes=self.settings.read_page_bytes), model=model, plan_revision=rev)
            if not self._activity_outcome(db, case, res, ActivityKind.DIAGNOSE_INCIDENT, now, st, plan_revision=rev):
                return
            diag = record_artifact(db, case, kind="diagnosis", content=res.output, trust_class=TrustClass.MODEL_HYPOTHESIS, produced_by=f"activity:{ActivityKind.DIAGNOSE_INCIDENT.value}", plan_revision=rev, now=now)
        design_input = {**self._incident_input(db, case, incident), "diagnosis": {"trust": "model_hypothesis", **(diag.content or {})}}
        res = self._run_activity_sync(db, case, ActivityKind.DESIGN_REPAIR, design_input, tools=h.read_tools(self.society.repo_root, page_bytes=self.settings.read_page_bytes), model=model, plan_revision=rev)
        if not self._activity_outcome(db, case, res, ActivityKind.DESIGN_REPAIR, now, st, plan_revision=rev):
            return
        plan, problems = self._make_plan(db, case, incident, res.output, rev, parent=None if rev == 1 else rev - 1, rescope_reason=None if rev == 1 else "re_diagnosis")
        if plan is None:
            self._activity_feedback(db, case, ActivityKind.DESIGN_REPAIR, problems, now)
            reschedule(case, now=now)
            return
        self._to(db, case, S.PLAN_READY, now, st, reason=f"plan_r{rev}_ready", actor=A, evidence=plan.digest)

    def _h_plan_ready(self, db, case, incident, now, st):
        plan = self._plan(db, case)
        if plan is None:
            self._escalate(db, case, now, reason="plan_missing", actor=C, resumable=False, st=st)
            return
        decision = MRC(plan.risk_class)
        previous = case.risk_class
        if decision is MRC.CONSTITUTIONAL:
            self._terminal(db, case, S.POLICY_REFUSED, now, st, reason="plan_scope_constitutional", evidence=plan.digest, detail={"reasons": plan.risk_reasons})
            return
        if pol.escalates(previous, decision):
            merge_facts(case, risk_escalations=(case.facts.get("risk_escalations") or []) + [{"from": previous, "to": decision.value, "plan_revision": plan.revision}])
        case.risk_class = decision.value
        case.attempt_count = int(case.attempt_count or 0) + 1
        self._new_attempt(db, case, plan, now)
        self._to(db, case, S.BUILDING, now, st, reason=f"build_attempt_{case.attempt_count}", detail={"risk_class": decision.value})

    def _h_building(self, db, case, incident, now, st):
        if not self._cognition_ok(db, case, now, st):
            return
        plan = self._plan(db, case)
        attempt = self._attempt(db, case)
        if plan is None or attempt is None:
            self._escalate(db, case, now, reason="attempt_state_missing", actor=C, resumable=False, st=st)
            return
        ws = h.open_attempt_workspace(self.society, case.id, attempt.attempt, fresh=True)  # replay-safe: always from base
        attempt.base_sha = ws.base_sha
        state = h.AttemptState(ws=ws, files_allowed=list(plan.files_allowed or []), test_targets=list(plan.acceptance_tests or []),
                               max_test_runs=self.settings.max_test_runs_per_attempt, test_timeout=self.society.qa_test_timeout_seconds, page_bytes=self.settings.read_page_bytes)
        payload = {
            **self._incident_input(db, case, incident),
            "plan": {"revision": plan.revision, "root_cause": {"trust": "model_hypothesis", "text": plan.root_cause}, "approach": plan.approach,
                     "files_allowed": plan.files_allowed, "acceptance_tests": plan.acceptance_tests, "base_sha": ws.base_sha},
            "feedback_from_previous_attempts": self._feedback(db, case),
            "patch_protocol": "apply_patch args: {files: [{path, operations: [{op: replace_exact|insert_after|insert_before|create|delete, old/new | anchor/text | text}]}]} -- exact text, each old/anchor unique",
        }
        # front-loaded: the target files (or the cited windows) are in the input, so reads are the exception
        diag = self._latest_artifact(db, case, "diagnosis")
        cites = [plan.root_cause or "", plan.approach or ""] + [str(x) for x in (((diag.content or {}) if diag else {}).get("evidence") or [])]
        files = h.target_file_context(ws.path, list(plan.files_allowed or []), cites, tests=list(plan.acceptance_tests or []))
        payload["target_files"] = {"trust": "untrusted_repository_data", "files": files}
        payload["read_budget"] = f"at most {self.settings.builder_max_read_calls} read-tool calls this try; the target files are above"
        res = self._run_activity_sync(db, case, ActivityKind.AUTHOR_PATCH, payload, tools=h.builder_tools(state), model=self.model(), plan_revision=plan.revision, attempt=attempt.attempt,
                                      max_turns=self.settings.builder_max_turns, max_read_calls=self.settings.builder_max_read_calls, submit_check=h.submit_check(state))
        attempt.turns = int(attempt.turns or 0) + (res.turns if res else 0)
        attempt.test_runs = int(attempt.test_runs or 0) + state.test_runs
        if res is None:
            return  # lease lost / in-flight elsewhere
        if not res.ok:
            if self._activity_tries(db, case, ActivityKind.AUTHOR_PATCH, plan.revision, attempt.attempt) < self.settings.activity_max_tries:
                self._activity_feedback(db, case, ActivityKind.AUTHOR_PATCH, [f"{res.error_class}: {res.error}"], now)
                reschedule(case, now=now, delay=timedelta(seconds=30))
                return
            attempt.outcome = "failed"
            attempt.finished_at = now
            self._next_attempt_or_rescope(db, case, now, st, reason=f"author_patch_{res.error_class}", feedback=[f"{res.error_class}: {res.error}"])
            return
        if res.rescope is not None:
            attempt.outcome = "needs_rescope"
            attempt.finished_at = now
            attempt.feedback = {"rescope": res.rescope}
            record_artifact(db, case, kind="rescope_request", content=res.rescope, trust_class=TrustClass.MODEL_HYPOTHESIS, produced_by="activity:AuthorPatch", plan_revision=plan.revision, attempt=attempt.attempt, now=now)
            self._to(db, case, S.NEEDS_RESCOPE, now, st, reason=f"builder:{res.rescope['reason']}", actor=A)
            return
        fin = h.finalize_attempt(ws, f"maintenance: case {case.id} r{plan.revision} a{attempt.attempt}\n\n{(res.output or {}).get('summary', '')[:400]}")
        if not fin["changed"] or fin["format_only"]:
            attempt.outcome = "failed"
            attempt.finished_at = now
            attempt.feedback = {"problems": ["no substantive change was produced"]}
            self._next_attempt_or_rescope(db, case, now, st, reason="empty_patch", feedback=["the submitted attempt changed nothing substantive"])
            return
        attempt.head_sha = fin["head_sha"]
        attempt.patch_digest = fin["diff_digest"]
        attempt.outcome = "patch_ready"
        record_artifact(db, case, kind="patchset", content={"head_sha": fin["head_sha"], "changed": fin["changed"], "diff_digest": fin["diff_digest"], "diff_lines": fin["diff_lines"],
                                                           "patch_digests": state.patch_digests, "summary": (res.output or {}).get("summary"), "builder_activity_turns": res.turns},
                        trust_class=TrustClass.TRUSTED_EXECUTION, produced_by="harness", plan_revision=plan.revision, attempt=attempt.attempt, now=now)
        case.head_sha = fin["head_sha"]
        case.base_sha = ws.base_sha
        self._to(db, case, S.VERIFYING, now, st, reason="patch_ready", actor=A, evidence=fin["diff_digest"])

    def _h_verifying(self, db, case, incident, now, st):
        plan = self._plan(db, case)
        attempt = self._attempt(db, case)
        ws = h.open_attempt_workspace(self.society, case.id, attempt.attempt, fresh=False)
        if ws_mod.head_sha(ws) != attempt.head_sha:
            self._next_attempt_or_rescope(db, case, now, st, reason="attempt_head_moved", feedback=["the attempt worktree no longer holds the verified head"])
            return
        v = h.verify(self.society, self.settings, ws, files_allowed=plan.files_allowed, acceptance_tests=plan.acceptance_tests)
        vdict = {"passed": v.passed, "qa": v.qa, "risk": v.risk, "changed": v.changed, "head_sha": v.head_sha, "diff_digest": v.diff_digest, "diff_lines": v.diff_lines, "feedback": v.feedback}
        record_artifact(db, case, kind="verification", content=vdict, trust_class=TrustClass.TRUSTED_EXECUTION, produced_by="qa:deterministic", plan_revision=plan.revision, attempt=attempt.attempt, now=now)
        risk = MRC(v.risk["risk_class"])
        if risk is MRC.CONSTITUTIONAL:
            attempt.outcome = "rejected"
            self._terminal(db, case, S.POLICY_REFUSED, now, st, reason="patch_constitutional", evidence=v.diff_digest, detail={"findings": v.risk.get("findings")})
            return
        if not v.passed:
            attempt.outcome = "rejected"
            attempt.finished_at = now
            attempt.feedback = {"qa": v.feedback}
            self._next_attempt_or_rescope(db, case, now, st, reason="qa_failed", feedback=v.feedback)
            return
        # model scrutiny: can only fail a patch the deterministic gates passed
        for kind in (ActivityKind.REVIEW_PATCH, ActivityKind.SECURITY_REVIEW):
            prior = self._latest_artifact(db, case, "security_review" if kind is ActivityKind.SECURITY_REVIEW else "patch_review", plan_revision=plan.revision, attempt=attempt.attempt)
            if prior is not None:
                out = prior.content or {}
            else:
                if not self._cognition_ok(db, case, now, st):
                    return
                res = self._run_activity_sync(db, case, kind, {"plan": {"files_allowed": plan.files_allowed, "approach": plan.approach}, "incident": self._incident_input(db, case, incident)["incident"],
                                                              "verification": {"qa": v.qa.get("verdict"), "static_findings": v.qa.get("static_findings"), "risk": v.risk}},
                                              tools={**h.read_tools(ws.path, page_bytes=self.settings.read_page_bytes), "read_diff": h.builder_tools(h.AttemptState(ws, list(plan.files_allowed), [], 0, 1, self.settings.read_page_bytes))["read_diff"]},
                                              model=self.model(), plan_revision=plan.revision, attempt=attempt.attempt)
                if res is None:
                    return
                if not self._activity_outcome(db, case, res, kind, now, st, plan_revision=plan.revision, attempt=attempt.attempt):
                    return
                out = {**res.output, "head_sha": v.head_sha, "reviewer": "maintenance-" + act.SPECS[kind].role}
                record_artifact(db, case, kind="security_review" if kind is ActivityKind.SECURITY_REVIEW else "patch_review", content=out, trust_class=TrustClass.MODEL_HYPOTHESIS, produced_by=f"activity:{kind.value}", plan_revision=plan.revision, attempt=attempt.attempt, now=now)
            blocking = [f for f in out.get("findings") or [] if f.get("severity") in ("high", "critical")]
            if out.get("verdict") != "pass" or blocking:
                attempt.outcome = "rejected"
                attempt.finished_at = now
                attempt.feedback = {"review": out}
                self._next_attempt_or_rescope(db, case, now, st, reason=f"{kind.value}_failed", feedback=[f"{kind.value}: {out.get('summary', '')}"] + [f"{f.get('severity')}: {f.get('file')}: {f.get('note')}" for f in (out.get('findings') or [])][:8])
                return
        if pol.escalates(case.risk_class, risk):
            merge_facts(case, risk_escalations=(case.facts.get("risk_escalations") or []) + [{"from": case.risk_class, "to": risk.value, "at": "verification"}])
        case.risk_class = (risk_max(MRC(case.risk_class), risk) if case.risk_class else risk).value
        attempt.outcome = "verified"
        attempt.finished_at = now
        sec = self._latest_artifact(db, case, "security_review", plan_revision=plan.revision, attempt=attempt.attempt)
        security = {**(sec.content if sec else {}), "verdict": "pass", "head_sha": v.head_sha, "static_findings": v.qa.get("static_findings") or []}
        cand = create_candidate(db, society_settings=self.society, case=case, incident=incident, plan=plan, ws=ws, verification=vdict, security=security)
        case.candidate_id = cand.id
        if not self.settings.green_promotion_enabled:
            # the verified, READY candidate is kept; an owner resume promotes it
            self._escalate(db, case, now, reason="promotion_disabled", actor=C, resumable=True, st=st)
            return
        promo = request_promotion(db, society_settings=self.society, case=case, candidate=cand)
        case.candidate_id = cand.id
        case.promotion_id = promo.id
        self._to(db, case, S.PROMOTING, now, st, reason="verified_promotion_requested", evidence=v.diff_digest, detail={"risk_class": case.risk_class, "promotion_id": str(promo.id)})

    def _h_needs_rescope(self, db, case, incident, now, st):
        if case.current_plan_revision >= self.settings.max_plan_revisions:
            self._escalate(db, case, now, reason="plan_revisions_exhausted", actor=C, resumable=False, st=st)
            return
        if not self._cognition_ok(db, case, now, st):
            return
        plan = self._plan(db, case)
        rev = case.current_plan_revision + 1
        payload = {
            **self._incident_input(db, case, incident),
            "current_plan": {"revision": plan.revision, "files_allowed": plan.files_allowed, "acceptance_tests": plan.acceptance_tests, "approach": plan.approach, "root_cause": {"trust": "model_hypothesis", "text": plan.root_cause}},
            "builder_and_qa_evidence": self._feedback(db, case),
            "instruction": "Propose revision N+1. It may add files; the trusted policy re-classifies its risk independently.",
        }
        res = self._run_activity_sync(db, case, ActivityKind.DESIGN_REPAIR, payload, tools=h.read_tools(self.society.repo_root, page_bytes=self.settings.read_page_bytes), model=self.model(), plan_revision=rev)
        if not self._activity_outcome(db, case, res, ActivityKind.DESIGN_REPAIR, now, st, plan_revision=rev):
            return
        req = self._latest_artifact(db, case, "rescope_request")
        new, problems = self._make_plan(db, case, incident, res.output, rev, parent=plan.revision, rescope_reason=((req.content or {}).get("reason") if req else "qa_feedback"))
        if new is None:
            self._activity_feedback(db, case, ActivityKind.DESIGN_REPAIR, problems, now)
            reschedule(case, now=now)
            return
        case.rescope_count = int(case.rescope_count or 0) + 1
        self._to(db, case, S.PLAN_READY, now, st, reason=f"rescoped_to_r{rev}", actor=A, evidence=new.digest)

    def _h_promoting(self, db, case, incident, now, st):
        promo = db.get(CodePromotion, case.promotion_id) if case.promotion_id else None
        cand = db.get(CodeCandidate, case.candidate_id) if case.candidate_id else None
        if promo is None or cand is None:
            self._escalate(db, case, now, reason="promotion_missing", actor=C, resumable=False, st=st)
            return
        status = getattr(promo.status, "value", promo.status)
        if status == PromotionStatus.MERGED.value:
            case.merged_sha = promo.merged_sha or promo.candidate_sha
            record_evidence(db, case, kind="merged_to_main", source="promotion_controller", collector_version=KERNEL_VERSION, trust_class=TrustClass.TRUSTED_PROVIDER,
                            payload={"promotion_id": str(promo.id), "pr": promo.external_pr_number, "merged_sha": case.merged_sha, "ci": promo.ci_state, "eligibility": {k: (promo.eligibility or {}).get(k) for k in ("auto_merge_allowed", "human_approvals", "merge_eligible")}}, now=now)
            self._to(db, case, S.READY_FOR_RELEASE, now, st, reason="merged_to_main", evidence=case.merged_sha)
            return
        if status in (PromotionStatus.REJECTED.value, PromotionStatus.CI_FAILED.value) or (status == PromotionStatus.SUPERSEDED.value and not (promo.evidence or {}).get("base_reconcile_pending")):
            record_evidence(db, case, kind="promotion_failed", source="promotion_controller", collector_version=KERNEL_VERSION, trust_class=TrustClass.TRUSTED_PROVIDER,
                            payload={"promotion_id": str(promo.id), "status": status, "reason": (promo.failure_reason or "")[:500], "ci": promo.ci_state}, now=now)
            self._next_attempt_or_rescope(db, case, now, st, reason=f"promotion_{status}", feedback=[f"promotion {status}: {(promo.failure_reason or '')[:400]}"], from_promoting=True)
            return
        ensure_experiment(db, society_settings=self.society, promotion=promo, candidate=cand)
        post_ci = status in (PromotionStatus.CI_PASSED.value, PromotionStatus.AWAITING_APPROVAL.value, PromotionStatus.MERGE_ELIGIBLE.value)
        elig = promo.eligibility or {}
        if post_ci and case.risk_class != MRC.MAINTENANCE_GREEN.value:
            self._escalate(db, case, now, reason="owner_approval_required", actor=C, resumable=True, st=st)
            return
        if post_ci and elig.get("human_approval_required") and not elig.get("auto_merge_enabled"):
            # GREEN, but autonomous merge is off in this environment: the owner merges
            self._escalate(db, case, now, reason="owner_approval_required", actor=C, resumable=True, st=st)
            return
        if post_ci and elig.get("merge_freeze"):
            merge_facts(case, merge_freeze=list(elig.get("merge_freeze") or [])[:10])
        if status == PromotionStatus.BLOCKED_EXTERNAL.value:
            self._escalate(db, case, now, reason="promotion_provider_unavailable", actor=C, resumable=True, st=st)
            return
        reschedule(case, now=now)

    def _h_ready_for_release(self, db, case, incident, now, st):
        risk = MRC(case.risk_class or MRC.RED.value)
        approval = (case.facts or {}).get("owner_approval")
        if risk is MRC.RED or risk is MRC.CONSTITUTIONAL or (risk is MRC.AMBER and not approval):
            self._escalate(db, case, now, reason="release_requires_owner", actor=C, resumable=False, st=st)
            return
        if not self.settings.green_release_enabled:
            self._escalate(db, case, now, reason="release_disabled", actor=C, resumable=True, st=st)
            return
        existing = db.query(MaintenanceRelease).filter(MaintenanceRelease.case_id == case.id, MaintenanceRelease.head_sha == case.merged_sha).first()
        if existing is None:
            att = self._attestation(db, case, incident, now)
            d, sig = att_mod.sign(att)
            rel = MaintenanceRelease(
                id=uuid.uuid4(), case_id=case.id, incident_id=case.incident_id, status=ReleaseStatus.PENDING.value, risk_class=risk.value,
                head_sha=case.merged_sha, attestation=att, attestation_digest=d, attestation_signature=sig, verification={}, services=[], deployments={}, rollback={},
                healthy_streak=0, attempt=0, next_action_at=now, deadline_at=now + timedelta(hours=3), created_at=now, updated_at=now,
            )
            sp = db.begin_nested()
            try:
                db.add(rel)
                db.flush()
                sp.commit()
            except IntegrityError:
                sp.rollback()
                merge_facts(case, release_wait="another maintenance release is in flight")
                reschedule(case, now=now, delay=timedelta(minutes=2))
                return
            existing = rel
        case.release_id = existing.id
        self._to(db, case, S.RELEASING, now, st, reason="attested_for_release", evidence=existing.attestation_digest)

    def _mirror_release(self, db, case, now, st) -> bool:
        rel = db.get(MaintenanceRelease, case.release_id) if case.release_id else None
        if rel is None:
            return False
        state = state_of(case)
        rs = ReleaseStatus(rel.status)
        if rs is ReleaseStatus.ROLLED_BACK:
            self._terminal(db, case, S.AUTO_ROLLED_BACK, now, st, reason="rolled_back_to_known_good", evidence=rel.attestation_digest, detail={"rollback": rel.rollback})
            return True
        if rs is ReleaseStatus.ROLLBACK_FAILED:
            self._escalate(db, case, now, reason="rollback_failed", actor=C, resumable=False, st=st)
            return True
        if rs is ReleaseStatus.REFUSED:
            self._escalate(db, case, now, reason=f"release_refused:{(rel.failure_reason or '')[:40]}", actor=C, resumable=False, st=st)
            return True
        if rs is ReleaseStatus.ROLLING_BACK and state is not S.RECOVERY_PENDING:
            self._to(db, case, S.RECOVERY_PENDING, now, st, reason="post_deploy_regression", actor=ActorType.RELEASE)
            return True
        if rs in (ReleaseStatus.POST_DEPLOY_VERIFYING, ReleaseStatus.SUCCEEDED) and state is S.RELEASING:
            self._to(db, case, S.POST_RELEASE_VERIFYING, now, st, reason=f"release_{rs.value}", actor=ActorType.RELEASE)
            return True
        return False

    def _h_releasing(self, db, case, incident, now, st):
        if not self._mirror_release(db, case, now, st):
            reschedule(case, now=now)

    def _h_post_release_verifying(self, db, case, incident, now, st):
        if self._mirror_release(db, case, now, st):
            return
        rel = db.get(MaintenanceRelease, case.release_id) if case.release_id else None
        if rel is not None and rel.status == ReleaseStatus.SUCCEEDED.value and incident is not None and incident.status == IncidentStatus.RECOVERED.value:
            self._terminal(db, case, S.AUTO_REPAIRED, now, st, reason="released_and_recovered", evidence=rel.attestation_digest,
                           detail={"release_id": str(rel.id), "production_sha": rel.production_merge_sha, "healthy_streak": incident.healthy_streak})
            return
        reschedule(case, now=now)

    def _h_recovery_pending(self, db, case, incident, now, st):
        if not self._mirror_release(db, case, now, st):
            reschedule(case, now=now)

    # ── recovery helpers ─────────────────────────────────────────────────
    def _fix_ineffective(self, db, case, incident, now, st):
        """Released, healthy product, but THIS violation persists: the fix was
        wrong. Same case, next plan revision -- never a new proposal."""
        if incident is not None and incident.status == IncidentStatus.RECOVERED.value:
            self._terminal(db, case, S.AUTO_REPAIRED, now, st, reason="released_and_recovered_at_deadline")
            return
        if case.attempt_count >= self.settings.max_repair_attempts or case.current_plan_revision >= self.settings.max_plan_revisions:
            self._escalate(db, case, now, reason="released_fix_ineffective", actor=W, resumable=False, st=st)
            return
        record_evidence(db, case, kind="fix_ineffective", source="maintenance_kernel", collector_version=KERNEL_VERSION, trust_class=TrustClass.TRUSTED_PROBE,
                        payload={"incident_status": incident.status if incident else None, "observations": incident.observation_count if incident else None, "release_id": str(case.release_id)}, now=now)
        case.release_id = None
        case.candidate_id = None
        case.promotion_id = None
        self._to(db, case, S.DIAGNOSING, now, st, reason="released_fix_ineffective", actor=W)

    def _next_attempt_or_rescope(self, db, case, now, st, *, reason: str, feedback: List[str], from_promoting: bool = False) -> None:
        record_artifact(db, case, kind="attempt_feedback", content={"attempt": case.attempt_count, "reason": reason, "feedback": feedback[:20]}, trust_class=TrustClass.TRUSTED_EXECUTION,
                        produced_by=self.worker_id, plan_revision=case.current_plan_revision, attempt=case.attempt_count, now=now)
        if case.attempt_count < self.settings.max_repair_attempts:
            plan = self._plan(db, case)
            case.attempt_count = int(case.attempt_count or 0) + 1
            self._new_attempt(db, case, plan, now)
            if from_promoting:
                case.candidate_id = None
                case.promotion_id = None
            self._to(db, case, S.BUILDING, now, st, reason=reason[:64], actor=C if from_promoting else A)
            return
        if case.current_plan_revision < self.settings.max_plan_revisions and not from_promoting:
            self._to(db, case, S.NEEDS_RESCOPE, now, st, reason=f"attempts_exhausted:{reason}"[:64], actor=C)
            return
        self._escalate(db, case, now, reason=f"repair_budget_exhausted:{reason}"[:64], actor=C, resumable=False, st=st)

    def _cognition_ok(self, db, case, now, st) -> bool:
        if not self.settings.cognition_enabled or self.model() is None:
            self._escalate(db, case, now, reason="cognition_unavailable", actor=C, resumable=False, st=st)
            return False
        ok, why = pol.budget_allows(db, self.settings, case, now=now)
        if not ok:
            if why == "case_budget_exhausted":
                self._escalate(db, case, now, reason=why, actor=C, resumable=False, st=st)
            else:
                merge_facts(case, budget_wait=why)
                reschedule(case, now=now, delay=timedelta(minutes=15))
            return False
        return True

    # ── owner resumes (persisted intent, no new reasoning) ────────────────
    def _resume_sweep(self, now: datetime, st: KernelStats) -> None:
        db = self.session_factory()
        try:
            rows = (
                db.query(RepairCase)
                .filter(RepairCase.state == S.SAFELY_ESCALATED.value, RepairCase.resumable.is_(True), RepairCase.terminal_reason == "owner_approval_required")
                .with_for_update(skip_locked=True)
                .limit(20)
                .all()
            )
            for case in rows:
                promo = db.get(CodePromotion, case.promotion_id) if case.promotion_id else None
                if promo is None or getattr(promo.status, "value", promo.status) != PromotionStatus.MERGED.value:
                    continue
                approval = {"kind": "owner_merged_pr", "promotion_id": str(promo.id), "pr": promo.external_pr_number, "merged_sha": promo.merged_sha,
                            "approvals": list((promo.eligibility or {}).get("human_approvals") or []), "observed_at": now.isoformat()}
                merge_facts(case, owner_approval=approval)
                case.merged_sha = promo.merged_sha or promo.candidate_sha
                transition(db, case, S.READY_FOR_RELEASE, actor=O, actor_id="owner:github-merge", reason="owner_approved_merge", now=now, detail=approval, evidence_digest=digest(approval))
                db.add(MaintenanceToilEvent(id=uuid.uuid4(), kind="owner_approval", case_id=case.id, actor="owner", detail=approval, created_at=now))
                st.resumed += 1
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("maintenance: resume sweep failed")
        finally:
            db.close()

    # ── stall detector (the deadlock-impossibility assertion) ──────────────
    def _stall_check(self, now: datetime, st: KernelStats) -> None:
        db = self.session_factory()
        try:
            n = stranded_count(db, now=now, stall_seconds=self.settings.stall_seconds)
            if not n:
                inc_mod.observe_healthy(db, self.settings, target=self.settings.target, desired_state_ref="kernel:case_liveness", sli="maintenance_liveness",
                                        source="maintenance_watchdog", collector_version=KERNEL_VERSION, now=now, record=False)
            if n:
                st.stalls = n
                v = inc_mod.Violation(target=self.settings.target, incident_class=IncidentClass.CONTROL_PLANE, desired_state_ref="kernel:case_liveness",
                                      failure="control_plane_stall", severity=Severity.MAJOR, base_priority=Priority.P1, source="maintenance_watchdog",
                                      collector_version=KERNEL_VERSION, sli="maintenance_liveness", trust_class=TrustClass.TRUSTED_DB, payload={"stranded_cases": n})
                inc_mod.ingest_violation(db, self.settings, v, now=now)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("maintenance: stall check failed")
        finally:
            db.close()

    # ── plans / attempts ──────────────────────────────────────────────────
    def _contract_id(self, incident: MaintenanceIncident) -> str:
        return (incident.provenance or {}).get("contract") or DEFAULT_CONTRACT.get(incident.incident_class, "public_surface")

    def _plan(self, db, case) -> Optional[RepairPlanRevision]:
        return db.query(RepairPlanRevision).filter(RepairPlanRevision.case_id == case.id, RepairPlanRevision.revision == case.current_plan_revision).first()

    def _attempt(self, db, case) -> Optional[RepairAttempt]:
        return db.query(RepairAttempt).filter(RepairAttempt.case_id == case.id, RepairAttempt.attempt == case.attempt_count).first()

    def _new_attempt(self, db, case, plan, now) -> RepairAttempt:
        existing = self._attempt(db, case)
        if existing is not None:
            return existing
        a = RepairAttempt(id=uuid.uuid4(), case_id=case.id, attempt=case.attempt_count, plan_revision=plan.revision if plan else 0, outcome="running", feedback={}, started_at=now, turns=0, test_runs=0)
        db.add(a)
        db.flush()
        return a

    def _make_plan(self, db, case, incident, out: Dict[str, Any], rev: int, *, parent: Optional[int], rescope_reason: Optional[str]) -> Tuple[Optional[RepairPlanRevision], List[str]]:
        """Deterministic validation of a model-designed plan. The contract's
        own verification tests are ALWAYS acceptance tests; the model can add
        existing tests, never remove the trusted ones."""
        root = pathlib.Path(self.society.repo_root)
        problems: List[str] = []
        files: List[str] = []
        for f in out.get("files_allowed") or []:
            f = str(f).strip()
            while f.startswith("./"):
                f = f[2:]
            if not f or f.startswith("/") or ".." in f.split("/"):
                problems.append(f"invalid path {f!r}")
                continue
            if not ((root / f).exists() or (root / f).parent.is_dir()):
                problems.append(f"{f} does not exist and its directory does not either")
                continue
            if f not in files:
                files.append(f)
        if parent is not None:
            prev = db.query(RepairPlanRevision).filter(RepairPlanRevision.case_id == case.id, RepairPlanRevision.revision == parent).first()
            # a revision never silently drops scope the Builder already needed; it only adds
            for f in (prev.files_allowed if prev else []):
                if f not in files:
                    files.append(f)
        if not files:
            problems.append("files_allowed is empty")
        if len(files) > self.settings.max_files * 2:
            problems.append(f"too many files ({len(files)})")
        try:
            contract = load_registry().get(self._contract_id(incident))
            trusted_tests = [t for t in contract.verification_tests if (root / t.split("::")[0]).is_file()]
        except KeyError:
            contract, trusted_tests = None, []
        tests: List[str] = list(trusted_tests)
        for t in out.get("acceptance_tests") or []:
            t = str(t).strip()
            p = t.split("::")[0]
            if (root / p).is_file() and pathlib.PurePosixPath(p).name.startswith("test_") and t not in tests:
                tests.append(t)
        self_judging = [f for f in files if f in {t.split("::")[0] for t in tests}]
        if self_judging:
            problems.append(f"acceptance tests may not be in files_allowed (self-judging): {self_judging}")
        if not tests:
            problems.append("no trusted acceptance test exists for this contract; the repair could not be verified")
        if problems:
            return None, problems
        decision = pol.classify_paths(files, settings=self.settings)
        body = {"revision": rev, "parent": parent, "files_allowed": files, "acceptance_tests": tests, "contract_refs": [c for c in (out.get("contract_refs") or []) if isinstance(c, str)][:12],
                "root_cause": out.get("root_cause", ""), "approach": out.get("approach", ""), "risk": decision.to_dict()}
        plan = RepairPlanRevision(
            id=uuid.uuid4(), case_id=case.id, revision=rev, parent_revision=parent, files_allowed=files, acceptance_tests=tests, contract_refs=body["contract_refs"],
            root_cause=str(out.get("root_cause", ""))[:4000], approach=str(out.get("approach", ""))[:6000], risk_class=decision.risk_class.value,
            risk_reasons=decision.reasons + decision.findings, rescope_reason=rescope_reason, digest=digest(body),
        )
        db.add(plan)
        db.flush()
        case.current_plan_revision = rev
        return plan, []

    # ── activities ────────────────────────────────────────────────────────
    def _activity_tries(self, db, case, kind: ActivityKind, rev: int, attempt: int) -> int:
        return db.query(RepairActivity).filter(RepairActivity.case_id == case.id, RepairActivity.kind == kind.value, RepairActivity.plan_revision == rev, RepairActivity.attempt == attempt).count()

    def _run_activity_sync(self, db, case, kind: ActivityKind, payload: Dict[str, Any], *, tools, model, plan_revision: int = 0, attempt: int = 0, max_turns: Optional[int] = None,
                           max_read_calls: Optional[int] = None, submit_check=None) -> Optional[act.ActivityResult]:
        """Record the try (committed), run the model loop outside any open
        transaction, then fence and record the outcome. Returns None when the
        lease was lost meanwhile."""
        spec = act.SPECS[kind]
        # a crashed earlier try of this activity: abandoned, and it counts
        for stale in db.query(RepairActivity).filter(RepairActivity.case_id == case.id, RepairActivity.status == ActivityStatus.RUNNING.value).all():
            stale.status = ActivityStatus.ABANDONED.value
            stale.error_class = "abandoned"
            stale.finished_at = utcnow()
        try_no = self._activity_tries(db, case, kind, plan_revision, attempt) + 1
        row = RepairActivity(
            id=uuid.uuid4(), case_id=case.id, kind=kind.value, role=spec.role, plan_revision=plan_revision, attempt=attempt, try_number=try_no,
            idempotency_key=f"{case.id}:{kind.value}:r{plan_revision}:a{attempt}:t{try_no}", status=ActivityStatus.RUNNING.value,
            input_digest=digest(payload), model_provider=getattr(model, "provider", "?"), model_name=getattr(model, "model_name", "?"),
            lease_owner=self.worker_id, lease_expires_at=utcnow() + timedelta(seconds=self.settings.lease_seconds), started_at=utcnow(),
        )
        db.add(row)
        case_id, row_id = case.id, row.id
        db.commit()  # the try is durable before the model is called
        remaining = max(Decimal("0.0001"), self.settings.max_case_cost_usd * (2 if case.priority in pol.URGENT else 1) - Decimal(str(case.model_cost_usd or 0)))
        try:
            res = asyncio.run(act.run_activity(spec, payload, model=model, tools=tools, max_turns=max_turns, cost_cap=remaining,
                                               timeout_seconds=float(self._activity_timeout or self.settings.activity_timeout_seconds),
                                               max_read_calls=max_read_calls, submit_check=submit_check))
        except Exception as exc:  # noqa: BLE001
            res = act.ActivityResult(ok=False, kind=kind, error_class="harness_error", error=type(exc).__name__)
        case = self._fence(db, case_id)
        row = db.get(RepairActivity, row_id)
        row.status = ActivityStatus.SUCCEEDED.value if res.ok else ActivityStatus.FAILED.value
        row.error_class = res.error_class
        row.error = (res.error or "")[:500] or None
        row.tokens_in, row.tokens_out, row.cost_usd, row.turns = res.tokens_in, res.tokens_out, res.cost_usd, res.turns
        row.turn_log = list(res.turn_log)[:40]
        row.output_digest = digest(res.output or res.rescope or {}) if res.ok else None
        row.finished_at = utcnow()
        row.lease_owner = None
        case.model_cost_usd = Decimal(str(case.model_cost_usd or 0)) + res.cost_usd
        case.model_calls = int(case.model_calls or 0) + res.turns
        db.flush()
        return res

    def _activity_outcome(self, db, case, res: Optional[act.ActivityResult], kind: ActivityKind, now, st, *, plan_revision: int = 0, attempt: int = 0) -> bool:
        """True when the activity succeeded. On failure: retry later within the
        try budget, else the state's exhaustion target -- never stranded."""
        if res is None:
            return False
        if res.ok:
            return True
        tries = self._activity_tries(db, case, kind, plan_revision, attempt)
        if tries < self.settings.activity_max_tries:
            backoff = timedelta(seconds=60 if res.error_class == "rate_limit" else 20 * tries)
            reschedule(case, now=now, delay=backoff)
            self._activity_feedback(db, case, kind, [f"{res.error_class}: {res.error}"], now)
            return False
        self._escalate(db, case, now, reason=f"activity_exhausted:{kind.value}"[:64], actor=A, resumable=False, st=st)
        return False

    def _activity_feedback(self, db, case, kind: ActivityKind, problems: List[str], now) -> None:
        record_artifact(db, case, kind="activity_feedback", content={"activity": kind.value, "problems": [str(p)[:300] for p in problems][:10], "at": now.isoformat()},
                        trust_class=TrustClass.TRUSTED_EXECUTION, produced_by=self.worker_id, plan_revision=case.current_plan_revision, attempt=case.attempt_count, now=now)

    def _latest_artifact(self, db, case, kind: str, *, plan_revision: Optional[int] = None, attempt: Optional[int] = None) -> Optional[RepairArtifact]:
        q = db.query(RepairArtifact).filter(RepairArtifact.case_id == case.id, RepairArtifact.kind == kind)
        if plan_revision is not None:
            q = q.filter(RepairArtifact.plan_revision == plan_revision)
        if attempt is not None:
            q = q.filter(RepairArtifact.attempt == attempt)
        return q.order_by(RepairArtifact.created_at.desc()).first()

    def _feedback(self, db, case) -> List[Dict[str, Any]]:
        rows = (
            db.query(RepairArtifact)
            .filter(RepairArtifact.case_id == case.id, RepairArtifact.kind.in_(["attempt_feedback", "activity_feedback", "rescope_request"]))
            .order_by(RepairArtifact.created_at.desc())
            .limit(8)
            .all()
        )
        return [{"kind": r.kind, "trust": r.trust_class, "plan_revision": r.plan_revision, "attempt": r.attempt, "content": r.content} for r in rows]

    def _incident_input(self, db, case, incident: MaintenanceIncident) -> Dict[str, Any]:
        last = (
            db.query(MaintenanceObservation)
            .filter(MaintenanceObservation.incident_id == incident.id, MaintenanceObservation.ok.is_(False))
            .order_by(MaintenanceObservation.observed_at.desc())
            .first()
        )
        contract_id = self._contract_id(incident)
        try:
            c = load_registry().get(contract_id)
            contract = {"id": c.id, "description": c.description, "verification_tests": list(c.verification_tests)}
        except KeyError:
            contract = {"id": contract_id}
        prior = know_mod.prior_knowledge(db, incident.fingerprint)
        return {
            "incident": {"class": incident.incident_class, "priority": incident.priority, "severity": incident.severity, "desired_state_ref": incident.desired_state_ref,
                         "target": incident.target, "observations": incident.observation_count, "affected_surfaces": incident.affected_surfaces,
                         "latest_structural_evidence": (last.payload if last else {}), "recurrence_count": incident.recurrence_count},
            "contract": contract,
            "prior_cases": prior,
            "evidence_note": "Structural evidence from trusted probes. Page content is never included. Do not edit the contract, detectors or verification tests.",
        }

    def _attestation(self, db, case, incident, now) -> Dict[str, Any]:
        cand = db.get(CodeCandidate, case.candidate_id) if case.candidate_id else None
        promo = db.get(CodePromotion, case.promotion_id) if case.promotion_id else None
        ver = self._latest_artifact(db, case, "verification")
        sec = self._latest_artifact(db, case, "security_review")
        kg = db.query(MaintenanceKnownGood).filter(MaintenanceKnownGood.retired_at.is_(None)).order_by(MaintenanceKnownGood.recorded_at.desc()).first()
        v = (ver.content if ver else {}) or {}
        browser = (
            db.query(MaintenanceObservation)
            .filter(MaintenanceObservation.sli == "browser_journey", MaintenanceObservation.target == self.settings.target)
            .order_by(MaintenanceObservation.observed_at.desc())
            .first()
        )
        return {
            "version": att_mod.ATTESTATION_VERSION,
            "incident_id": str(case.incident_id),
            "repair_case_id": str(case.id),
            "incident_fingerprint": incident.fingerprint if incident else None,
            "base_sha": case.base_sha,
            "head_sha": case.head_sha,
            "merged_sha": case.merged_sha,
            "tree_sha": None,
            "diff_digest": v.get("diff_digest"),
            "changed_files": sorted(v.get("changed") or (cand.changed_files if cand else []) or []),
            "risk_decision": {"class": case.risk_class, "reasons": ((v.get("risk") or {}).get("reasons") or [])[:20]},
            "ci": {"promotion_id": str(promo.id) if promo else None, "pr": promo.external_pr_number if promo else None, "state": promo.ci_state if promo else None},
            "qa_verdict": (v.get("qa") or {}).get("verdict"),
            "security_verdict": (sec.content or {}).get("verdict") if sec else None,
            "staging_proof": (case.facts or {}).get("staging_evidence"),
            "browser_proof": None if browser is None else {"ok": browser.ok, "digest": browser.digest, "observed_at": browser.observed_at.isoformat()},
            "contract_proof": {"acceptance_tests": (self._plan(db, case).acceptance_tests if self._plan(db, case) else []), "qa": (v.get("qa") or {}).get("verdict")},
            "slo_state": {"exhausted": slo_mod.error_budget_exhausted(db, target=self.settings.target, now=now)},
            "known_good_production_sha": kg.production_sha if kg else None,
            "owner_approval": (case.facts or {}).get("owner_approval"),
            "priority": case.priority,
            "created_at": now.isoformat(),
        }


def stranded_count(db: Session, *, now: Optional[datetime] = None, stall_seconds: int = 900) -> int:
    """The formal graduation invariant (ADR-0010 D6):

        COUNT(non-terminal cases with no active lease, and next_action_at is
              null or overdue by more than the stall bound, and not awaiting
              explicit approval) = 0

    Non-terminal rows cannot have a null next_action_at (DB constraint);
    awaiting approval is the terminal-but-resumable SAFELY_ESCALATED."""
    now = now or utcnow()
    row = db.execute(
        text(
            """
            SELECT COUNT(*) FROM repair_cases
             WHERE state NOT IN ('AUTO_REPAIRED','AUTO_ROLLED_BACK','SAFELY_ESCALATED','CANNOT_REPRODUCE','DUPLICATE_RESOLVED','POLICY_REFUSED')
               AND (lease_expires_at IS NULL OR lease_expires_at < :now)
               AND (next_action_at IS NULL OR next_action_at < :stale)
            """
        ),
        {"now": now, "stale": now - timedelta(seconds=stall_seconds)},
    ).scalar()
    return int(row or 0)


__all__ = ["MaintenanceKernel", "KernelStats", "LeaseLost", "stranded_count", "KERNEL_VERSION"]
