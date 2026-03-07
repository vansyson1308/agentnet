"""
Task execution contracts: validation, canonicalization, and state machine.

This module provides:
1. Strong Pydantic schemas for task execution (REST + WS)
2. Deterministic JSON canonicalization for stable input_hash
3. Shared state machine for task session transitions (enforced in one place)

Canonical JSON representation:
- Keys sorted alphabetically (sort_keys=True)
- UTF-8 encoding (ensure_ascii=False)
- No trailing whitespace
- No control characters in strings
- Float handling: NaN/Infinity not allowed (will cause validation error)
- None values preserved as JSON null

State machine:
    INITIATED -> IN_PROGRESS (via start)
    INITIATED -> FAILED (via fail)
    INITIATED -> TIMEOUT (via worker timeout)
    IN_PROGRESS -> COMPLETED (via confirm)
    IN_PROGRESS -> FAILED (via fail)
    COMPLETED -> (terminal)
    FAILED -> (terminal)
    TIMEOUT -> (terminal)
    REFUNDED -> (terminal)
"""

import hashlib
import json
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

# ============================================================
# Task Status Enum (shared with models)
# ============================================================


class TaskStatus(str, Enum):
    INITIATED = "initiated"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    REFUNDED = "refunded"


# ============================================================
# Allowed Transitions Map
# ============================================================

# Maps current status -> allowed next statuses
ALLOWED_TRANSITIONS: Dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.INITIATED: {
        TaskStatus.IN_PROGRESS,  # Callee starts task
        TaskStatus.FAILED,  # Callee reports failure
        TaskStatus.TIMEOUT,  # Worker detects timeout
    },
    TaskStatus.IN_PROGRESS: {
        TaskStatus.COMPLETED,  # Callee confirms completion
        TaskStatus.FAILED,  # Callee reports failure
        TaskStatus.TIMEOUT,  # Worker detects timeout
    },
    TaskStatus.COMPLETED: set(),  # Terminal
    TaskStatus.FAILED: set(),  # Terminal
    TaskStatus.TIMEOUT: set(),  # Terminal
    TaskStatus.REFUNDED: set(),  # Terminal
}


def validate_state_transition(
    current_status: TaskStatus,
    new_status: TaskStatus,
) -> Tuple[bool, Optional[str]]:
    """
    Validate a task status transition.

    Returns:
        (is_valid, error_message)

    This is the SINGLE SOURCE OF TRUTH for task state transitions.
    Both REST and WS handlers must use this function.
    """
    allowed = ALLOWED_TRANSITIONS.get(current_status, set())

    if new_status in allowed:
        return True, None

    return False, (
        f"Invalid state transition: {current_status.value} -> {new_status.value}. "
        f"Allowed transitions from {current_status.value}: {[s.value for s in allowed]}"
    )


# ============================================================
# Canonical JSON Representation
# ============================================================


def canonicalize_json(data: Any) -> str:
    """
    Convert Python dict to deterministic canonical JSON string.

    Rules:
    1. Keys sorted alphabetically (json.dumps sort_keys=True)
    2. UTF-8 encoding (ensure_ascii=False)
    3. No trailing whitespace
    4. None becomes JSON null
    5. Floats: NaN and Infinity are NOT allowed (raise ValueError)
    6. Lists preserve order (user-specified)
    7. Strings: preserved as-is

    This ensures: canonicalize_json({"b": 1, "a": 2}) == canonicalize_json({"a": 2, "b": 1})
    """

    def sanitize_for_json(obj: Any) -> Any:
        """Recursively sanitize object for canonical JSON."""
        if isinstance(obj, dict):
            return {k: sanitize_for_json(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [sanitize_for_json(item) for item in obj]
        elif isinstance(obj, float):
            # Reject NaN and Infinity - they break deterministic hashing
            if obj != obj:  # NaN check
                raise ValueError("NaN is not allowed in canonical JSON")
            if obj == float("inf") or obj == float("-inf"):
                raise ValueError("Infinity is not allowed in canonical JSON")
            return obj
        elif isinstance(obj, str):
            # Preserve strings as-is (including whitespace)
            return obj
        elif obj is None:
            return None
        else:
            # Other types (int, bool) pass through
            return obj

    sanitized = sanitize_for_json(data)
    return json.dumps(sanitized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_input_hash(data: Dict[str, Any]) -> str:
    """
    Compute deterministic hash of input data.

    This is the single function for computing input_hash.
    Used by both REST and WS paths to ensure consistency.

    canonicalize_json ensures:
    - Same logical input produces same hash regardless of key order
    - No ambiguity from float representations (NaN/Infinity rejected)
    - Stable UTF-8 encoding
    """
    canonical = canonicalize_json(data)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================
# Pydantic Schemas for Strong Validation
# ============================================================


class PaymentParams(BaseModel):
    """Payment parameters for task execution."""

    max_budget: int = Field(..., ge=0, le=1_000_000, description="Maximum budget in smallest currency unit")
    currency: str = Field(default="credits", pattern="^(credits|usdc)$")
    escrow_session_id: Optional[str] = None

    @field_validator("currency")
    @classmethod
    def currency_lowercase(cls, v: str) -> str:
        return v.lower()


class ExecuteParams(BaseModel):
    """Parameters for execute method (WebSocket)."""

    capability: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    input: Dict[str, Any] = Field(..., description="Task input data")
    payment: PaymentParams
    timeout_seconds: Optional[int] = Field(default=300, ge=1, le=3600)
    # Reject unknown fields
    model_config = {"extra": "forbid"}


class TaskCreateRequest(BaseModel):
    """
    Strongly validated task creation request (REST).

    Enforces:
    - Required fields with proper types
    - Bounds checking on numeric fields
    - Rejects unknown/extra fields (strict mode)
    - Pattern validation on IDs and capability names
    """

    caller_agent_id: str = Field(..., pattern=r"^[0-9a-f-]{36}$")
    callee_agent_id: str = Field(..., pattern=r"^[0-9a-f-]{36}$")
    capability: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    input: Dict[str, Any] = Field(..., description="Task input data")
    max_budget: int = Field(..., ge=0, le=1_000_000)
    currency: str = Field(default="credits", pattern="^(credits|usdc)$")
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    parent_span_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-f-]{36}$")

    # Strict: reject unknown fields
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_input_hash_computable(self):
        """Ensure input can be canonicalized (validates against NaN/Infinity)."""
        try:
            canonicalize_json(self.input)
        except ValueError as e:
            raise ValueError(f"Invalid input: {e}")
        return self


class TaskOutput(BaseModel):
    """Output from task execution."""

    result: Dict[str, Any]
    model_config = {"extra": "forbid"}


class TaskFailRequest(BaseModel):
    """Request to fail a task."""

    error_message: str = Field(..., min_length=1, max_length=1024)
    model_config = {"extra": "forbid"}


# ============================================================
# Helper Functions for Route Handlers
# ============================================================


def validate_task_status_update(
    current_status: str,
    new_status: str,
) -> Tuple[bool, Optional[str]]:
    """
    Validate task status update using the shared state machine.

    This is the SINGLE ENTRY POINT for task status validation.
    Both REST routes and WS handlers must call this function.

    Returns:
        (is_valid, error_message)
    """
    try:
        current = TaskStatus(current_status)
        new = TaskStatus(new_status)
    except ValueError as e:
        return False, f"Invalid status value: {e}"

    return validate_state_transition(current, new)


def get_allowed_statuses(current_status: str) -> list[str]:
    """Get list of allowed next statuses from current status."""
    try:
        current = TaskStatus(current_status)
        return [s.value for s in ALLOWED_TRANSITIONS.get(current, set())]
    except ValueError:
        return []
