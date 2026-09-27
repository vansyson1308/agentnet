"""Stable machine vocabularies for maintenance (ADR-0010 D3).

Every value here is persisted and compared by code. Adding a value is an
additive change; renaming one is a migration. There is no free-text category
anywhere in the maintenance data model.
"""

from __future__ import annotations

import enum
from typing import Dict, Tuple


class IncidentClass(str, enum.Enum):
    AVAILABILITY = "AVAILABILITY"
    FUNCTIONAL_CONTRACT = "FUNCTIONAL_CONTRACT"
    AUTH = "AUTH"
    A2A = "A2A"
    UI_NAVIGATION = "UI_NAVIGATION"
    UI_RENDERING = "UI_RENDERING"
    ACCESSIBILITY = "ACCESSIBILITY"
    PERFORMANCE = "PERFORMANCE"
    SECURITY = "SECURITY"
    DEPENDENCY = "DEPENDENCY"
    ECONOMIC_INVARIANT = "ECONOMIC_INVARIANT"
    DATA_INVARIANT = "DATA_INVARIANT"
    RELEASE_REGRESSION = "RELEASE_REGRESSION"
    CONTROL_PLANE = "CONTROL_PLANE"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"


class Priority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


PRIORITY_RANK: Dict[Priority, int] = {Priority.P0: 0, Priority.P1: 1, Priority.P2: 2, Priority.P3: 3}


class Severity(str, enum.Enum):
    """Observation severity as the collectors report it (surface.py uses the
    same three words)."""

    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class IncidentStatus(str, enum.Enum):
    OPEN = "open"            # the product violates desired state now (or did, and no recovery streak yet)
    RECOVERED = "recovered"  # a healthy streak was observed; kept for recurrence linking
    CLOSED = "closed"        # closed by a terminal case outcome that is not a recovery (e.g. duplicate)


class TrustClass(str, enum.Enum):
    """Where a piece of evidence came from. Only TRUSTED_* classes may change
    workflow state; MODEL_HYPOTHESIS is interpretation, never fact."""

    TRUSTED_PROBE = "trusted_probe"          # deterministic monitor / browser probe
    TRUSTED_CI = "trusted_ci"                # CI / required checks
    TRUSTED_PROVIDER = "trusted_provider"    # GitHub / Railway API state
    TRUSTED_DB = "trusted_db"                # database invariant / durable row
    TRUSTED_EXECUTION = "trusted_execution"  # a test run / patch application we executed
    MODEL_HYPOTHESIS = "model_hypothesis"    # model-authored text (untrusted)


TRUSTED_EVIDENCE = frozenset(t for t in TrustClass if t is not TrustClass.MODEL_HYPOTHESIS)


class ActivityKind(str, enum.Enum):
    DIAGNOSE_INCIDENT = "DiagnoseIncident"
    DESIGN_REPAIR = "DesignRepair"
    AUTHOR_PATCH = "AuthorPatch"
    REVIEW_PATCH = "ReviewPatch"
    SECURITY_REVIEW = "SecurityReview"
    EXPLAIN_ESCALATION = "ExplainEscalation"
    DRAFT_POSTMORTEM = "DraftPostmortem"


class ActivityStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"          # this try failed; the policy decides whether another try follows
    ABANDONED = "abandoned"    # lease expired mid-try (crash); counted as a failed try


class MaintenanceRiskClass(str, enum.Enum):
    """Trusted maintenance risk (policy.py). Distinct from the path tier in
    society/risk.py, which is one of its inputs."""

    MAINTENANCE_GREEN = "MAINTENANCE_GREEN"
    AMBER = "AMBER"
    RED = "RED"
    CONSTITUTIONAL = "CONSTITUTIONAL"   # NEVER tier / constitutional path: refused outright


RISK_ORDER: Dict[MaintenanceRiskClass, int] = {
    MaintenanceRiskClass.MAINTENANCE_GREEN: 0,
    MaintenanceRiskClass.AMBER: 1,
    MaintenanceRiskClass.RED: 2,
    MaintenanceRiskClass.CONSTITUTIONAL: 3,
}


def risk_max(a: MaintenanceRiskClass, b: MaintenanceRiskClass) -> MaintenanceRiskClass:
    return a if RISK_ORDER[a] >= RISK_ORDER[b] else b


class ReleaseStatus(str, enum.Enum):
    PENDING = "pending"                          # attested, waiting for the release controller
    VERIFYING = "verifying"                      # controller recomputing the attestation
    PREVIEW_VALIDATING = "preview_validating"    # exact SHA validated on the release-preview surface
    PR_OPEN = "pr_open"                          # release/prod-<sha> -> production PR open
    MERGED = "merged"                            # production PR merged (required CI passed)
    DEPLOYING = "deploying"                      # waiting for the provider deployment of that SHA
    POST_DEPLOY_VERIFYING = "post_deploy_verifying"
    SUCCEEDED = "succeeded"                      # N consecutive healthy public observations
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"                  # known-good restored AND public desired state healthy
    ROLLBACK_FAILED = "rollback_failed"          # P0 escalation + release freeze
    REFUSED = "refused"                          # controller verification failed closed


RELEASE_TERMINAL = frozenset({ReleaseStatus.SUCCEEDED, ReleaseStatus.ROLLED_BACK, ReleaseStatus.ROLLBACK_FAILED, ReleaseStatus.REFUSED})
RELEASE_IN_FLIGHT = frozenset(set(ReleaseStatus) - RELEASE_TERMINAL)


class ActorType(str, enum.Enum):
    CONTROLLER = "controller"   # the reconciler (deterministic)
    WATCHDOG = "watchdog"       # deadline / stall recovery (deterministic)
    RELEASE = "release"         # the release controller (deterministic)
    ACTIVITY = "activity"       # the outcome of a cognitive activity, applied by the controller
    OWNER = "owner"             # an operator decision (approval / refusal / abandon)
    MONITOR = "monitor"         # a trusted observation


# ── classification of existing observation sources ────────────────────────

#: surface.py failure class -> (incident class, base priority)
SURFACE_FAILURE_CLASS: Dict[str, Tuple[IncidentClass, Priority]] = {
    "unreachable": (IncidentClass.AVAILABILITY, Priority.P0),
    "timeout": (IncidentClass.AVAILABILITY, Priority.P1),
    "server_error": (IncidentClass.AVAILABILITY, Priority.P1),
    "client_error": (IncidentClass.FUNCTIONAL_CONTRACT, Priority.P2),
    "unexpected_status": (IncidentClass.FUNCTIONAL_CONTRACT, Priority.P2),
    "masked_by_landing_redirect": (IncidentClass.UI_NAVIGATION, Priority.P2),
    "wrong_final_path": (IncidentClass.UI_NAVIGATION, Priority.P2),
    "marker_missing": (IncidentClass.UI_RENDERING, Priority.P2),
    "redirect_loop": (IncidentClass.UI_NAVIGATION, Priority.P1),
    "offsite_redirect": (IncidentClass.SECURITY, Priority.P1),
    "response_too_large": (IncidentClass.PERFORMANCE, Priority.P3),
    "placeholder_link": (IncidentClass.UI_NAVIGATION, Priority.P2),
    "empty_asset": (IncidentClass.UI_RENDERING, Priority.P3),
}

#: Public-surface contract items whose failure breaks an auth/A2A-critical
#: journey (their incident class, and so a P1 floor).
CRITICAL_JOURNEY_ITEMS: Dict[str, IncidentClass] = {
    "login": IncidentClass.AUTH,
    "register": IncidentClass.AUTH,
    "a2a_agent_card": IncidentClass.A2A,
}


def priority_for(incident_class: IncidentClass, severity: Severity, base: Priority) -> Priority:
    """Deterministic priority (ADR-0010 D12). The model never sets it."""
    if incident_class in (IncidentClass.ECONOMIC_INVARIANT, IncidentClass.DATA_INVARIANT):
        return Priority.P0
    if incident_class is IncidentClass.SECURITY:
        return Priority.P0 if severity is Severity.CRITICAL else min_priority(base, Priority.P1)
    if incident_class is IncidentClass.AVAILABILITY and severity is Severity.CRITICAL:
        return Priority.P0
    if incident_class in (IncidentClass.AUTH, IncidentClass.A2A):
        return min_priority(base, Priority.P1)
    if severity is Severity.MINOR:
        return Priority.P3
    return base


def min_priority(a: Priority, b: Priority) -> Priority:
    """The more urgent of two priorities."""
    return a if PRIORITY_RANK[a] <= PRIORITY_RANK[b] else b
