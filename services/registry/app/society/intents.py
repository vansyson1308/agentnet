"""Typed intent contract for the Autonomous Society Runtime.

The model never receives infrastructure access. It returns an
``AgentDecision`` — a summary, a bounded list of *typed* intents, and a
sleep hint. Every intent payload is a strict Pydantic model
(``extra="forbid"``, bounded lengths). Anything that does not validate is
persisted as INVALID and never executed. Free text is DATA, never a
command: there is no intent that takes a shell string, a SQL string or a
URL to fetch.

Two groups of intent types exist:

* ``ALLOWED_INTENT_TYPES`` — executable by ``executor.py`` when policy
  allows (role grant + risk ceiling + budget + feature flag).
* ``FORBIDDEN_INTENT_TYPES`` — recognised so an attempt is *recorded* as a
  policy denial (good for red-team observability) but has no executor.
  These are the privilege-escalation / money / infra surfaces.

Risk classes (policy.py maps type -> class):
  LOW    read state, memory, internal message, proposal, non-financial goal
  MEDIUM paid task, offer/negotiation within budget, code change request,
         candidate build/evaluation, staging deploy request
  HIGH   production deploy, grants/budget/secret/auth/wallet changes,
         shell — denied or approval-gated; never auto-executed in v1
"""

from __future__ import annotations

import enum
import hashlib
import json
import uuid
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MAX_TEXT = 4000
MAX_TITLE = 255


class IntentType(str, enum.Enum):
    # ── communication / cognition (LOW) ──
    SEND_MESSAGE = "SEND_MESSAGE"
    WRITE_MEMORY = "WRITE_MEMORY"
    CREATE_GOAL = "CREATE_GOAL"
    UPDATE_GOAL = "UPDATE_GOAL"
    CREATE_IMPROVEMENT = "CREATE_IMPROVEMENT"
    REVIEW_IMPROVEMENT = "REVIEW_IMPROVEMENT"
    SLEEP = "SLEEP"
    # ── economy (MEDIUM) ──
    CREATE_OFFER = "CREATE_OFFER"
    COUNTER_OFFER = "COUNTER_OFFER"
    ACCEPT_OFFER = "ACCEPT_OFFER"
    CREATE_TASK = "CREATE_TASK"
    START_TASK = "START_TASK"
    COMPLETE_TASK = "COMPLETE_TASK"
    FAIL_TASK = "FAIL_TASK"
    # ── engineering loop (MEDIUM) ──
    REQUEST_CODE_CHANGE = "REQUEST_CODE_CHANGE"
    SUBMIT_CODE_CANDIDATE = "SUBMIT_CODE_CANDIDATE"
    REQUEST_QA = "REQUEST_QA"
    EVALUATE_CODE_CANDIDATE = "EVALUATE_CODE_CANDIDATE"
    SECURITY_REVIEW_CANDIDATE = "SECURITY_REVIEW_CANDIDATE"
    REQUEST_STAGING_DEPLOY = "REQUEST_STAGING_DEPLOY"
    # ── HIGH: recognised, never auto-executed ──
    REQUEST_PRODUCTION_DEPLOY = "REQUEST_PRODUCTION_DEPLOY"
    SHELL_EXEC = "SHELL_EXEC"
    GRANT_CAPABILITY = "GRANT_CAPABILITY"
    MODIFY_BUDGET = "MODIFY_BUDGET"
    TRANSFER_FUNDS = "TRANSFER_FUNDS"
    MODIFY_WALLET = "MODIFY_WALLET"
    MODIFY_SECRET = "MODIFY_SECRET"
    CHANGE_AUTH_POLICY = "CHANGE_AUTH_POLICY"
    DELETE_DATA = "DELETE_DATA"
    OPEN_NETWORK_ACCESS = "OPEN_NETWORK_ACCESS"
    RUN_MIGRATION = "RUN_MIGRATION"


FORBIDDEN_INTENT_TYPES = frozenset(
    {
        IntentType.REQUEST_PRODUCTION_DEPLOY,
        IntentType.SHELL_EXEC,
        IntentType.GRANT_CAPABILITY,
        IntentType.MODIFY_BUDGET,
        IntentType.TRANSFER_FUNDS,
        IntentType.MODIFY_WALLET,
        IntentType.MODIFY_SECRET,
        IntentType.CHANGE_AUTH_POLICY,
        IntentType.DELETE_DATA,
        IntentType.OPEN_NETWORK_ACCESS,
        IntentType.RUN_MIGRATION,
    }
)
ALLOWED_INTENT_TYPES = frozenset(t for t in IntentType if t not in FORBIDDEN_INTENT_TYPES)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


AgentRef = str  # agent name (e.g. "Society_Architect") or UUID string


class SendMessagePayload(_Strict):
    to_agent: Optional[AgentRef] = Field(None, max_length=255, description="agent name or id; null = broadcast")
    title: str = Field(..., min_length=1, max_length=MAX_TITLE)
    content: str = Field(..., min_length=1, max_length=MAX_TEXT)
    message_type: Literal["note", "alert", "proposal", "review_request", "review_result", "completed", "system"] = "note"
    thread_id: Optional[uuid.UUID] = None


class WriteMemoryPayload(_Strict):
    title: str = Field(..., min_length=1, max_length=MAX_TITLE)
    content: str = Field(..., min_length=1, max_length=MAX_TEXT)
    scope: Literal["agent", "society"] = "agent"
    tags: List[str] = Field(default_factory=list, max_length=12)
    importance: int = Field(50, ge=0, le=100)
    source_task_id: Optional[uuid.UUID] = None

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: List[str]) -> List[str]:
        return [t[:48] for t in v if t]


class CreateGoalPayload(_Strict):
    title: str = Field(..., min_length=1, max_length=MAX_TITLE)
    description: Optional[str] = Field(None, max_length=MAX_TEXT)
    owner: Literal["agent", "society"] = "agent"
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    success_criteria: List[str] = Field(default_factory=list, max_length=10)
    parent_goal_id: Optional[uuid.UUID] = None


class UpdateGoalPayload(_Strict):
    goal_id: uuid.UUID
    status: Optional[Literal["active", "paused", "completed", "failed", "cancelled"]] = None
    priority: Optional[Literal["low", "medium", "high", "critical"]] = None
    note: Optional[str] = Field(None, max_length=MAX_TEXT)


class CreateImprovementPayload(_Strict):
    title: str = Field(..., min_length=1, max_length=MAX_TITLE)
    problem: str = Field(..., min_length=1, max_length=MAX_TEXT)
    root_cause: Optional[str] = Field(None, max_length=MAX_TEXT)
    proposed_change: str = Field(..., min_length=1, max_length=MAX_TEXT)
    expected_benefit: Optional[str] = Field(None, max_length=MAX_TEXT)
    risk: Optional[str] = Field(None, max_length=MAX_TEXT)
    importance: int = Field(50, ge=0, le=100)
    target_scope: Literal["agent", "platform"] = "platform"
    source_task_id: Optional[uuid.UUID] = None


class ReviewImprovementPayload(_Strict):
    proposal_id: uuid.UUID
    decision: Literal["approve", "reject"]
    reason: str = Field(..., min_length=1, max_length=MAX_TEXT)


class SleepPayload(_Strict):
    seconds: int = Field(300, ge=0, le=86400)


class CreateOfferPayload(_Strict):
    to_agent: AgentRef = Field(..., max_length=255)
    title: str = Field(..., min_length=1, max_length=MAX_TITLE)
    description: Optional[str] = Field(None, max_length=MAX_TEXT)
    price: int = Field(..., ge=1)
    expires_in_seconds: int = Field(3600, ge=60, le=7 * 86400)


class CounterOfferPayload(_Strict):
    offer_id: uuid.UUID
    price: int = Field(..., ge=1)
    terms: Optional[str] = Field(None, max_length=MAX_TEXT)


class AcceptOfferPayload(_Strict):
    offer_id: uuid.UUID


class CreateTaskPayload(_Strict):
    callee_agent: AgentRef = Field(..., max_length=255)
    capability: str = Field(..., min_length=1, max_length=128)
    input: Dict[str, Any] = Field(default_factory=dict)
    max_budget: int = Field(..., ge=1)
    timeout_seconds: int = Field(600, ge=30, le=86400)
    goal_id: Optional[uuid.UUID] = None
    proposal_id: Optional[uuid.UUID] = None


class StartTaskPayload(_Strict):
    task_id: uuid.UUID


class CompleteTaskPayload(_Strict):
    task_id: uuid.UUID
    output: Dict[str, Any] = Field(default_factory=dict)


class FailTaskPayload(_Strict):
    task_id: uuid.UUID
    error: str = Field(..., min_length=1, max_length=MAX_TEXT)


class CodeChangeSpec(_Strict):
    """Bounded implementation task. ``files_allowed`` is a hard allow-list
    enforced by the Builder workspace; ``acceptance`` are pytest node ids /
    file paths the QA step will run (never shell strings)."""

    description: str = Field(..., min_length=1, max_length=MAX_TEXT)
    files_allowed: List[str] = Field(..., min_length=1, max_length=20)
    acceptance_tests: List[str] = Field(default_factory=list, max_length=20)
    must_compile: bool = True
    kind: Literal["docs", "test_fixture", "code"] = "docs"

    @field_validator("files_allowed", "acceptance_tests")
    @classmethod
    def _no_traversal(cls, v: List[str]) -> List[str]:
        for p in v:
            if p.startswith("/") or ".." in p.split("/") or p.startswith("~") or "\\" in p or len(p) > 255:
                raise ValueError(f"unsafe path: {p!r}")
        return v


class RequestCodeChangePayload(_Strict):
    title: str = Field(..., min_length=1, max_length=MAX_TITLE)
    spec: CodeChangeSpec
    proposal_id: Optional[uuid.UUID] = None
    goal_id: Optional[uuid.UUID] = None
    task_id: Optional[uuid.UUID] = None
    requires_security_review: bool = False


class FileEdit(_Strict):
    path: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., max_length=200_000)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, v: str) -> str:
        if v.startswith("/") or ".." in v.split("/") or v.startswith("~") or "\\" in v or "\0" in v:
            raise ValueError(f"unsafe path: {v!r}")
        return v


class SubmitCodeCandidatePayload(_Strict):
    candidate_id: uuid.UUID
    edits: List[FileEdit] = Field(..., min_length=1, max_length=20)
    summary: str = Field(..., min_length=1, max_length=MAX_TEXT)


class CandidateRefPayload(_Strict):
    candidate_id: uuid.UUID


class SecurityReviewPayload(_Strict):
    candidate_id: uuid.UUID
    verdict: Literal["pass", "fail"]
    findings: List[str] = Field(default_factory=list, max_length=20)


class OpaquePayload(_Strict):
    """Payload for recognised-but-forbidden intents: kept for the audit
    trail, never executed. Extra keys are allowed here on purpose so the
    denial record preserves what the model tried to do."""

    model_config = ConfigDict(extra="allow")


PAYLOAD_MODELS: Dict[IntentType, type] = {
    IntentType.SEND_MESSAGE: SendMessagePayload,
    IntentType.WRITE_MEMORY: WriteMemoryPayload,
    IntentType.CREATE_GOAL: CreateGoalPayload,
    IntentType.UPDATE_GOAL: UpdateGoalPayload,
    IntentType.CREATE_IMPROVEMENT: CreateImprovementPayload,
    IntentType.REVIEW_IMPROVEMENT: ReviewImprovementPayload,
    IntentType.SLEEP: SleepPayload,
    IntentType.CREATE_OFFER: CreateOfferPayload,
    IntentType.COUNTER_OFFER: CounterOfferPayload,
    IntentType.ACCEPT_OFFER: AcceptOfferPayload,
    IntentType.CREATE_TASK: CreateTaskPayload,
    IntentType.START_TASK: StartTaskPayload,
    IntentType.COMPLETE_TASK: CompleteTaskPayload,
    IntentType.FAIL_TASK: FailTaskPayload,
    IntentType.REQUEST_CODE_CHANGE: RequestCodeChangePayload,
    IntentType.SUBMIT_CODE_CANDIDATE: SubmitCodeCandidatePayload,
    IntentType.REQUEST_QA: CandidateRefPayload,
    IntentType.EVALUATE_CODE_CANDIDATE: CandidateRefPayload,
    IntentType.SECURITY_REVIEW_CANDIDATE: SecurityReviewPayload,
    IntentType.REQUEST_STAGING_DEPLOY: CandidateRefPayload,
}
for _t in FORBIDDEN_INTENT_TYPES:
    PAYLOAD_MODELS[_t] = OpaquePayload


class IntentSpec(BaseModel):
    """One intent as emitted by the model (pre-validation shape)."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., min_length=1, max_length=64)
    payload: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = Field(None, max_length=120)


class AgentDecision(BaseModel):
    """Structured output contract of ``CognitiveModel.decide``."""

    model_config = ConfigDict(extra="forbid")

    decision_summary: str = Field(..., min_length=1, max_length=1000)
    intents: List[IntentSpec] = Field(default_factory=list, max_length=50)
    sleep_for_seconds: int = Field(300, ge=0, le=86400)


class ValidatedIntent(BaseModel):
    """Result of validating one IntentSpec. ``payload`` is the typed model
    when ``valid`` else the raw dict for the audit trail."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    seq: int
    type_name: str
    intent_type: Optional[IntentType] = None
    valid: bool
    error: Optional[str] = None
    payload: Any = None
    raw_payload: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str

    @property
    def is_forbidden(self) -> bool:
        return self.intent_type in FORBIDDEN_INTENT_TYPES


class DecisionValidationError(ValueError):
    """Top-level decision did not match the contract (not a valid JSON
    object of the right shape). The run fails and may be retried."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def derive_idempotency_key(run_id: uuid.UUID, seq: int, type_name: str, payload: Dict[str, Any], explicit: Optional[str]) -> str:
    """Deterministic per-(run, seq, content) key so a re-executed run after
    a crash maps to the same intent rows (UNIQUE in the DB). An explicit
    model-supplied key is namespaced by run so a model cannot collide with
    another agent's intent."""
    if explicit:
        return f"{run_id}:{hashlib.sha256(explicit.encode('utf-8')).hexdigest()[:24]}"
    digest = hashlib.sha256(f"{run_id}:{seq}:{type_name}:{_canonical(payload)}".encode("utf-8")).hexdigest()
    return f"{run_id}:{digest[:24]}"


def parse_decision(raw: Any, *, max_intents: int) -> AgentDecision:
    """Validate the model's top-level output. Raises DecisionValidationError."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DecisionValidationError(f"decision is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise DecisionValidationError("decision must be a JSON object")
    try:
        decision = AgentDecision.model_validate(raw)
    except ValidationError as exc:
        raise DecisionValidationError(f"decision schema violation: {exc.errors()[:3]}") from exc
    if len(decision.intents) > max_intents:
        # Truncate rather than fail: the first N intents are still valid work.
        decision.intents = decision.intents[:max_intents]
    return decision


def validate_intents(decision: AgentDecision, run_id: uuid.UUID) -> List[ValidatedIntent]:
    out: List[ValidatedIntent] = []
    for seq, spec in enumerate(decision.intents):
        key = derive_idempotency_key(run_id, seq, spec.type, spec.payload, spec.idempotency_key)
        try:
            itype = IntentType(spec.type)
        except ValueError:
            out.append(
                ValidatedIntent(
                    seq=seq,
                    type_name=spec.type[:64],
                    intent_type=None,
                    valid=False,
                    error=f"unknown intent type {spec.type!r}",
                    raw_payload=spec.payload,
                    idempotency_key=key,
                )
            )
            continue
        model = PAYLOAD_MODELS[itype]
        try:
            payload = model.model_validate(spec.payload)
        except ValidationError as exc:
            out.append(
                ValidatedIntent(
                    seq=seq,
                    type_name=itype.value,
                    intent_type=itype,
                    valid=False,
                    error=f"payload schema violation: {exc.errors()[:3]}",
                    raw_payload=spec.payload,
                    idempotency_key=key,
                )
            )
            continue
        out.append(
            ValidatedIntent(
                seq=seq,
                type_name=itype.value,
                intent_type=itype,
                valid=True,
                payload=payload,
                raw_payload=spec.payload,
                idempotency_key=key,
            )
        )
    return out


def payload_to_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, BaseModel):
        return json.loads(payload.model_dump_json())
    if isinstance(payload, dict):
        return json.loads(_canonical(payload))
    return {}


__all__ = [
    "IntentType",
    "ALLOWED_INTENT_TYPES",
    "FORBIDDEN_INTENT_TYPES",
    "IntentSpec",
    "AgentDecision",
    "ValidatedIntent",
    "DecisionValidationError",
    "parse_decision",
    "validate_intents",
    "derive_idempotency_key",
    "payload_to_dict",
    "PAYLOAD_MODELS",
    "CodeChangeSpec",
    "FileEdit",
]
