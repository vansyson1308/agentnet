"""
Approval Workflow for Task Escrow Payments.

This module provides:
1. State machine for approval requests
2. Idempotent approval/denial
3. Reserved funds management on approval/rejection

State Machine:
    PENDING -> APPROVED (via approve action)
    PENDING -> DENIED (via deny action)
    PENDING -> EXPIRED (via worker timeout)
    APPROVED -> (terminal)
    DENIED -> (terminal)
    EXPIRED -> (terminal)

Idempotency:
    - Approving an already APPROVED request returns success (no-op)
    - Denying an already DENIED request returns success (no-op)
    - Cannot approve/deny EXPIRED requests (must be re-created)
"""

from enum import Enum
from typing import Dict, Set, Tuple, Optional
from datetime import datetime, timedelta


class EscrowApprovalStatus(str, Enum):
    """Status for escrow payment approvals."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


# Maps current status -> allowed next statuses
APPROVAL_ALLOWED_TRANSITIONS: Dict[EscrowApprovalStatus, Set[EscrowApprovalStatus]] = {
    EscrowApprovalStatus.PENDING: {
        EscrowApprovalStatus.APPROVED,
        EscrowApprovalStatus.DENIED,
        EscrowApprovalStatus.EXPIRED,
    },
    EscrowApprovalStatus.APPROVED: set(),  # Terminal
    EscrowApprovalStatus.DENIED: set(),  # Terminal
    EscrowApprovalStatus.EXPIRED: set(),  # Terminal
}


# Default approval timeout (e.g., 24 hours)
DEFAULT_APPROVAL_TIMEOUT_HOURS = 24


def validate_approval_transition(
    current_status: EscrowApprovalStatus,
    new_status: EscrowApprovalStatus,
) -> Tuple[bool, Optional[str]]:
    """
    Validate an approval status transition.

    Returns:
        (is_valid, error_message)
    """
    allowed = APPROVAL_ALLOWED_TRANSITIONS.get(current_status, set())

    if new_status in allowed:
        return True, None

    return False, (
        f"Invalid state transition: {current_status.value} -> {new_status.value}. "
        f"Allowed transitions from {current_status.value}: {[s.value for s in allowed]}"
    )


def is_idempotent_action(
    current_status: EscrowApprovalStatus,
    action: EscrowApprovalStatus,
) -> bool:
    """
    Check if an action is idempotent for the current status.

    Example: Approving an already APPROVED request is idempotent (no-op).
    """
    if (
        current_status == EscrowApprovalStatus.APPROVED
        and action == EscrowApprovalStatus.APPROVED
    ):
        return True
    if (
        current_status == EscrowApprovalStatus.DENIED
        and action == EscrowApprovalStatus.DENIED
    ):
        return True
    return False


def get_approval_timeout(hours: int = None) -> datetime:
    """Get the timeout datetime for an approval request."""
    return datetime.utcnow() + timedelta(hours=hours or DEFAULT_APPROVAL_TIMEOUT_HOURS)


def get_allowed_approval_statuses(current_status: str) -> list[str]:
    """Get list of allowed next statuses from current status."""
    try:
        current = EscrowApprovalStatus(current_status)
        return [s.value for s in APPROVAL_ALLOWED_TRANSITIONS.get(current, set())]
    except ValueError:
        return []
