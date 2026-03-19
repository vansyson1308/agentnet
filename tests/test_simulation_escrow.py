"""
Tests for simulation escrow invariants.

Verifies that:
1. Escrow client calls go through task session API (never direct wallet modification)
2. Simulation models never contain wallet/balance fields
3. State transitions respect terminal state rules

Money invariant: wallet_balance + reserved = constant throughout simulation lifecycle.
The simulation service achieves this by delegating ALL escrow operations to the
registry/payment services via HTTP calls.
"""

from services.simulation.app.models import (
    SimAgentProfile,
    SimChatMessage,
    SimReport,
    SimResult,
    SimSession,
    SimStatus,
    validate_sim_transition,
)


class TestEscrowInvariants:
    """Verify simulation service never touches wallet state."""

    def test_sim_session_has_no_wallet_fields(self):
        """SimSession should not have any wallet/balance columns."""
        column_names = {c.name for c in SimSession.__table__.columns}
        wallet_fields = {"balance", "wallet_balance", "reserved_credits", "reserved_usdc"}
        assert (
            not column_names & wallet_fields
        ), f"SimSession must not have wallet fields: {column_names & wallet_fields}"

    def test_sim_session_has_cost_field(self):
        """SimSession should track cost_credits for escrow reference."""
        column_names = {c.name for c in SimSession.__table__.columns}
        assert "cost_credits" in column_names

    def test_sim_result_has_no_wallet_fields(self):
        """SimResult should not contain any payment data."""
        column_names = {c.name for c in SimResult.__table__.columns}
        payment_fields = {"amount", "balance", "payment", "wallet", "escrow"}
        assert (
            not column_names & payment_fields
        ), f"SimResult must not have payment fields: {column_names & payment_fields}"

    def test_sim_report_has_no_wallet_fields(self):
        """SimReport should not contain payment data."""
        column_names = {c.name for c in SimReport.__table__.columns}
        payment_fields = {"amount", "balance", "payment", "wallet", "escrow"}
        assert not column_names & payment_fields

    def test_sim_agent_profile_has_no_wallet_fields(self):
        """SimAgentProfile should not reference wallets."""
        column_names = {c.name for c in SimAgentProfile.__table__.columns}
        wallet_fields = {"balance", "wallet", "credits", "usdc"}
        assert not column_names & wallet_fields

    def test_sim_chat_message_has_no_wallet_fields(self):
        """SimChatMessage should not reference wallets."""
        column_names = {c.name for c in SimChatMessage.__table__.columns}
        wallet_fields = {"balance", "wallet", "credits", "usdc", "payment"}
        assert not column_names & wallet_fields


class TestEscrowStateTransitions:
    """Test that escrow-relevant state transitions are correct."""

    def test_failed_is_terminal(self):
        """FAILED is terminal — escrow refund should be triggered externally."""
        for target in SimStatus:
            if target != SimStatus.FAILED:
                assert not validate_sim_transition(SimStatus.FAILED, target)

    def test_completed_is_terminal(self):
        """COMPLETED is terminal — escrow release should be triggered externally."""
        for target in SimStatus:
            if target != SimStatus.COMPLETED:
                assert not validate_sim_transition(SimStatus.COMPLETED, target)

    def test_cancelled_is_terminal(self):
        """CANCELLED is terminal — escrow refund should be triggered externally."""
        for target in SimStatus:
            if target != SimStatus.CANCELLED:
                assert not validate_sim_transition(SimStatus.CANCELLED, target)

    def test_timeout_is_terminal(self):
        """TIMEOUT is terminal — handled by worker's refund loop."""
        for target in SimStatus:
            if target != SimStatus.TIMEOUT:
                assert not validate_sim_transition(SimStatus.TIMEOUT, target)

    def test_running_can_timeout(self):
        """RUNNING should be able to transition to TIMEOUT."""
        assert validate_sim_transition(SimStatus.RUNNING, SimStatus.TIMEOUT)


class TestEscrowClientDesign:
    """Verify escrow_client module design constraints."""

    def test_escrow_client_uses_httpx(self):
        """Escrow client should use httpx for external calls, not direct DB."""
        import inspect

        from services.simulation.app.services import escrow_client

        source = inspect.getsource(escrow_client)

        # Should use httpx for HTTP calls
        assert "httpx" in source, "escrow_client must use httpx for API calls"

        # Should NOT import wallet models
        assert "Wallet" not in source, "escrow_client must not import Wallet model"

        # Should NOT import Transaction model
        assert "Transaction" not in source, "escrow_client must not import Transaction"

    def test_escrow_client_functions_are_async(self):
        """All escrow operations should be async."""
        import asyncio

        from services.simulation.app.services.escrow_client import (
            lock_escrow,
            refund_escrow,
            release_escrow,
        )

        assert asyncio.iscoroutinefunction(lock_escrow)
        assert asyncio.iscoroutinefunction(release_escrow)
        assert asyncio.iscoroutinefunction(refund_escrow)
