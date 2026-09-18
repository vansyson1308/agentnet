"""Deployment provider interface + durable deployment/rollback requests.

No hosting provider exists in this phase. The interface is real, the state
is durable, and the only implementations are:

* ``DisabledDeploymentProvider`` — every request becomes BLOCKED_EXTERNAL
  (never a fake success);
* ``FakeDeploymentProvider`` — in-memory, deterministic, for tests of the
  request/observe/rollback lifecycle.

Production is not an environment a provider can serve: a production request
is persisted as REFUSED with the reason, and there is no executor path that
could ever turn it into a deploy (hard OFF, see config.production_deploy_enabled).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Protocol

from sqlalchemy.orm import Session

from ..models import CodeCandidate, CodePromotion, DeploymentRequest, DeploymentRequestStatus
from .config import SocietySettings
from .events import EventType, emit_event, utcnow

logger = logging.getLogger(__name__)

STAGING = "staging"
PRODUCTION = "production"


class DeploymentUnavailable(Exception):
    """No provider is configured (or it cannot serve the environment)."""


@dataclass
class DeploymentStatus:
    status: str          # in_progress | succeeded | failed | rolled_back
    external_ref: str = ""
    note: str = ""


class DeploymentProvider(Protocol):
    name: str

    def request_staging(self, request: DeploymentRequest) -> DeploymentStatus: ...  # pragma: no cover - protocol
    def observe_status(self, request: DeploymentRequest) -> DeploymentStatus: ...  # pragma: no cover
    def request_rollback(self, request: DeploymentRequest, original: DeploymentRequest) -> DeploymentStatus: ...  # pragma: no cover


class DisabledDeploymentProvider:
    name = "disabled"

    def request_staging(self, request: DeploymentRequest) -> DeploymentStatus:
        raise DeploymentUnavailable("no deployment provider configured (SOCIETY_DEPLOYMENT_PROVIDER=disabled)")

    def observe_status(self, request: DeploymentRequest) -> DeploymentStatus:
        raise DeploymentUnavailable("no deployment provider configured")

    def request_rollback(self, request: DeploymentRequest, original: DeploymentRequest) -> DeploymentStatus:
        raise DeploymentUnavailable("no deployment provider configured")


class FakeDeploymentProvider:
    """Deterministic test double: staging deploys succeed (or fail when told
    to), statuses are observable, rollbacks recorded. Never touches a host."""

    name = "fake"

    def __init__(self, *, fail_targets: Optional[set] = None):
        self.fail_targets = set(fail_targets or ())
        self.calls: list = []
        self._state: Dict[uuid.UUID, DeploymentStatus] = {}

    def request_staging(self, request: DeploymentRequest) -> DeploymentStatus:
        self.calls.append(("request_staging", str(request.id), request.target_sha))
        if request.environment != STAGING:
            raise DeploymentUnavailable(f"fake provider serves staging only, not {request.environment}")
        ok = request.target_sha not in self.fail_targets
        st = DeploymentStatus("succeeded" if ok else "failed", external_ref=f"fake-deploy-{str(request.id)[:8]}", note="fake provider")
        self._state[request.id] = st
        return st

    def observe_status(self, request: DeploymentRequest) -> DeploymentStatus:
        self.calls.append(("observe_status", str(request.id)))
        return self._state.get(request.id, DeploymentStatus("in_progress", note="fake provider: unknown request"))

    def request_rollback(self, request: DeploymentRequest, original: DeploymentRequest) -> DeploymentStatus:
        self.calls.append(("request_rollback", str(request.id), str(original.id)))
        st = DeploymentStatus("rolled_back", external_ref=f"fake-rollback-{str(request.id)[:8]}", note=f"rolled back {original.external_ref}")
        self._state[request.id] = st
        self._state[original.id] = DeploymentStatus("rolled_back", external_ref=original.external_ref or "", note="superseded by rollback")
        return st


def get_deployment_provider(settings: SocietySettings, *, override: Optional[DeploymentProvider] = None) -> DeploymentProvider:
    if override is not None:
        return override
    if settings.deployment_provider == "fake":
        return FakeDeploymentProvider()
    return DisabledDeploymentProvider()


def request_deployment(
    db: Session,
    *,
    settings: SocietySettings,
    provider: DeploymentProvider,
    environment: str,
    candidate: Optional[CodeCandidate],
    promotion: Optional[CodePromotion],
    correlation_id: uuid.UUID,
    target_sha: Optional[str],
    requested_by_agent_id: Optional[uuid.UUID],
    causation=None,
    source_run_id: Optional[uuid.UUID] = None,
    kind: str = "deploy",
    rollback_of: Optional[DeploymentRequest] = None,
) -> DeploymentRequest:
    """Create a durable request and hand it to the provider. Does NOT commit.
    Idempotent per (environment, target_sha, kind, promotion): an existing
    non-terminal request is returned unchanged."""
    if environment == PRODUCTION or settings.production_deploy_enabled:
        # Recorded, never executed: no executor path exists for production.
        req = DeploymentRequest(
            id=uuid.uuid4(),
            candidate_id=candidate.id if candidate else None,
            promotion_id=promotion.id if promotion else None,
            correlation_id=correlation_id,
            environment=environment,
            kind=kind,
            target_sha=target_sha,
            provider=getattr(provider, "name", "disabled"),
            status=DeploymentRequestStatus.REFUSED,
            note="production deployment is hard OFF in this phase; request recorded only",
            requested_by_agent_id=requested_by_agent_id,
        )
        db.add(req)
        db.flush()
        emit_event(db, event_type=EventType.DEPLOYMENT_REFUSED, payload={"request_id": str(req.id), "environment": environment, "reason": req.note}, actor_type="system", subject_type="deployment_request", subject_id=req.id, correlation_id=correlation_id, causation=causation, idempotency_key=f"deployment-refused:{req.id}", source_run_id=source_run_id, notify=False)
        return req
    existing = (
        db.query(DeploymentRequest)
        .filter(
            DeploymentRequest.environment == environment,
            DeploymentRequest.kind == kind,
            DeploymentRequest.target_sha == target_sha,
            DeploymentRequest.promotion_id == (promotion.id if promotion else None),
            DeploymentRequest.status.in_([DeploymentRequestStatus.REQUESTED, DeploymentRequestStatus.BLOCKED_EXTERNAL, DeploymentRequestStatus.IN_PROGRESS]),
        )
        .first()
    )
    if existing is not None:
        return existing
    req = DeploymentRequest(
        id=uuid.uuid4(),
        candidate_id=candidate.id if candidate else None,
        promotion_id=promotion.id if promotion else None,
        correlation_id=correlation_id,
        environment=environment,
        kind=kind,
        target_sha=target_sha,
        provider=getattr(provider, "name", "disabled"),
        status=DeploymentRequestStatus.REQUESTED,
        rollback_of=rollback_of.id if rollback_of else None,
        requested_by_agent_id=requested_by_agent_id,
    )
    db.add(req)
    db.flush()
    emit_event(db, event_type=EventType.DEPLOYMENT_REQUESTED, payload={"request_id": str(req.id), "environment": environment, "kind": kind, "target_sha": target_sha, "provider": req.provider}, actor_type="system", subject_type="deployment_request", subject_id=req.id, correlation_id=correlation_id, causation=causation, idempotency_key=f"deployment-requested:{req.id}", source_run_id=source_run_id, notify=False)
    try:
        if kind == "rollback" and rollback_of is not None:
            st = provider.request_rollback(req, rollback_of)
        else:
            st = provider.request_staging(req)
    except DeploymentUnavailable as exc:
        req.status = DeploymentRequestStatus.BLOCKED_EXTERNAL
        req.note = f"blocked: {exc}"
        req.updated_at = utcnow()
        emit_event(db, event_type=EventType.DEPLOYMENT_BLOCKED, payload={"request_id": str(req.id), "environment": environment, "reason": str(exc)[:300]}, actor_type="system", subject_type="deployment_request", subject_id=req.id, correlation_id=correlation_id, causation=causation, idempotency_key=f"deployment-blocked:{req.id}", source_run_id=source_run_id, notify=False)
        return req
    _apply_status(req, st)
    return req


def _apply_status(req: DeploymentRequest, st: DeploymentStatus) -> None:
    mapping = {
        "in_progress": DeploymentRequestStatus.IN_PROGRESS,
        "succeeded": DeploymentRequestStatus.SUCCEEDED,
        "failed": DeploymentRequestStatus.FAILED,
        "rolled_back": DeploymentRequestStatus.ROLLED_BACK,
    }
    req.status = mapping.get(st.status, DeploymentRequestStatus.IN_PROGRESS)
    req.external_ref = st.external_ref or req.external_ref
    req.note = st.note or req.note
    req.updated_at = utcnow()


def observe_requests(db: Session, provider: DeploymentProvider, *, limit: int = 50) -> int:
    """Refresh IN_PROGRESS requests from the provider. Commits. Returns updated count."""
    rows = db.query(DeploymentRequest).filter(DeploymentRequest.status == DeploymentRequestStatus.IN_PROGRESS).limit(limit).all()
    n = 0
    for req in rows:
        try:
            st = provider.observe_status(req)
        except DeploymentUnavailable:
            continue
        _apply_status(req, st)
        n += 1
    if n:
        db.commit()
    return n


__all__ = [
    "DeploymentProvider",
    "DeploymentStatus",
    "DeploymentUnavailable",
    "DisabledDeploymentProvider",
    "FakeDeploymentProvider",
    "get_deployment_provider",
    "request_deployment",
    "observe_requests",
    "STAGING",
    "PRODUCTION",
]
