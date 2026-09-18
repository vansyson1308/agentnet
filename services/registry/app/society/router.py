"""Model router + cost governor: the model never chooses its own tier.

Logical tiers (``fast`` / ``strong``) map to provider model names through
configuration only (``SOCIETY_MODEL_FAST_NAME`` / ``SOCIETY_MODEL_STRONG_NAME``);
no provider model name appears in business logic. Routing is a pure function
of role, event, risk, prior failures and remaining budget, recorded on the
run (``model_tier`` / ``route_reason``). Escalation fails closed: when the
strong tier is not configured or a budget is exhausted, the route is FAST or
the run is refused by the pre-run budget gate — never an unbounded upgrade.

Initial policy (docs/SELF_DEVELOPMENT.md):
  Scout / Governor → FAST
  Architect → FAST; STRONG on RED work or after an invalid response
  Builder → STRONG for real code (kind=code) or after a QA failure; FAST for docs
  QA → FAST (deterministic checks do the work)
  Security → FAST unless a semantic review is needed (static findings present)
  Evaluator → FAST; STRONG on ambiguous (inconclusive) evidence
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import AgentRun, AgentRunStatus
from .config import SocietySettings

FAST = "fast"
STRONG = "strong"


@dataclass(frozen=True)
class Route:
    tier: str
    model_name: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"tier": self.tier, "model_name": self.model_name, "reason": self.reason}


def correlation_spend(db: Session, correlation_id) -> Decimal:
    v = db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0)).filter(AgentRun.correlation_id == correlation_id).scalar() or 0
    return Decimal(str(v))


def prior_invalid_in_correlation(db: Session, correlation_id, agent_id) -> int:
    return int(
        db.query(func.count(AgentRun.id))
        .filter(AgentRun.correlation_id == correlation_id, AgentRun.agent_id == agent_id, AgentRun.status.in_([AgentRunStatus.DEAD, AgentRunStatus.QUEUED]), AgentRun.error.ilike("%structured output%"))
        .scalar()
        or 0
    )


def choose_route(
    settings: SocietySettings,
    *,
    role: str,
    event_type: str,
    event_payload: Optional[Dict[str, Any]] = None,
    risk_tier: Optional[str] = None,
    prior_invalid: int = 0,
    qa_failures: int = 0,
    budget_remaining_usd: Optional[Decimal] = None,
    correlation_remaining_usd: Optional[Decimal] = None,
) -> Route:
    payload = event_payload or {}
    strong_available = bool(settings.model_strong_name.strip())
    fast = settings.model_fast_name
    strong = settings.model_strong_name or fast

    def _fast(reason: str) -> Route:
        return Route(FAST, fast, reason)

    def _strong(reason: str) -> Route:
        if not strong_available:
            return Route(FAST, fast, f"{reason}; strong tier not configured -> fast (fail closed)")
        # Cost governor: escalation needs headroom in BOTH the agent and the correlation budget.
        floor = Decimal("0.05")
        if budget_remaining_usd is not None and budget_remaining_usd < floor:
            return Route(FAST, fast, f"{reason}; agent budget {budget_remaining_usd} below escalation floor -> fast")
        if correlation_remaining_usd is not None and correlation_remaining_usd < floor:
            return Route(FAST, fast, f"{reason}; correlation budget {correlation_remaining_usd} below escalation floor -> fast")
        return Route(STRONG, strong, reason)

    red = (risk_tier or "").lower() in ("red", "never")
    if role in ("scout", "governor", "qa"):
        return _fast(f"{role}: fast by policy")
    if role == "architect":
        if red:
            return _strong("architect: RED-tier work")
        if prior_invalid:
            return _strong(f"architect: {prior_invalid} prior invalid response(s)")
        return _fast("architect: fast by policy")
    if role == "builder":
        kind = str(payload.get("kind") or payload.get("spec_kind") or "")
        if kind == "code" or event_type == "code_change.requested" and payload.get("kind") == "code":
            return _strong("builder: real code change")
        if qa_failures:
            return _strong(f"builder: {qa_failures} QA failure(s)")
        return _fast("builder: documentation/fixture change")
    if role == "security":
        if payload.get("static_findings"):
            return _strong("security: static findings need semantic review")
        return _fast("security: deterministic scan first")
    if role == "evaluator":
        if str(payload.get("decision") or "") == "inconclusive":
            return _strong("evaluator: ambiguous evidence")
        return _fast("evaluator: fast by policy")
    return _fast(f"{role}: default fast")


__all__ = ["FAST", "STRONG", "Route", "choose_route", "correlation_spend", "prior_invalid_in_correlation"]
