"""Policy engine: risk classification, capability grants, budgets. FAIL CLOSED.

The model proposes; this module decides. Nothing here consults the model,
and nothing here can be influenced by message/artifact content — only by
the grant row, the feature flags and the intent's typed payload.

Two gates:

* ``check_run_budget`` — evaluated BEFORE any model call (runs/hour,
  daily spend, cooldown, circuit breaker). Denies cheaply.
* ``evaluate_intent`` — evaluated per intent AFTER the model returns.

Invariant (tested): no code path in the society package writes to
``agent_capability_grants`` except ``seed.py``/admin API. There is no
intent type whose executor mutates a grant, a wallet balance, a spending
cap, or a secret.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Agent, AgentCapabilityGrant, AgentRun, AgentRunStatus, IntentRiskClass, PolicyDecision
from .config import SocietySettings
from .intents import FORBIDDEN_INTENT_TYPES, IntentType, ValidatedIntent

logger = logging.getLogger(__name__)

RISK_BY_TYPE = {
    IntentType.SEND_MESSAGE: IntentRiskClass.LOW,
    IntentType.WRITE_MEMORY: IntentRiskClass.LOW,
    IntentType.CREATE_GOAL: IntentRiskClass.LOW,
    IntentType.UPDATE_GOAL: IntentRiskClass.LOW,
    IntentType.CREATE_IMPROVEMENT: IntentRiskClass.LOW,
    IntentType.REVIEW_IMPROVEMENT: IntentRiskClass.LOW,
    IntentType.SLEEP: IntentRiskClass.LOW,
    IntentType.CREATE_OFFER: IntentRiskClass.MEDIUM,
    IntentType.COUNTER_OFFER: IntentRiskClass.MEDIUM,
    IntentType.ACCEPT_OFFER: IntentRiskClass.MEDIUM,
    IntentType.CREATE_TASK: IntentRiskClass.MEDIUM,
    IntentType.START_TASK: IntentRiskClass.MEDIUM,
    IntentType.COMPLETE_TASK: IntentRiskClass.MEDIUM,
    IntentType.FAIL_TASK: IntentRiskClass.MEDIUM,
    IntentType.REQUEST_CODE_CHANGE: IntentRiskClass.MEDIUM,
    IntentType.SUBMIT_CODE_CANDIDATE: IntentRiskClass.MEDIUM,
    IntentType.REQUEST_QA: IntentRiskClass.MEDIUM,
    IntentType.EVALUATE_CODE_CANDIDATE: IntentRiskClass.MEDIUM,
    IntentType.SECURITY_REVIEW_CANDIDATE: IntentRiskClass.MEDIUM,
    IntentType.REQUEST_STAGING_DEPLOY: IntentRiskClass.MEDIUM,
}
for _t in FORBIDDEN_INTENT_TYPES:
    RISK_BY_TYPE[_t] = IntentRiskClass.HIGH

_RISK_ORDER = {IntentRiskClass.LOW: 0, IntentRiskClass.MEDIUM: 1, IntentRiskClass.HIGH: 2}

_CODE_INTENTS = {
    IntentType.REQUEST_CODE_CHANGE,
    IntentType.SUBMIT_CODE_CANDIDATE,
    IntentType.REQUEST_QA,
    IntentType.EVALUATE_CODE_CANDIDATE,
    IntentType.SECURITY_REVIEW_CANDIDATE,
}


def risk_of(intent_type: Optional[IntentType]) -> IntentRiskClass:
    if intent_type is None:
        return IntentRiskClass.HIGH  # unknown type: treat as worst case for the record
    return RISK_BY_TYPE.get(intent_type, IntentRiskClass.HIGH)


@dataclass(frozen=True)
class PolicyVerdict:
    decision: PolicyDecision
    risk: IntentRiskClass
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision == PolicyDecision.ALLOW


@dataclass(frozen=True)
class RunBudgetVerdict:
    ok: bool
    reason: str = ""


def _as_enum(value, enum_cls):
    if isinstance(value, enum_cls):
        return value
    return enum_cls(str(value))


def evaluate_intent(
    intent: ValidatedIntent,
    *,
    grant: Optional[AgentCapabilityGrant],
    settings: SocietySettings,
    agent: Agent,
    approval_granted: bool = False,
) -> PolicyVerdict:
    """Adjudicate one validated intent. Order matters: cheapest, most
    conservative checks first; every branch returns explicitly.

    ``approval_granted`` is set only by the approval-resume path AFTER a
    durable human decision. It satisfies exactly one condition — the
    grant's ``approval_required_intents`` — and nothing else: forbidden
    types, disabled grants, ceilings, feature flags, scopes and caps are
    re-evaluated from current state and still fail closed."""
    risk = risk_of(intent.intent_type)

    if not intent.valid:
        return PolicyVerdict(PolicyDecision.INVALID, risk, intent.error or "invalid intent")

    itype = intent.intent_type
    assert itype is not None

    if itype in FORBIDDEN_INTENT_TYPES:
        return PolicyVerdict(
            PolicyDecision.DENY, risk, f"{itype.value} is a HIGH-risk surface with no executor in v1 (fail closed)"
        )

    if grant is None:
        return PolicyVerdict(PolicyDecision.DENY, risk, "agent has no capability grant")
    if not grant.enabled:
        return PolicyVerdict(PolicyDecision.DENY, risk, "agent grant is disabled")
    paused_until = grant.paused_until
    if paused_until is not None and paused_until > datetime.now(timezone.utc):
        return PolicyVerdict(PolicyDecision.DENY, risk, f"agent is paused by circuit breaker until {paused_until.isoformat()}")

    allowed = set(grant.allowed_intents or [])
    if itype.value not in allowed:
        return PolicyVerdict(PolicyDecision.DENY, risk, f"{itype.value} is not in the agent's allowed_intents")

    ceiling = _as_enum(grant.risk_ceiling, IntentRiskClass)
    if _RISK_ORDER[risk] > _RISK_ORDER[ceiling]:
        return PolicyVerdict(PolicyDecision.DENY, risk, f"risk {risk.value} exceeds grant ceiling {ceiling.value}")

    # Feature flags (global kill-switches) — evaluated after the grant so a
    # denial reason is precise, but they always win.
    if itype in _CODE_INTENTS and not settings.autonomous_code_enabled:
        return PolicyVerdict(PolicyDecision.DENY, risk, "SOCIETY_AUTONOMOUS_CODE_ENABLED is off")
    if itype == IntentType.REQUEST_STAGING_DEPLOY and not settings.staging_deploy_enabled:
        return PolicyVerdict(PolicyDecision.DENY, risk, "SOCIETY_STAGING_DEPLOY_ENABLED is off")

    # Payload-level scope checks (typed payload, never free text).
    payload = intent.payload
    scopes = grant.resource_scopes or {}

    if itype in (IntentType.CREATE_TASK, IntentType.CREATE_OFFER):
        amount = int(getattr(payload, "max_budget", None) or getattr(payload, "price", 0) or 0)
        cap = min(int(grant.max_task_escrow_credits or 0), int(settings.max_task_escrow_credits))
        if amount <= 0 or amount > cap:
            return PolicyVerdict(
                PolicyDecision.DENY, risk, f"amount {amount} exceeds escrow cap {cap} (grant/global) for {itype.value}"
            )
        callee = getattr(payload, "callee_agent", None) or getattr(payload, "to_agent", None)
        if callee and (callee == agent.name or callee == str(agent.id)):
            return PolicyVerdict(PolicyDecision.DENY, risk, "an agent cannot create a paid task/offer to itself")

    if itype == IntentType.WRITE_MEMORY:
        memory_scopes = set(scopes.get("memory_scopes") or ["agent"])
        if payload.scope not in memory_scopes:
            return PolicyVerdict(PolicyDecision.DENY, risk, f"memory scope {payload.scope!r} not granted")

    if itype == IntentType.CREATE_GOAL:
        goal_owners = set(scopes.get("goal_owners") or ["agent"])
        if payload.owner not in goal_owners:
            return PolicyVerdict(PolicyDecision.DENY, risk, f"goal owner {payload.owner!r} not granted")

    if itype == IntentType.SEND_MESSAGE:
        targets = scopes.get("message_targets")
        if targets and payload.to_agent and payload.to_agent not in targets:
            return PolicyVerdict(PolicyDecision.DENY, risk, f"message target {payload.to_agent!r} outside granted scope")
        if payload.to_agent and (payload.to_agent == agent.name or payload.to_agent == str(agent.id)):
            return PolicyVerdict(PolicyDecision.DENY, risk, "an agent cannot message itself")

    if itype.value in set(grant.approval_required_intents or []):
        if approval_granted:
            return PolicyVerdict(PolicyDecision.ALLOW, risk, "allowed: human approval recorded and all other checks re-passed")
        return PolicyVerdict(PolicyDecision.APPROVAL_REQUIRED, risk, f"{itype.value} requires human approval for this agent")

    return PolicyVerdict(PolicyDecision.ALLOW, risk, "allowed by grant")


# ── pre-run budget gate ───────────────────────────────────────────────


def _hour_ago(now: datetime) -> datetime:
    return now - timedelta(hours=1)


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def runs_last_hour(db: Session, *, agent_id=None, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    q = db.query(func.count(AgentRun.id)).filter(
        AgentRun.started_at >= _hour_ago(now),
        AgentRun.status.in_([AgentRunStatus.RUNNING, AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.DEAD]),
    )
    if agent_id is not None:
        q = q.filter(AgentRun.agent_id == agent_id)
    return int(q.scalar() or 0)


def spend_today_usd(db: Session, *, agent_id=None, now: Optional[datetime] = None) -> Decimal:
    now = now or datetime.now(timezone.utc)
    q = db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0)).filter(AgentRun.created_at >= _day_start(now))
    if agent_id is not None:
        q = q.filter(AgentRun.agent_id == agent_id)
    return Decimal(str(q.scalar() or 0))


def last_run_started_at(db: Session, agent_id, exclude_run_id=None) -> Optional[datetime]:
    q = db.query(func.max(AgentRun.started_at)).filter(AgentRun.agent_id == agent_id)
    if exclude_run_id is not None:
        q = q.filter(AgentRun.id != exclude_run_id)
    return q.scalar()


def check_run_budget(
    db: Session,
    *,
    agent: Agent,
    grant: Optional[AgentCapabilityGrant],
    settings: SocietySettings,
    run: AgentRun,
    now: Optional[datetime] = None,
) -> RunBudgetVerdict:
    """Cheap gates before spending model budget on a run."""
    now = now or datetime.now(timezone.utc)
    if not settings.runtime_enabled:
        return RunBudgetVerdict(False, "SOCIETY_RUNTIME_ENABLED is off")
    if grant is None:
        return RunBudgetVerdict(False, "agent has no capability grant")
    if not grant.enabled:
        return RunBudgetVerdict(False, "agent grant is disabled")
    if grant.paused_until is not None and grant.paused_until > now:
        return RunBudgetVerdict(False, f"circuit breaker: paused until {grant.paused_until.isoformat()}")

    global_runs = runs_last_hour(db, now=now)
    if global_runs >= settings.max_runs_per_hour:
        return RunBudgetVerdict(False, f"global runs/hour limit reached ({global_runs}/{settings.max_runs_per_hour})")
    agent_runs = runs_last_hour(db, agent_id=agent.id, now=now)
    if agent_runs >= int(grant.max_runs_per_hour):
        return RunBudgetVerdict(False, f"agent runs/hour limit reached ({agent_runs}/{grant.max_runs_per_hour})")

    global_spend = spend_today_usd(db, now=now)
    if global_spend >= settings.daily_model_budget_usd:
        return RunBudgetVerdict(False, f"daily model budget exhausted ({global_spend} >= {settings.daily_model_budget_usd} USD)")
    agent_spend = spend_today_usd(db, agent_id=agent.id, now=now)
    if agent_spend >= Decimal(str(grant.daily_model_budget_usd)):
        return RunBudgetVerdict(False, f"agent daily model budget exhausted ({agent_spend} USD)")

    last = last_run_started_at(db, agent.id, exclude_run_id=run.id)
    cooldown = int(grant.wake_cooldown_seconds or 0)
    if last is not None and cooldown > 0 and (now - last).total_seconds() < cooldown:
        return RunBudgetVerdict(False, f"agent wake cooldown ({cooldown}s) not elapsed")
    return RunBudgetVerdict(True, "")


GRANT_MUTATION_ALLOWED_MODULES = ("services.registry.app.society.seed", "app.society.seed")
