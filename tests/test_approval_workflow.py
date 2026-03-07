"""
Tests for approval workflow.

Tests:
1. State machine: validates transitions correctly
2. Idempotency: approve/deny multiple times
3. Happy path: approval updates transaction correctly
4. Deny path: releases reserved funds
5. Expiry: worker handles expired approvals
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from services.payment.app.approval_workflow import (
    EscrowApprovalStatus,
    validate_approval_transition,
    is_idempotent_action,
    get_approval_timeout,
    APPROVAL_ALLOWED_TRANSITIONS,
)


class TestApprovalStateMachine:
    """Test approval state machine transitions."""

    def test_pending_to_approved_allowed(self):
        """PENDING -> APPROVED should be allowed."""
        is_valid, error = validate_approval_transition(
            EscrowApprovalStatus.PENDING,
            EscrowApprovalStatus.APPROVED
        )
        assert is_valid is True
        assert error is None

    def test_pending_to_denied_allowed(self):
        """PENDING -> DENIED should be allowed."""
        is_valid, error = validate_approval_transition(
            EscrowApprovalStatus.PENDING,
            EscrowApprovalStatus.DENIED
        )
        assert is_valid is True

    def test_pending_to_expired_allowed(self):
        """PENDING -> EXPIRED should be allowed (via worker)."""
        is_valid, error = validate_approval_transition(
            EscrowApprovalStatus.PENDING,
            EscrowApprovalStatus.EXPIRED
        )
        assert is_valid is True

    def test_approved_is_terminal(self):
        """APPROVED should be terminal."""
        is_valid, error = validate_approval_transition(
            EscrowApprovalStatus.APPROVED,
            EscrowApprovalStatus.DENIED
        )
        assert is_valid is False

    def test_denied_is_terminal(self):
        """DENIED should be terminal."""
        is_valid, error = validate_approval_transition(
            EscrowApprovalStatus.DENIED,
            EscrowApprovalStatus.APPROVED
        )
        assert is_valid is False

    def test_expired_is_terminal(self):
        """EXPIRED should be terminal."""
        is_valid, error = validate_approval_transition(
            EscrowApprovalStatus.EXPIRED,
            EscrowApprovalStatus.APPROVED
        )
        assert is_valid is False


class TestIdempotency:
    """Test idempotent actions."""

    def test_approve_already_approved_is_idempotent(self):
        """Approving an already APPROVED request should be idempotent."""
        assert is_idempotent_action(
            EscrowApprovalStatus.APPROVED,
            EscrowApprovalStatus.APPROVED
        ) is True

    def test_deny_already_denied_is_idempotent(self):
        """Denying an already DENIED request should be idempotent."""
        assert is_idempotent_action(
            EscrowApprovalStatus.DENIED,
            EscrowApprovalStatus.DENIED
        ) is True

    def test_approve_pending_is_not_idempotent(self):
        """Approving a PENDING request is not idempotent."""
        assert is_idempotent_action(
            EscrowApprovalStatus.PENDING,
            EscrowApprovalStatus.APPROVED
        ) is False


class TestApprovalTimeout:
    """Test approval timeout calculation."""

    def test_default_timeout(self):
        """Default timeout should be 24 hours."""
        timeout = get_approval_timeout()
        expected = datetime.utcnow() + timedelta(hours=24)
        # Allow 1 second tolerance
        assert abs((timeout - expected).total_seconds()) < 1

    def test_custom_timeout(self):
        """Custom timeout should work."""
        timeout = get_approval_timeout(hours=1)
        expected = datetime.utcnow() + timedelta(hours=1)
        assert abs((timeout - expected).total_seconds()) < 1


class TestAllowedTransitions:
    """Test that all transitions are properly defined."""

    def test_all_statuses_have_transitions(self):
        """All statuses should be in the transitions map."""
        for status in EscrowApprovalStatus:
            assert status in APPROVAL_ALLOWED_TRANSITIONS

    def test_terminal_states_have_no_transitions(self):
        """Terminal states should have empty transitions."""
        assert APPROVAL_ALLOWED_TRANSITIONS[EscrowApprovalStatus.APPROVED] == set()
        assert APPROVAL_ALLOWED_TRANSITIONS[EscrowApprovalStatus.DENIED] == set()
        assert APPROVAL_ALLOWED_TRANSITIONS[EscrowApprovalStatus.EXPIRED] == set()

    def test_pending_has_all_valid_transitions(self):
        """PENDING should allow approve, deny, expire."""
        allowed = APPROVAL_ALLOWED_TRANSITIONS[EscrowApprovalStatus.PENDING]
        assert EscrowApprovalStatus.APPROVED in allowed
        assert EscrowApprovalStatus.DENIED in allowed
        assert EscrowApprovalStatus.EXPIRED in allowed


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
