"""Role definitions for the internal society fleet.

A role is configuration, not code: mission text, the intent types the role
may emit, its risk ceiling, budgets, and the event types that wake it.
Defaults below are the v1 fleet (Governor, Scout, Architect, Builder, QA,
Security). Operators can extend or override them with a JSON file named by
``SOCIETY_ROLES_FILE`` (merged by role key; unknown keys rejected) — no code
change is needed to add a role.

``seed.py`` turns these into ``agents`` rows (reusing an existing agent by
name) and ``agent_capability_grants`` rows. The runtime never reads this
module to decide permissions at execution time — it reads the grant row —
so a change here only takes effect through an explicit re-seed by an
operator. That is deliberate: an agent cannot edit its own permissions by
writing a file.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from ..models import IntentRiskClass
from .events import EventType
from .intents import IntentType

logger = logging.getLogger(__name__)

ROLE_GOVERNOR = "governor"
ROLE_SCOUT = "scout"
ROLE_ARCHITECT = "architect"
ROLE_BUILDER = "builder"
ROLE_QA = "qa"
ROLE_SECURITY = "security"

ALL_ROLES = (ROLE_GOVERNOR, ROLE_SCOUT, ROLE_ARCHITECT, ROLE_BUILDER, ROLE_QA, ROLE_SECURITY)


@dataclass(frozen=True)
class RoleDefinition:
    role: str
    agent_name: str
    mission: str
    description: str
    allowed_intents: Tuple[str, ...]
    subscriptions: Tuple[str, ...]
    risk_ceiling: str = IntentRiskClass.LOW.value
    approval_required_intents: Tuple[str, ...] = ()
    resource_scopes: Dict[str, object] = field(default_factory=dict)
    capabilities: Tuple[Dict[str, object], ...] = ()
    max_runs_per_hour: int = 20
    max_intents_per_run: int = 5
    daily_model_budget_usd: Decimal = Decimal("0.50")
    max_task_escrow_credits: int = 0
    wake_cooldown_seconds: int = 30

    def to_grant_fields(self) -> dict:
        return {
            "role": self.role,
            "allowed_intents": list(self.allowed_intents),
            "approval_required_intents": list(self.approval_required_intents),
            "resource_scopes": dict(self.resource_scopes),
            "risk_ceiling": self.risk_ceiling,
            "max_runs_per_hour": self.max_runs_per_hour,
            "max_intents_per_run": self.max_intents_per_run,
            "daily_model_budget_usd": self.daily_model_budget_usd,
            "max_task_escrow_credits": self.max_task_escrow_credits,
            "wake_cooldown_seconds": self.wake_cooldown_seconds,
        }


_LOW_COMMON = (
    IntentType.SEND_MESSAGE.value,
    IntentType.WRITE_MEMORY.value,
    IntentType.SLEEP.value,
)

# Capability entries follow the shape task_service._validate_capability
# expects (name + input_schema + price). The price is what a requester must
# escrow (task_service reserves exactly `price` from the caller's wallet).
_BUILDER_CAPABILITY = {
    "name": "implement_change",
    "version": "1.0",
    "description": "Implement a bounded, QA-verified code change in an isolated worktree",
    "input_schema": {
        "type": "object",
        "properties": {"candidate_id": {"type": "string"}, "title": {"type": "string"}},
        "required": ["candidate_id"],
    },
    "output_schema": {"type": "object"},
    "price": 10,  # credits escrowed by the requester per implementation task
}
_QA_CAPABILITY = {
    "name": "evaluate_candidate",
    "version": "1.0",
    "description": "Independently evaluate a code candidate (compile + acceptance tests)",
    "input_schema": {"type": "object", "properties": {"candidate_id": {"type": "string"}}, "required": ["candidate_id"]},
    "output_schema": {"type": "object"},
    "price": 5,
}

DEFAULT_ROLES: Dict[str, RoleDefinition] = {
    ROLE_GOVERNOR: RoleDefinition(
        role=ROLE_GOVERNOR,
        agent_name="Society_Governor",
        mission=(
            "Maintain the overall AgentNet mission: a safe, durable, observable economy of agents. "
            "Choose and prioritise the few goals that matter, review improvement proposals, and keep "
            "the society bounded — never expand any agent's authority."
        ),
        description="Governor — mission keeper, goal prioritisation, proposal review",
        allowed_intents=_LOW_COMMON
        + (
            IntentType.CREATE_GOAL.value,
            IntentType.UPDATE_GOAL.value,
            IntentType.REVIEW_IMPROVEMENT.value,
        ),
        subscriptions=(
            EventType.PROPOSAL_CREATED,
            EventType.CODE_CANDIDATE_READY,
            EventType.CODE_CANDIDATE_REJECTED,
            EventType.SOCIETY_HEARTBEAT,
        ),
        resource_scopes={"memory_scopes": ["agent", "society"], "goal_owners": ["agent", "society"]},
        max_runs_per_hour=12,
        max_intents_per_run=4,
    ),
    ROLE_SCOUT: RoleDefinition(
        role=ROLE_SCOUT,
        agent_name="Society_Scout",
        mission=(
            "Observe platform behaviour and surface meaningful, evidence-backed problems or opportunities: "
            "task failures, repeated errors, inactivity, reliability degradation. Turn a real signal into one "
            "concrete improvement proposal; never invent work and never repeat a proposal already open."
        ),
        description="Scout — observation, anomaly triage, improvement proposals",
        allowed_intents=_LOW_COMMON
        + (
            IntentType.CREATE_IMPROVEMENT.value,
            IntentType.CREATE_GOAL.value,
        ),
        subscriptions=(
            EventType.PLATFORM_METRIC_ANOMALY,
            EventType.TASK_FAILED,
            EventType.TASK_TIMEOUT,
            EventType.QA_FAILED,
            EventType.AGENT_INACTIVE,
            EventType.CODE_CANDIDATE_READY,
            EventType.CODE_CANDIDATE_REJECTED,
        ),
        resource_scopes={"memory_scopes": ["agent", "society"], "goal_owners": ["agent"]},
        max_runs_per_hour=30,
        max_intents_per_run=4,
    ),
    ROLE_ARCHITECT: RoleDefinition(
        role=ROLE_ARCHITECT,
        agent_name="Society_Architect",
        mission=(
            "Translate approved proposals into bounded technical designs: a small allow-list of files, "
            "explicit acceptance tests, and a clear description. Split work so the Builder can finish "
            "in one isolated change and QA can verify it mechanically."
        ),
        description="Architect — bounded design, task decomposition, acceptance criteria",
        allowed_intents=_LOW_COMMON
        + (
            IntentType.REQUEST_CODE_CHANGE.value,
            IntentType.CREATE_TASK.value,
            IntentType.UPDATE_GOAL.value,
        ),
        subscriptions=(
            EventType.PROPOSAL_APPROVED,
            EventType.CODE_CANDIDATE_QA_FAILED,
            EventType.CODE_CANDIDATE_READY,
        ),
        risk_ceiling=IntentRiskClass.MEDIUM.value,
        resource_scopes={"memory_scopes": ["agent", "society"]},
        capabilities=(),
        max_runs_per_hour=20,
        max_intents_per_run=4,
        max_task_escrow_credits=50,
    ),
    ROLE_BUILDER: RoleDefinition(
        role=ROLE_BUILDER,
        agent_name="Society_Builder",
        mission=(
            "Implement exactly the bounded change requested, inside an isolated git worktree on an "
            "agentnet-auto/* branch. Touch only the allowed files, keep the diff minimal, and hand the "
            "candidate to QA. Never merge, never deploy, never widen scope."
        ),
        description="Builder — isolated worktree implementation",
        allowed_intents=_LOW_COMMON
        + (
            IntentType.SUBMIT_CODE_CANDIDATE.value,
            IntentType.REQUEST_QA.value,
            IntentType.START_TASK.value,
            IntentType.COMPLETE_TASK.value,
            IntentType.FAIL_TASK.value,
        ),
        subscriptions=(
            EventType.CODE_CHANGE_REQUESTED,
            EventType.CODE_CANDIDATE_QA_FAILED,
            EventType.CODE_CANDIDATE_READY,
            EventType.CODE_CANDIDATE_REJECTED,
        ),
        risk_ceiling=IntentRiskClass.MEDIUM.value,
        resource_scopes={"memory_scopes": ["agent"]},
        capabilities=(_BUILDER_CAPABILITY,),
        max_runs_per_hour=20,
        max_intents_per_run=3,
        wake_cooldown_seconds=10,
    ),
    ROLE_QA: RoleDefinition(
        role=ROLE_QA,
        agent_name="Society_QA",
        mission=(
            "Independently verify Builder output. Run the acceptance tests and regression checks the "
            "runtime provides, examine the diff, and report an explicit PASS/FAIL with evidence. Never "
            "trust the Builder's claims and never approve your own work."
        ),
        description="QA — independent evaluation, PASS/FAIL with evidence",
        allowed_intents=_LOW_COMMON
        + (
            IntentType.EVALUATE_CODE_CANDIDATE.value,
            IntentType.CREATE_IMPROVEMENT.value,
        ),
        subscriptions=(EventType.CODE_CANDIDATE_BUILT,),
        risk_ceiling=IntentRiskClass.MEDIUM.value,
        resource_scopes={"memory_scopes": ["agent", "society"]},
        capabilities=(_QA_CAPABILITY,),
        max_runs_per_hour=20,
        max_intents_per_run=3,
        wake_cooldown_seconds=10,
    ),
    ROLE_SECURITY: RoleDefinition(
        role=ROLE_SECURITY,
        agent_name="Society_Security",
        mission=(
            "Review code candidates that touch risky surfaces (auth, network, shell, secrets, permissions, "
            "payment, capability grants, sandbox boundaries, dependencies). Fail closed: any finding that "
            "cannot be ruled out is a FAIL. You are independently permissioned and cannot be overruled by QA."
        ),
        description="Security Reviewer — risky-surface review, fail closed",
        allowed_intents=_LOW_COMMON + (IntentType.SECURITY_REVIEW_CANDIDATE.value,),
        subscriptions=(EventType.CODE_CANDIDATE_SECURITY_REVIEW,),
        risk_ceiling=IntentRiskClass.MEDIUM.value,
        resource_scopes={"memory_scopes": ["agent", "society"]},
        max_runs_per_hour=20,
        max_intents_per_run=3,
        wake_cooldown_seconds=10,
    ),
}

_OVERRIDABLE = {
    "agent_name",
    "mission",
    "description",
    "allowed_intents",
    "subscriptions",
    "risk_ceiling",
    "approval_required_intents",
    "resource_scopes",
    "max_runs_per_hour",
    "max_intents_per_run",
    "daily_model_budget_usd",
    "max_task_escrow_credits",
    "wake_cooldown_seconds",
}


def _apply_override(base: RoleDefinition, override: dict) -> RoleDefinition:
    unknown = set(override) - _OVERRIDABLE
    if unknown:
        raise ValueError(f"role override for {base.role!r} has unknown keys: {sorted(unknown)}")
    kwargs = {}
    for k, v in override.items():
        if k in ("allowed_intents", "approval_required_intents"):
            bad = [x for x in v if x not in {t.value for t in IntentType}]
            if bad:
                raise ValueError(f"role {base.role!r}: unknown intent types {bad}")
            kwargs[k] = tuple(v)
        elif k == "subscriptions":
            kwargs[k] = tuple(v)
        elif k == "risk_ceiling":
            kwargs[k] = IntentRiskClass(v).value
        elif k == "daily_model_budget_usd":
            kwargs[k] = Decimal(str(v))
        else:
            kwargs[k] = v
    return replace(base, **kwargs)


def load_role_definitions(path: Optional[str] = None) -> Dict[str, RoleDefinition]:
    """Defaults merged with an optional JSON override file.

    File shape: ``{"<role>": {<overridable fields>}, "extra_role": {...all fields...}}``.
    New roles must provide every non-defaulted field.
    """
    roles = dict(DEFAULT_ROLES)
    path = path or os.getenv("SOCIETY_ROLES_FILE", "")
    if not path:
        return roles
    with open(path, encoding="utf-8") as fh:
        overrides = json.load(fh)
    if not isinstance(overrides, dict):
        raise ValueError("SOCIETY_ROLES_FILE must contain a JSON object keyed by role")
    for role, override in overrides.items():
        if role in roles:
            roles[role] = _apply_override(roles[role], override)
        else:
            required = {"agent_name", "mission", "description", "allowed_intents", "subscriptions"}
            missing = required - set(override)
            if missing:
                raise ValueError(f"new role {role!r} is missing {sorted(missing)}")
            roles[role] = _apply_override(
                RoleDefinition(
                    role=role,
                    agent_name=override["agent_name"],
                    mission=override["mission"],
                    description=override["description"],
                    allowed_intents=tuple(override["allowed_intents"]),
                    subscriptions=tuple(override["subscriptions"]),
                ),
                {k: v for k, v in override.items() if k not in required},
            )
    return roles


def subscriptions_by_event(roles: Dict[str, RoleDefinition]) -> Dict[str, List[str]]:
    """event_type -> [role, ...] routing table used by the dispatcher."""
    table: Dict[str, List[str]] = {}
    for r in roles.values():
        for evt in r.subscriptions:
            table.setdefault(evt, []).append(r.role)
    return table


def role_as_dict(r: RoleDefinition) -> dict:
    d = asdict(r)
    d["daily_model_budget_usd"] = str(r.daily_model_budget_usd)
    return d
