"""Promotion Controller: READY candidate -> branch -> PR -> CI -> evaluation
-> merge eligibility. Deterministic, NON-LLM, lease-based, idempotent.

Trust model
-----------
* The model can only emit ``REQUEST_PR_PROMOTION`` (executor.py). Everything
  after that is this module, driven by the society worker, never by a
  decision text.
* Risk is classified by the TRUSTED BASE ``risk.py`` (the module imported by
  the running controller) from the candidate's real diff. A candidate that
  edits ``risk.py``/``policy.py``/``fitness.py`` in its worktree is still
  classified by the base copy (tests/society/test_risk_and_meta_change.py).
* The provider is the only component that ever holds a GitHub credential
  (``promotion_github.py`` reads it from its own environment variable at call
  time; cognition, context and this controller never see it).
* ``main``/the configured base branch can never be a publish target; there is
  no force option anywhere in the provider interface.
* Merge eligibility is computed from persisted facts (CI, QA, Security,
  trusted tier, fitness experiment, approvals, change budget). Auto-merge is
  OFF by default, refused with the GitHub provider in this phase, and even
  when on applies to GREEN only.

Crash safety
------------
Every transition is persisted before the next side effect; provider
operations are idempotent per candidate (deterministic branch name, PR
lookup by head branch). A worker that dies mid-step leaves a lease that
expires; the next claim re-runs the SAME step and converges without a second
branch or PR (tests/society/test_promotion_crash_recovery.py).
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    AgentRun,
    ChangeExperiment,
    CodeCandidate,
    CodeCandidateStatus,
    CodePromotion,
    ExperimentStatus,
    PromotionStatus,
    RiskTier,
    SocietyEvent,
)
from .config import SocietySettings
from .engineering import workspace as ws_mod
from .engineering.qa import static_security_scan
from .events import EventType, emit_event, utcnow
from .risk import assess as assess_risk

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (
    PromotionStatus.REQUESTED,
    PromotionStatus.VALIDATING,
    PromotionStatus.BRANCH_READY,
    PromotionStatus.PR_OPEN,
    PromotionStatus.CI_PENDING,
    PromotionStatus.CI_PASSED,
    PromotionStatus.AWAITING_APPROVAL,
    PromotionStatus.MERGE_ELIGIBLE,
    PromotionStatus.BLOCKED_EXTERNAL,
)
TERMINAL_STATUSES = (PromotionStatus.MERGED, PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED)
OPEN_PR_STATUSES = (PromotionStatus.PR_OPEN, PromotionStatus.CI_PENDING, PromotionStatus.CI_PASSED, PromotionStatus.CI_FAILED, PromotionStatus.AWAITING_APPROVAL, PromotionStatus.MERGE_ELIGIBLE)

# Legal forward transitions; anything else is refused (state-machine tests).
TRANSITIONS: Dict[PromotionStatus, set] = {
    PromotionStatus.REQUESTED: {PromotionStatus.VALIDATING, PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED},
    PromotionStatus.VALIDATING: {PromotionStatus.BRANCH_READY, PromotionStatus.BLOCKED_EXTERNAL, PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED},
    PromotionStatus.BLOCKED_EXTERNAL: {PromotionStatus.VALIDATING, PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED},
    PromotionStatus.BRANCH_READY: {PromotionStatus.PR_OPEN, PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED},
    PromotionStatus.PR_OPEN: {PromotionStatus.CI_PENDING, PromotionStatus.CI_PASSED, PromotionStatus.CI_FAILED, PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED, PromotionStatus.MERGED},
    PromotionStatus.CI_PENDING: {PromotionStatus.CI_PASSED, PromotionStatus.CI_FAILED, PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED, PromotionStatus.MERGED},
    # CI_PENDING is reachable again from every post-CI state: when the base moves,
    # the controller reconciles the branch and the required checks MUST re-run on
    # the new head. Going backwards here is the point -- a promotion that has
    # already passed CI has NOT passed it on the commit that would now be merged.
    PromotionStatus.CI_PASSED: {PromotionStatus.AWAITING_APPROVAL, PromotionStatus.MERGE_ELIGIBLE, PromotionStatus.CI_PENDING, PromotionStatus.CI_FAILED, PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED, PromotionStatus.MERGED},
    PromotionStatus.CI_FAILED: {PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED},
    PromotionStatus.AWAITING_APPROVAL: {PromotionStatus.MERGE_ELIGIBLE, PromotionStatus.MERGED, PromotionStatus.CI_PENDING, PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED, PromotionStatus.CI_FAILED},
    PromotionStatus.MERGE_ELIGIBLE: {PromotionStatus.MERGED, PromotionStatus.REJECTED, PromotionStatus.SUPERSEDED, PromotionStatus.AWAITING_APPROVAL, PromotionStatus.CI_PENDING, PromotionStatus.CI_FAILED},
    PromotionStatus.MERGED: set(),
    PromotionStatus.REJECTED: set(),
    PromotionStatus.SUPERSEDED: set(),
}

_STATUS_EVENT = {
    PromotionStatus.REQUESTED: EventType.PROMOTION_REQUESTED,
    PromotionStatus.VALIDATING: EventType.PROMOTION_VALIDATING,
    PromotionStatus.BRANCH_READY: EventType.PROMOTION_BRANCH_READY,
    PromotionStatus.PR_OPEN: EventType.PROMOTION_PR_OPEN,
    PromotionStatus.CI_PENDING: EventType.PROMOTION_CI_PENDING,
    PromotionStatus.CI_PASSED: EventType.PROMOTION_CI_PASSED,
    PromotionStatus.CI_FAILED: EventType.PROMOTION_CI_FAILED,
    PromotionStatus.AWAITING_APPROVAL: EventType.PROMOTION_AWAITING_APPROVAL,
    PromotionStatus.MERGE_ELIGIBLE: EventType.PROMOTION_MERGE_ELIGIBLE,
    PromotionStatus.MERGED: EventType.PROMOTION_MERGED,
    PromotionStatus.REJECTED: EventType.PROMOTION_REJECTED,
    PromotionStatus.SUPERSEDED: EventType.PROMOTION_SUPERSEDED,
    PromotionStatus.BLOCKED_EXTERNAL: EventType.PROMOTION_BLOCKED_EXTERNAL,
}


# ── provider contract ─────────────────────────────────────────────────


class ProviderUnavailable(Exception):
    """Provider not configured / no credential: nothing can be published."""


class ProviderTransient(Exception):
    """Timeout / 429 / 5xx: retry later with backoff (bounded attempts)."""


class ProviderConflict(Exception):
    """Branch or PR exists with different content, base moved, head changed."""


class ProviderRefused(Exception):
    """Permanent refusal (403, invalid target, protected branch)."""


class IllegalTransition(Exception):
    pass


@dataclass
class BranchRef:
    name: str
    sha: str
    created: bool = False


@dataclass
class PRRef:
    number: int
    url: str
    created: bool = False


@dataclass
class PRState:
    ci: str = "unknown"                   # pending | passed | failed | unknown
    head_sha: str = ""
    base_moved: bool = False
    merged: bool = False
    merged_sha: str = ""
    mergeable: Optional[bool] = None
    approvals: List[str] = field(default_factory=list)
    unresolved_reviews: int = 0
    closed: bool = False
    draft: bool = False
    node_id: str = ""
    detail: str = ""


class PromotionProvider(Protocol):
    name: str

    def publish_branch(self, promotion: CodePromotion, candidate: CodeCandidate, ws: ws_mod.Workspace) -> BranchRef: ...  # pragma: no cover - protocol
    def open_or_update_pr(self, promotion: CodePromotion, candidate: CodeCandidate, title: str, body: str) -> PRRef: ...  # pragma: no cover
    def get_pr_state(self, promotion: CodePromotion) -> PRState: ...  # pragma: no cover
    def update_branch(self, promotion: CodePromotion, expected_head_sha: str) -> str: ...  # pragma: no cover
    def mark_ready_for_review(self, promotion: CodePromotion, state: PRState) -> bool: ...  # pragma: no cover
    def merge(self, promotion: CodePromotion, expected_head_sha: str) -> str: ...  # pragma: no cover


class DisabledPromotionProvider:
    name = "disabled"

    def publish_branch(self, promotion, candidate, ws):
        raise ProviderUnavailable("no promotion provider configured (SOCIETY_PROMOTION_PROVIDER=disabled)")

    def open_or_update_pr(self, promotion, candidate, title, body):
        raise ProviderUnavailable("no promotion provider configured")

    def get_pr_state(self, promotion):
        raise ProviderUnavailable("no promotion provider configured")

    def update_branch(self, promotion, expected_head_sha):
        raise ProviderUnavailable("no promotion provider configured")

    def mark_ready_for_review(self, promotion, state):
        raise ProviderUnavailable("no promotion provider configured")

    def merge(self, promotion, expected_head_sha):
        raise ProviderUnavailable("no promotion provider configured")


class FakePromotionProvider:
    """In-memory GitHub double for shadow promotion. Deterministic and
    inspectable: branch/PR creation counts, injectable faults (transient,
    conflict, refused), scripted CI outcomes, simulated human approval/merge,
    base movement. It never touches the network."""

    name = "fake"

    def __init__(self, *, base_branch: str = "main", ci_outcome: str = "passed"):
        self.base_branch = base_branch
        self.default_ci = ci_outcome
        self.branches: Dict[str, str] = {}
        self.prs: Dict[str, Dict[str, Any]] = {}      # head branch -> pr dict
        self.ci_by_branch: Dict[str, str] = {}
        self.calls: List[tuple] = []
        self.faults: List[Exception] = []             # raised in order on the next provider call(s)
        self.base_sha = "base0000"
        self._next_pr = 100
        self.publish_count = 0
        self.pr_create_count = 0
        self.merge_count = 0
        self.update_branch_count = 0
        self.ready_count = 0

    # test helpers
    def inject(self, exc: Exception) -> None:
        self.faults.append(exc)

    def set_ci(self, branch: str, outcome: str) -> None:
        self.ci_by_branch[branch] = outcome

    def approve(self, pr_number: int, user: str) -> None:
        for pr in self.prs.values():
            if pr["number"] == pr_number:
                pr["approvals"].append(user)

    def human_merge(self, pr_number: int, merged_sha: str = "") -> None:
        for pr in self.prs.values():
            if pr["number"] == pr_number:
                pr["merged"] = True
                pr["merged_sha"] = merged_sha or f"merge-{pr_number}"
                pr["closed"] = True

    def close(self, pr_number: int) -> None:
        for pr in self.prs.values():
            if pr["number"] == pr_number:
                pr["closed"] = True

    def move_base(self, sha: str = "base0001") -> None:
        self.base_sha = sha

    def _maybe_fault(self) -> None:
        if self.faults:
            raise self.faults.pop(0)

    # provider contract
    def publish_branch(self, promotion, candidate, ws):
        self.calls.append(("publish_branch", candidate.branch_name, candidate.head_sha))
        self._maybe_fault()
        if candidate.branch_name in (self.base_branch, "main", "master"):
            raise ProviderRefused("refusing to publish to the protected base branch")
        existing = self.branches.get(candidate.branch_name)
        if existing is not None and existing != candidate.head_sha:
            raise ProviderConflict(f"branch {candidate.branch_name} exists with different history ({existing[:8]} != {candidate.head_sha[:8]}); no force push")
        created = existing is None
        if created:
            self.publish_count += 1
        self.branches[candidate.branch_name] = candidate.head_sha
        return BranchRef(name=candidate.branch_name, sha=candidate.head_sha, created=created)

    def open_or_update_pr(self, promotion, candidate, title, body):
        self.calls.append(("open_or_update_pr", candidate.branch_name))
        self._maybe_fault()
        pr = self.prs.get(candidate.branch_name)
        if pr is None:
            self._next_pr += 1
            self.pr_create_count += 1
            pr = {"number": self._next_pr, "url": f"fake://pr/{self._next_pr}", "title": title, "body": body, "head_sha": candidate.head_sha, "base_sha": self.base_sha, "approvals": [], "merged": False, "merged_sha": "", "closed": False, "unresolved_reviews": 0, "draft": True}
            self.prs[candidate.branch_name] = pr
            return PRRef(number=pr["number"], url=pr["url"], created=True)
        pr["title"], pr["body"], pr["head_sha"] = title, body, candidate.head_sha
        return PRRef(number=pr["number"], url=pr["url"], created=False)

    def get_pr_state(self, promotion):
        self.calls.append(("get_pr_state", promotion.external_branch))
        self._maybe_fault()
        pr = self.prs.get(promotion.external_branch or "")
        if pr is None:
            return PRState(ci="unknown", detail="no such PR")
        ci = self.ci_by_branch.get(promotion.external_branch, self.default_ci)
        return PRState(ci=ci, head_sha=pr["head_sha"], base_moved=pr["base_sha"] != self.base_sha, merged=pr["merged"], merged_sha=pr["merged_sha"], mergeable=not pr["closed"], approvals=list(pr["approvals"]), unresolved_reviews=pr["unresolved_reviews"], closed=pr["closed"], draft=bool(pr.get("draft", True)), node_id=f"fake-node-{pr['number']}")

    def update_branch(self, promotion, expected_head_sha):
        """Merge the base INTO the head, exactly as GitHub's update-branch does:
        a new merge commit on the PR branch. Never a rewrite of history."""
        self.calls.append(("update_branch", promotion.external_branch, expected_head_sha))
        self._maybe_fault()
        pr = self.prs.get(promotion.external_branch or "")
        if pr is None:
            raise ProviderRefused("no PR")
        if pr["head_sha"] != expected_head_sha:
            raise ProviderConflict("head moved since the reconcile was decided")
        self.update_branch_count += 1
        pr["head_sha"] = f"reconciled-{pr['number']}-{self.update_branch_count}"
        pr["base_sha"] = self.base_sha
        self.branches[promotion.external_branch or ""] = pr["head_sha"]
        return pr["head_sha"]

    def mark_ready_for_review(self, promotion, state):
        self.calls.append(("mark_ready_for_review", promotion.external_branch))
        self._maybe_fault()
        pr = self.prs.get(promotion.external_branch or "")
        if pr is None:
            raise ProviderRefused("no PR")
        if not pr.get("draft"):
            return False
        pr["draft"] = False
        self.ready_count += 1
        return True

    def merge(self, promotion, expected_head_sha):
        self.calls.append(("merge", promotion.external_branch, expected_head_sha))
        self._maybe_fault()
        pr = self.prs.get(promotion.external_branch or "")
        if pr is None:
            raise ProviderRefused("no PR")
        if pr["head_sha"] != expected_head_sha:
            raise ProviderConflict("head moved since final validation")
        self.merge_count += 1
        pr["merged"] = True
        pr["merged_sha"] = f"merge-{pr['number']}"
        pr["closed"] = True
        return pr["merged_sha"]


def get_promotion_provider(settings: SocietySettings, *, override: Optional[PromotionProvider] = None) -> PromotionProvider:
    if override is not None:
        return override
    if settings.promotion_provider == "fake":
        return FakePromotionProvider(base_branch=settings.github_base_branch)
    if settings.promotion_provider == "github":
        from .promotion_github import GitHubPromotionProvider

        return GitHubPromotionProvider(settings)
    return DisabledPromotionProvider()


# ── request (called by the executor on behalf of an intent) ───────────


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def promotions_today(db: Session, now: Optional[datetime] = None) -> int:
    now = now or utcnow()
    return int(db.query(CodePromotion).filter(CodePromotion.created_at >= _day_start(now)).count())


def open_prs(db: Session) -> int:
    return int(db.query(CodePromotion).filter(CodePromotion.status.in_(OPEN_PR_STATUSES)).count())


def active_promotion_for(db: Session, candidate_id: uuid.UUID) -> Optional[CodePromotion]:
    return db.query(CodePromotion).filter(CodePromotion.candidate_id == candidate_id, CodePromotion.status.in_(ACTIVE_STATUSES)).first()


def request_promotion(db: Session, *, settings: SocietySettings, candidate: CodeCandidate, agent: Agent, run: AgentRun, causation, source_run_id: uuid.UUID) -> tuple[CodePromotion, bool]:
    """Create the durable REQUESTED record (idempotent per candidate). Flushes,
    does not commit. Returns (promotion, created)."""
    if candidate.status != CodeCandidateStatus.READY and getattr(candidate.status, "value", candidate.status) != "ready":
        raise ValueError(f"only READY candidates can be promoted (candidate is {getattr(candidate.status, 'value', candidate.status)})")
    existing = active_promotion_for(db, candidate.id)
    if existing is not None:
        return existing, False
    if promotions_today(db) >= settings.max_promotions_per_day:
        raise ValueError(f"change budget exhausted: {settings.max_promotions_per_day} promotions today")
    if open_prs(db) >= settings.max_open_autonomous_prs:
        raise ValueError(f"change budget exhausted: {settings.max_open_autonomous_prs} autonomous PRs already open")
    promotion = CodePromotion(
        id=uuid.uuid4(),
        candidate_id=candidate.id,
        correlation_id=candidate.correlation_id,
        risk_tier=candidate.risk_tier or RiskTier.AMBER.value,   # provisional; re-classified in VALIDATING
        base_sha=candidate.base_sha,
        candidate_sha=candidate.head_sha,
        provider=settings.promotion_provider,
        status=PromotionStatus.REQUESTED,
        requested_by_agent_id=agent.id,
        requested_by_run_id=run.id,
        eligibility={},
        evidence={"requested_by": agent.name, "run_id": str(run.id)},
    )
    db.add(promotion)
    db.flush()
    emit_event(db, event_type=EventType.PROMOTION_REQUESTED, payload=_event_payload(promotion, candidate), actor_type="agent", actor_id=agent.id, subject_type="code_promotion", subject_id=promotion.id, causation=causation, idempotency_key=f"promotion:{promotion.id}:requested", source_run_id=source_run_id, notify=True)
    return promotion, True


def _event_payload(promotion: CodePromotion, candidate: CodeCandidate, **extra) -> Dict[str, Any]:
    payload = {
        "promotion_id": str(promotion.id),
        "candidate_id": str(candidate.id),
        "title": candidate.title,
        "status": getattr(promotion.status, "value", promotion.status),
        "risk_tier": promotion.risk_tier,
        "provider": promotion.provider,
        "branch": promotion.external_branch,
        "pr_number": promotion.external_pr_number,
        "pr_url": promotion.external_pr_url,
        "ci_state": promotion.ci_state,
        "failure_reason": (promotion.failure_reason or "")[:300] or None,
    }
    payload.update(extra)
    return payload


# ── controller: claim + advance ───────────────────────────────────────

_CLAIM_SQL = text(
    """
    WITH candidate AS (
        SELECT id FROM code_promotions
        WHERE status IN ('requested','validating','branch_ready','pr_open','ci_pending','ci_passed','awaiting_approval','merge_eligible','blocked_external')
          AND (lease_expires_at IS NULL OR lease_expires_at < :now)
          AND (status NOT IN ('pr_open','ci_pending','awaiting_approval','merge_eligible','blocked_external')
               OR updated_at <= :not_after)
        ORDER BY updated_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE code_promotions p
       SET worker_id = :worker_id, lease_expires_at = :lease_until, attempt = p.attempt + 1
      FROM candidate
     WHERE p.id = candidate.id
 RETURNING p.id
    """
)


def claim_next_promotion(db: Session, *, worker_id: str, lease_seconds: int, now: Optional[datetime] = None, min_age_seconds: int = 0) -> Optional[CodePromotion]:
    """Atomically claim one advanceable promotion (FOR UPDATE SKIP LOCKED). Commits.
    ``min_age_seconds`` rate-limits the states that wait on the outside world
    (open PR / CI / human / blocked provider): they are re-polled only once
    their last update is at least that old; internal steps advance at once."""
    now = now or utcnow()
    row = db.execute(_CLAIM_SQL, {"now": now, "worker_id": worker_id, "lease_until": now + timedelta(seconds=lease_seconds), "not_after": now - timedelta(seconds=min_age_seconds)}).fetchone()
    db.commit()
    if row is None:
        return None
    promo = db.query(CodePromotion).filter(CodePromotion.id == row[0]).first()
    if promo is not None:
        db.refresh(promo)
    return promo


def _transition(db: Session, promotion: CodePromotion, candidate: CodeCandidate, new_status: PromotionStatus, *, reason: str = "", **extra) -> None:
    current = promotion.status if isinstance(promotion.status, PromotionStatus) else PromotionStatus(str(promotion.status))
    if new_status == current:
        return
    if new_status not in TRANSITIONS.get(current, set()):
        raise IllegalTransition(f"promotion {promotion.id}: {current.value} -> {new_status.value} is not a legal transition")
    promotion.status = new_status
    if reason:
        promotion.failure_reason = reason[:2000]
    promotion.updated_at = utcnow()
    db.flush()
    cause = db.query(SocietyEvent).filter(SocietyEvent.subject_type == "code_promotion", SocietyEvent.subject_id == promotion.id).order_by(SocietyEvent.created_at.desc()).first()
    emit_event(db, event_type=_STATUS_EVENT[new_status], payload=_event_payload(promotion, candidate, reason=reason[:300] or None, **extra), actor_type="system", subject_type="code_promotion", subject_id=promotion.id, correlation_id=promotion.correlation_id, causation=cause, idempotency_key=f"promotion:{promotion.id}:{new_status.value}", notify=True)


def _release(db: Session, promotion: CodePromotion) -> None:
    promotion.worker_id = None
    promotion.lease_expires_at = None
    promotion.updated_at = utcnow()
    db.commit()


def _trusted_base_head(settings: SocietySettings) -> str:
    return ws_mod.main_branch_head(settings)


def _base_is_ancestor(settings: SocietySettings, base_sha: str) -> bool:
    try:
        ws_mod._git(["merge-base", "--is-ancestor", base_sha, "HEAD"], cwd=pathlib.Path(settings.repo_root))
        return True
    except ws_mod.WorkspaceError:
        return False


def validate(db: Session, settings: SocietySettings, promotion: CodePromotion, candidate: CodeCandidate) -> Dict[str, Any]:
    """Trusted-base validation. Returns the eligibility snapshot; sets the
    trusted risk tier on both rows. Raises ValueError (reject) / LookupError
    (superseded) with the reason."""
    checks: Dict[str, Any] = {}
    status = getattr(candidate.status, "value", candidate.status)
    checks["candidate_ready"] = status == CodeCandidateStatus.READY.value
    if not checks["candidate_ready"]:
        raise ValueError(f"candidate is {status}, not READY")
    if not candidate.branch_name or candidate.branch_name.split("/")[-1] in ("main", "master") or candidate.branch_name in (settings.github_base_branch, "main", "master"):
        raise ValueError("refusing to promote a branch that names the protected base branch")
    if not candidate.branch_name.startswith(settings.branch_prefix.rstrip("/") + "/"):
        raise ValueError(f"branch {candidate.branch_name!r} is outside the autonomous prefix {settings.branch_prefix!r}")
    try:
        ws = ws_mod.ensure_workspace(settings, candidate.id)
        head = ws_mod.head_sha(ws)
        changed = ws_mod.changed_files(ws)
        diff = ws_mod.diff_text(ws, max_chars=400_000)
        static = static_security_scan(ws, changed)
    except ws_mod.WorkspaceError as exc:
        raise ValueError(f"candidate workspace unavailable: {exc}") from exc
    checks["head_sha_matches"] = head == candidate.head_sha
    if not checks["head_sha_matches"]:
        raise LookupError(f"candidate sha changed since QA ({candidate.head_sha[:8]} -> {head[:8]}); re-QA required")
    checks["base_sha_current"] = candidate.base_sha == _trusted_base_head(settings)
    checks["base_is_ancestor"] = _base_is_ancestor(settings, candidate.base_sha or "")
    if not checks["base_is_ancestor"]:
        raise LookupError("candidate base is no longer an ancestor of the trusted base; rebuild on the current base")
    # TRUSTED risk classification from the real diff (never the candidate's copy of risk.py)
    risk = assess_risk(changed, diff, spec_kind=str((candidate.spec or {}).get("kind") or ""))
    candidate.risk_tier = risk.tier.value
    promotion.risk_tier = risk.tier.value
    checks["risk_tier"] = risk.tier.value
    checks["risk_reasons"] = risk.reasons[:20]
    if risk.tier == RiskTier.NEVER:
        raise ValueError("NEVER tier: " + "; ".join(risk.never_findings[:5]))
    never_paths = [p for p in changed if ws_mod.is_protected(p)]
    checks["no_prohibited_files"] = not never_paths
    if never_paths:
        raise ValueError(f"prohibited files changed: {never_paths}")
    checks["files_within_budget"] = len(changed) <= settings.max_files_per_candidate and int(candidate.diff_lines or 0) <= settings.max_diff_lines
    if not checks["files_within_budget"]:
        raise ValueError("change exceeds the files/diff-lines budget")
    qa = candidate.qa_report or {}
    sec = candidate.security_report or {}
    checks["qa_pass"] = qa.get("verdict") == "pass" and qa.get("head_sha") == candidate.head_sha
    if not checks["qa_pass"]:
        raise ValueError("QA PASS for this exact head is required")
    needs_security = bool(candidate.requires_security_review) or risk.tier in (RiskTier.AMBER, RiskTier.RED) or bool(static)
    checks["security_required"] = needs_security
    checks["security_pass"] = (sec.get("verdict") == "pass" and sec.get("head_sha") == candidate.head_sha) if needs_security else True
    if needs_security and not checks["security_pass"]:
        raise ValueError("independent Security PASS for this exact head is required for this tier")
    checks["no_critical_findings"] = not any("secret" in f for f in static)
    if not checks["no_critical_findings"]:
        raise ValueError("static scan reports a possible secret in the diff")
    if risk.tier == RiskTier.RED and (candidate.security_agent_id is None or candidate.security_agent_id in (candidate.builder_agent_id, candidate.qa_agent_id, candidate.requested_by_agent_id)):
        raise ValueError("RED tier requires an INDEPENDENT Security reviewer")
    checks["independence"] = len({candidate.builder_agent_id, candidate.qa_agent_id, candidate.requested_by_agent_id} - {None}) >= 2
    checks["changed_files"] = changed[:50]
    checks["validated_at"] = utcnow().isoformat()
    return checks


def latest_experiment(db: Session, promotion: CodePromotion) -> Optional[ChangeExperiment]:
    return db.query(ChangeExperiment).filter(ChangeExperiment.promotion_id == promotion.id).order_by(ChangeExperiment.created_at.desc()).first()


def _fitness_satisfied(exp: Optional[ChangeExperiment]) -> bool:
    """Does the fitness engine RAISE NO OBJECTION to merging this candidate?

    ``pass`` requires a measured IMPROVEMENT (``fitness.py``: no improvement and
    no regression is ``inconclusive``). A documentation candidate cannot move a
    latency or failure-rate metric, so under an ``exp_status == PASS`` test it is
    not merely unlikely to qualify -- it can NEVER qualify, and neither can any
    other change whose value is not a metric. That is a defect in the law, not a
    property of the change: the live promotion of candidate b8cee13c passed every
    hard gate, regressed nothing, and was still blocked forever.

    So an inconclusive experiment satisfies this gate ONLY when it actually
    looked and found nothing wrong:

    * every hard gate passed (no test regression, no test removal, no security
      regression, no NEVER finding, no metric collection disabled, the candidate
      test run completed) -- an empty gate list is NOT "all passed";
    * no metric regressed;
    * confidence is high, which ``fitness.py`` sets only when both the baseline
      and candidate test runs completed without timing out.

    The attempt-budget-exhausted path is also ``inconclusive``, but it records a
    FAILED ``attempts`` gate with low confidence, so it is excluded on two
    independent counts. ``fail`` and a missing experiment stay blocking.
    """
    if exp is None:
        return False
    status = getattr(exp.status, "value", exp.status)
    if status == ExperimentStatus.PASS.value:
        return True
    if status != ExperimentStatus.INCONCLUSIVE.value:
        return False
    gates = list(exp.hard_gate_results or [])
    if not gates or not all(g.get("passed") for g in gates):
        return False
    if any((v or {}).get("verdict") == "regression" for v in (exp.metric_deltas or {}).values()):
        return False
    return exp.confidence == "high"


def compute_eligibility(db: Session, settings: SocietySettings, promotion: CodePromotion, candidate: CodeCandidate, state: Optional[PRState]) -> Dict[str, Any]:
    """Merge gates from persisted facts. Human approval is required for AMBER
    and RED always, and for GREEN whenever auto-merge is off."""
    tier = RiskTier(promotion.risk_tier)
    exp = latest_experiment(db, promotion)
    exp_status = getattr(exp.status, "value", exp.status) if exp else None
    gates = {
        "trusted_risk_tier": tier.value,
        "risk_promotable": tier != RiskTier.NEVER,
        "ci_passed": (state.ci == "passed") if state else (promotion.ci_state == "passed"),
        "branch_up_to_date": (not state.base_moved) if state else True,
        "head_unchanged": (state.head_sha == promotion.candidate_sha) if (state and state.head_sha) else True,
        "qa_pass": bool((candidate.qa_report or {}).get("verdict") == "pass"),
        "security_pass": bool((candidate.security_report or {}).get("verdict") == "pass") or not (candidate.requires_security_review or tier != RiskTier.GREEN),
        "no_critical_findings": bool((promotion.eligibility or {}).get("no_critical_findings", True)),
        "fitness_precheck": _fitness_satisfied(exp),
        "fitness_status": exp_status,
        "fitness_decision": (exp.decision if exp else None),
        "fitness_confidence": (exp.confidence if exp else None),
        "no_unresolved_review": (state.unresolved_reviews == 0) if state else True,
        "change_budget": open_prs(db) <= settings.max_open_autonomous_prs,
        # A draft PR cannot be merged by anyone, so it can never be merge-eligible.
        "pr_not_draft": (not state.draft) if state else True,
        "human_approval_required": tier != RiskTier.GREEN or not settings.auto_merge_enabled,
        "human_approvals": list(state.approvals) if state else [],
        "auto_merge_enabled": bool(settings.auto_merge_enabled),
    }
    gates["human_approval_satisfied"] = bool(gates["human_approvals"]) if gates["human_approval_required"] else True
    hard = ["risk_promotable", "ci_passed", "branch_up_to_date", "head_unchanged", "qa_pass", "security_pass", "no_critical_findings", "fitness_precheck", "no_unresolved_review", "change_budget", "human_approval_satisfied"]
    gates["blocking"] = [g for g in hard if not gates.get(g)]
    gates["merge_eligible"] = not gates["blocking"]
    # "PR not draft" gates AUTO-merge, not eligibility: a draft PR cannot be
    # merged by anyone, but a human may take a non-GREEN PR out of draft and
    # merge it themselves -- which is exactly the governance path RED keeps.
    gates["auto_merge_allowed"] = gates["merge_eligible"] and tier == RiskTier.GREEN and settings.auto_merge_enabled and gates["pr_not_draft"]
    gates["computed_at"] = utcnow().isoformat()
    return gates


def pr_body_from_facts(db: Session, promotion: CodePromotion, candidate: CodeCandidate) -> str:
    """PR description generated ONLY from durable records (never model prose)."""
    from ..models import ImprovementProposal

    prop = db.query(ImprovementProposal).filter(ImprovementProposal.id == candidate.proposal_id).first() if candidate.proposal_id else None
    qa = candidate.qa_report or {}
    sec = candidate.security_report or {}
    spec = candidate.spec or {}
    exp = latest_experiment(db, promotion)
    lines = [
        f"## Autonomous candidate: {candidate.title}",
        "",
        f"- candidate: `{candidate.id}` · correlation: `{candidate.correlation_id}` · promotion: `{promotion.id}`",
        f"- proposal: {('`' + str(prop.id) + '` — ' + prop.title) if prop else 'none'}",
        f"- trusted risk tier: **{promotion.risk_tier}** (classified from the base revision)",
        f"- base `{(candidate.base_sha or '')[:12]}` → head `{(candidate.head_sha or '')[:12]}` · {len(candidate.changed_files or [])} file(s), {candidate.diff_lines or 0} diff line(s)",
        "",
        "### Files",
        *[f"- `{f}`" for f in (candidate.changed_files or [])[:50]],
        "",
        "### Acceptance criteria",
        *[f"- `{t}`" for t in (spec.get("acceptance_tests") or [])[:20]],
        f"- expected effect: {spec.get('expected_effect') or 'n/a'}",
        f"- signal: {spec.get('signal') or (prop.title if prop else 'n/a')}",
        "",
        "### QA",
        f"- verdict: **{qa.get('verdict', 'n/a')}** (attempts {qa.get('attempts', 'n/a')}, by {qa.get('evaluated_by', 'n/a')})",
        f"- summary: {qa.get('summary', 'n/a')}",
        "",
        "### Security",
        f"- verdict: **{sec.get('verdict', 'not required')}** (by {sec.get('reviewed_by', 'n/a')})",
        f"- static findings: {len(sec.get('static_findings') or [])} · reviewer findings: {len(sec.get('findings') or [])}",
        "",
        "### Fitness",
        (f"- experiment `{exp.id}`: {getattr(exp.status, 'value', exp.status)} · decision {exp.decision} · confidence {exp.confidence} · rollback recommended: {exp.rollback_recommended}" if exp else "- no experiment yet (requested after CI)"),
        "",
        "### Rollback criteria",
        "- any hard fitness gate failing after merge (test/security regression, disabled metrics) → rollback recommendation to the previous known-good SHA",
        "",
        "_Generated by the AgentNet promotion controller from persisted runtime records. The model cannot edit this body._",
    ]
    return "\n".join(lines)


#: A base that moves faster than CI finishes would reconcile forever. Bounded.
MAX_BASE_RECONCILES = 3


def _reconcile_base(db: Session, settings: SocietySettings, provider: PromotionProvider, promotion: CodePromotion, candidate: CodeCandidate, state: PRState) -> bool:
    """Bring a stale autonomous branch up to date with its base, the ONLY way
    that is allowed: ask the provider to merge the base INTO the head.

    Before this, the controller detected ``base_moved``, wrote ``merge_state =
    "stale"`` and returned -- so a promotion whose base moved while CI ran was
    parked forever with no action that could ever clear it. That is what
    happened to the first real promotion: main advanced twice while PR #30 sat
    at ``awaiting_approval``.

    What this deliberately does NOT do:

    * it never rebases or force-pushes -- history on a published branch stays
      valid for anyone who fetched it, and the ruleset is never bypassed;
    * it never merges the PR, and never touches the base branch;
    * it never runs when the head is not the sha the controller validated, so a
      push by anyone else still lands in SUPERSEDED rather than being quietly
      absorbed.

    The new head is a merge commit the provider made, so it is recorded as the
    validated sha and the promotion returns to CI_PENDING: the required checks
    MUST run again on the reconciled head before any gate can pass. The
    candidate's own ``head_sha`` is untouched -- its diff did not change.
    """
    evidence = dict(promotion.evidence or {})
    done = int(evidence.get("base_reconciles") or 0)
    if done >= MAX_BASE_RECONCILES:
        promotion.evidence = {**evidence, "base_reconcile_note": f"base moved again after {done} reconciles; not chasing it further"}
        return False
    if state.head_sha and promotion.candidate_sha and state.head_sha != promotion.candidate_sha:
        return False  # handled earlier as SUPERSEDED; never reconcile someone else's push
    try:
        new_head = provider.update_branch(promotion, promotion.candidate_sha or state.head_sha or "")
    except (ProviderUnavailable, ProviderRefused, ProviderConflict) as exc:
        promotion.evidence = {**evidence, "base_reconcile_error": str(exc)[:300]}
        return False
    if not new_head:
        return False
    promotion.candidate_sha = new_head
    promotion.ci_state = "pending"
    promotion.merge_state = "reconciling"
    promotion.evidence = {
        **evidence,
        "base_reconciles": done + 1,
        "base_reconciled_from": (state.head_sha or "")[:40],
        "base_reconciled_to": new_head[:40],
        "base_reconciled_at": utcnow().isoformat(),
    }
    promotion.eligibility = {**(promotion.eligibility or {}), "branch_up_to_date": True, "ci_passed": False}
    _transition(db, promotion, candidate, PromotionStatus.CI_PENDING, reason="base moved: branch reconciled, required checks must re-run", head_sha=new_head[:12])
    return True


def _ready_blockers(candidate: CodeCandidate, promotion: CodePromotion, state: PRState) -> List[str]:
    """Why this draft PR may NOT be offered for review yet.

    A draft PR cannot merge, so leaving it draft is the safe default and the
    transition out of it is an authority decision -- taken by the trusted
    controller from persisted facts, never by the model, and never on a change
    whose trusted tier is not GREEN (an AMBER/RED/CONSTITUTIONAL candidate may
    be published for humans to read, but it is not put in the queue that
    autonomous merge draws from).
    """
    blockers: List[str] = []
    if getattr(candidate.status, "value", candidate.status) != CodeCandidateStatus.READY.value:
        blockers.append("candidate_not_ready")
    if (candidate.qa_report or {}).get("verdict") != "pass":
        blockers.append("qa_not_passed")
    sec = candidate.security_report or {}
    if candidate.requires_security_review and sec.get("verdict") != "pass":
        blockers.append("security_not_passed")
    if sec.get("findings") or sec.get("static_findings"):
        blockers.append("security_findings_present")
    if str(promotion.risk_tier) != RiskTier.GREEN.value:
        blockers.append(f"risk_tier_{promotion.risk_tier}")
    if state.head_sha and promotion.candidate_sha and state.head_sha != promotion.candidate_sha:
        blockers.append("head_moved")
    return blockers


def _offer_for_review(db: Session, provider: PromotionProvider, promotion: CodePromotion, candidate: CodeCandidate, state: PRState) -> None:
    """Take a GREEN, fully gated draft PR out of draft. Records why, either way."""
    if not state.draft:
        return
    blockers = _ready_blockers(candidate, promotion, state)
    if blockers:
        promotion.evidence = {**(promotion.evidence or {}), "ready_for_review_blocked_by": blockers}
        return
    try:
        changed = provider.mark_ready_for_review(promotion, state)
    except (ProviderUnavailable, ProviderRefused, ProviderConflict) as exc:
        promotion.evidence = {**(promotion.evidence or {}), "ready_for_review_error": str(exc)[:300]}
        return
    if changed:
        # The PR is no longer a draft, so the state this cycle reasons from must
        # say so: otherwise pr_not_draft blocks eligibility for one more poll on
        # a fact that is already false.
        state.draft = False
        promotion.evidence = {**(promotion.evidence or {}), "ready_for_review_at": utcnow().isoformat(), "ready_for_review_by": "promotion-controller"}
        db.flush()
        cause = db.query(SocietyEvent).filter(SocietyEvent.subject_type == "code_promotion", SocietyEvent.subject_id == promotion.id).order_by(SocietyEvent.created_at.desc()).first()
        emit_event(
            db,
            event_type=EventType.PROMOTION_READY_FOR_REVIEW,
            payload=_event_payload(promotion, candidate, reason="internal gates satisfied; offered for review"),
            actor_type="system",
            subject_type="code_promotion",
            subject_id=promotion.id,
            correlation_id=promotion.correlation_id,
            causation=cause,
            idempotency_key=f"promotion:{promotion.id}:ready_for_review",
            notify=True,
        )


def advance(db: Session, *, settings: SocietySettings, provider: PromotionProvider, promotion: CodePromotion, worker_id: str) -> str:
    """Run ONE deterministic step for a claimed promotion. Commits. Returns the resulting status value."""
    candidate = db.query(CodeCandidate).filter(CodeCandidate.id == promotion.candidate_id).first()
    if candidate is None:
        _transition(db, promotion, CodeCandidate(id=promotion.candidate_id, title="?", correlation_id=promotion.correlation_id), PromotionStatus.REJECTED, reason="candidate row missing")
        _release(db, promotion)
        return PromotionStatus.REJECTED.value
    status = promotion.status if isinstance(promotion.status, PromotionStatus) else PromotionStatus(str(promotion.status))
    try:
        if status in (PromotionStatus.REQUESTED, PromotionStatus.BLOCKED_EXTERNAL):
            if status == PromotionStatus.BLOCKED_EXTERNAL and getattr(provider, "name", "disabled") == "disabled":
                _release(db, promotion)
                return status.value
            _transition(db, promotion, candidate, PromotionStatus.VALIDATING)
            db.commit()
            status = PromotionStatus.VALIDATING
        if status == PromotionStatus.VALIDATING:
            try:
                promotion.eligibility = validate(db, settings, promotion, candidate)
            except LookupError as exc:
                _transition(db, promotion, candidate, PromotionStatus.SUPERSEDED, reason=str(exc))
                _release(db, promotion)
                return PromotionStatus.SUPERSEDED.value
            except ValueError as exc:
                _transition(db, promotion, candidate, PromotionStatus.REJECTED, reason=str(exc))
                _release(db, promotion)
                return PromotionStatus.REJECTED.value
            promotion.previous_good_sha = promotion.previous_good_sha or candidate.base_sha
            db.commit()
            ws = ws_mod.ensure_workspace(settings, candidate.id)
            try:
                ref = provider.publish_branch(promotion, candidate, ws)
            except ProviderUnavailable as exc:
                _transition(db, promotion, candidate, PromotionStatus.BLOCKED_EXTERNAL, reason=str(exc))
                _release(db, promotion)
                return PromotionStatus.BLOCKED_EXTERNAL.value
            promotion.external_branch = ref.name
            promotion.evidence = {**(promotion.evidence or {}), "branch_created": ref.created, "branch_sha": ref.sha}
            _transition(db, promotion, candidate, PromotionStatus.BRANCH_READY)
            _release(db, promotion)
            return PromotionStatus.BRANCH_READY.value
        if status == PromotionStatus.BRANCH_READY:
            pr = provider.open_or_update_pr(promotion, candidate, f"[agentnet-auto] {candidate.title}"[:200], pr_body_from_facts(db, promotion, candidate))
            promotion.external_pr_number = pr.number
            promotion.external_pr_url = pr.url
            promotion.evidence = {**(promotion.evidence or {}), "pr_created": pr.created}
            _transition(db, promotion, candidate, PromotionStatus.PR_OPEN, pr_number=pr.number, pr_url=pr.url)
            _release(db, promotion)
            return PromotionStatus.PR_OPEN.value
        if status in (PromotionStatus.PR_OPEN, PromotionStatus.CI_PENDING, PromotionStatus.CI_PASSED, PromotionStatus.AWAITING_APPROVAL, PromotionStatus.MERGE_ELIGIBLE):
            state = provider.get_pr_state(promotion)
            promotion.ci_state = state.ci
            if state.merged:
                promotion.merged_sha = state.merged_sha or promotion.merged_sha
                promotion.merge_state = "merged"
                _transition(db, promotion, candidate, PromotionStatus.MERGED, merged_sha=promotion.merged_sha)
                _release(db, promotion)
                return PromotionStatus.MERGED.value
            if state.closed:
                _transition(db, promotion, candidate, PromotionStatus.REJECTED, reason="pull request closed without merge")
                _release(db, promotion)
                return PromotionStatus.REJECTED.value
            if state.head_sha and state.head_sha != promotion.candidate_sha:
                _transition(db, promotion, candidate, PromotionStatus.SUPERSEDED, reason="PR head moved away from the validated candidate sha")
                _release(db, promotion)
                return PromotionStatus.SUPERSEDED.value
            if state.base_moved and status in (PromotionStatus.CI_PASSED, PromotionStatus.AWAITING_APPROVAL, PromotionStatus.MERGE_ELIGIBLE):
                promotion.merge_state = "stale"
                promotion.eligibility = {**(promotion.eligibility or {}), "branch_up_to_date": False}
                if status == PromotionStatus.MERGE_ELIGIBLE:
                    _transition(db, promotion, candidate, PromotionStatus.AWAITING_APPROVAL, reason="base moved: CI must re-run on an up-to-date branch")
                reconciled = _reconcile_base(db, settings, provider, promotion, candidate, state)
                _release(db, promotion)
                if reconciled:
                    return PromotionStatus.CI_PENDING.value
                return promotion.status.value if isinstance(promotion.status, PromotionStatus) else str(promotion.status)
            if state.ci == "failed":
                _transition(db, promotion, candidate, PromotionStatus.CI_FAILED, reason=state.detail or "CI failed")
                _release(db, promotion)
                return PromotionStatus.CI_FAILED.value
            if state.ci in ("pending", "unknown"):
                if status == PromotionStatus.PR_OPEN:
                    _transition(db, promotion, candidate, PromotionStatus.CI_PENDING)
                _release(db, promotion)
                return promotion.status.value if isinstance(promotion.status, PromotionStatus) else str(promotion.status)
            # CI passed
            if status in (PromotionStatus.PR_OPEN, PromotionStatus.CI_PENDING):
                _transition(db, promotion, candidate, PromotionStatus.CI_PASSED)
                _release(db, promotion)
                return PromotionStatus.CI_PASSED.value
            # Offer a fully gated GREEN candidate for review BEFORE eligibility is
            # computed: a draft PR can never merge, so leaving it draft would make
            # merge_eligible unreachable for exactly the changes that qualify.
            _offer_for_review(db, provider, promotion, candidate, state)
            gates = compute_eligibility(db, settings, promotion, candidate, state)
            promotion.eligibility = {**(promotion.eligibility or {}), **gates}
            promotion.merge_state = "eligible" if gates["merge_eligible"] else "blocked"
            if gates.get("human_approvals"):
                promotion.evidence = {**(promotion.evidence or {}), "approvals": gates["human_approvals"]}
                promotion.approval_at = promotion.approval_at or utcnow()
            if gates["merge_eligible"]:
                if status != PromotionStatus.MERGE_ELIGIBLE:
                    _transition(db, promotion, candidate, PromotionStatus.MERGE_ELIGIBLE, gates=gates["blocking"])
                if gates["auto_merge_allowed"]:
                    merged = provider.merge(promotion, promotion.candidate_sha or "")
                    promotion.merged_sha = merged
                    promotion.merge_state = "merged"
                    _transition(db, promotion, candidate, PromotionStatus.MERGED, merged_sha=merged)
                _release(db, promotion)
                return promotion.status.value if isinstance(promotion.status, PromotionStatus) else str(promotion.status)
            if status == PromotionStatus.CI_PASSED:
                _transition(db, promotion, candidate, PromotionStatus.AWAITING_APPROVAL, blocking=gates["blocking"])
            elif status == PromotionStatus.MERGE_ELIGIBLE:
                _transition(db, promotion, candidate, PromotionStatus.AWAITING_APPROVAL, reason="eligibility lost: " + ", ".join(gates["blocking"]), blocking=gates["blocking"])
            _release(db, promotion)
            return promotion.status.value if isinstance(promotion.status, PromotionStatus) else str(promotion.status)
        _release(db, promotion)
        return status.value
    except ProviderTransient as exc:
        db.rollback()
        promotion = db.merge(promotion)
        if int(promotion.attempt or 0) >= settings.promotion_max_attempts:
            candidate = db.merge(candidate)
            _transition(db, promotion, candidate, PromotionStatus.REJECTED, reason=f"provider kept failing after {promotion.attempt} attempts: {exc}")
            _release(db, promotion)
            return PromotionStatus.REJECTED.value
        # keep the lease so the step is retried after backoff (lease expiry)
        promotion.failure_reason = f"transient: {exc}"[:2000]
        promotion.lease_expires_at = utcnow() + timedelta(seconds=min(settings.promotion_lease_seconds, 5 * (2 ** max(0, int(promotion.attempt or 1) - 1))))
        promotion.updated_at = utcnow()
        db.commit()
        return "retry"
    except ProviderConflict as exc:
        db.rollback()
        promotion = db.merge(promotion)
        candidate = db.merge(candidate)
        _transition(db, promotion, candidate, PromotionStatus.SUPERSEDED, reason=f"conflict: {exc}")
        _release(db, promotion)
        return PromotionStatus.SUPERSEDED.value
    except ProviderRefused as exc:
        db.rollback()
        promotion = db.merge(promotion)
        candidate = db.merge(candidate)
        _transition(db, promotion, candidate, PromotionStatus.REJECTED, reason=f"refused: {exc}")
        _release(db, promotion)
        return PromotionStatus.REJECTED.value
    except ProviderUnavailable as exc:
        db.rollback()
        promotion = db.merge(promotion)
        candidate = db.merge(candidate)
        if promotion.status != PromotionStatus.BLOCKED_EXTERNAL:
            try:
                _transition(db, promotion, candidate, PromotionStatus.BLOCKED_EXTERNAL, reason=str(exc))
            except IllegalTransition:
                promotion.failure_reason = f"blocked: {exc}"[:2000]
        _release(db, promotion)
        return PromotionStatus.BLOCKED_EXTERNAL.value


def process_promotions(db_factory, *, settings: SocietySettings, provider: PromotionProvider, worker_id: str, max_items: int = 20) -> Dict[str, int]:
    """Worker loop step: claim and advance promotions until none is claimable."""
    stats: Dict[str, int] = {}
    for _ in range(max_items):
        db = db_factory()
        try:
            promo = claim_next_promotion(db, worker_id=worker_id, lease_seconds=settings.promotion_lease_seconds, min_age_seconds=settings.promotion_poll_interval_seconds)
            if promo is None:
                break
            try:
                result = advance(db, settings=settings, provider=provider, promotion=promo, worker_id=worker_id)
            except IllegalTransition as exc:
                db.rollback()
                logger.error("promotion %s illegal transition: %s", promo.id, exc)
                promo = db.merge(promo)
                promo.failure_reason = str(exc)[:2000]
                _release(db, promo)
                result = "illegal"
            except Exception as exc:  # noqa: BLE001 — never let one promotion kill the loop
                db.rollback()
                logger.exception("promotion %s crashed", promo.id)
                promo = db.merge(promo)
                promo.failure_reason = f"{type(exc).__name__}: {exc}"[:2000]
                _release(db, promo)
                result = "error"
            stats[result] = stats.get(result, 0) + 1
        finally:
            db.close()
    return stats


__all__ = [
    "PromotionProvider",
    "DisabledPromotionProvider",
    "FakePromotionProvider",
    "ProviderUnavailable",
    "ProviderTransient",
    "ProviderConflict",
    "ProviderRefused",
    "IllegalTransition",
    "BranchRef",
    "PRRef",
    "PRState",
    "TRANSITIONS",
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "get_promotion_provider",
    "request_promotion",
    "claim_next_promotion",
    "validate",
    "compute_eligibility",
    "pr_body_from_facts",
    "advance",
    "process_promotions",
    "active_promotion_for",
]
