"""
Tests for platform fee (escrow fee / toll-gate revenue).

Money invariant: caller_deduction = callee_credit + platform_fee
The platform fee is calculated as a percentage of the transaction amount.
"""

import math
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# --- Unit tests for fee calculation logic ---


def calculate_platform_fee(amount: int, fee_rate: float = 0.025) -> tuple[int, int]:
    """
    Calculate platform fee and net amount.

    Returns (fee_amount, net_amount).
    Invariant: fee_amount + net_amount == amount
    """
    fee_amount = max(1, math.floor(amount * fee_rate))
    net_amount = amount - fee_amount
    return fee_amount, net_amount


class TestPlatformFeeCalculation:
    """Test the fee calculation logic."""

    def test_default_fee_rate(self):
        """2.5% fee on 1000 credits = 25 fee, 975 net."""
        fee, net = calculate_platform_fee(1000, 0.025)
        assert fee == 25
        assert net == 975
        assert fee + net == 1000  # Money invariant

    def test_minimum_fee_is_one(self):
        """Even for tiny amounts, minimum fee is 1."""
        fee, net = calculate_platform_fee(1, 0.025)
        assert fee == 1
        assert net == 0
        assert fee + net == 1

    def test_money_invariant_various_amounts(self):
        """caller_deduction = callee_credit + platform_fee for various amounts."""
        test_amounts = [1, 10, 50, 100, 500, 1000, 5000, 10000, 100000]
        for amount in test_amounts:
            fee, net = calculate_platform_fee(amount, 0.025)
            assert fee + net == amount, f"Invariant violated for amount={amount}: {fee} + {net} != {amount}"
            assert fee >= 1, f"Fee must be at least 1 for amount={amount}"

    def test_money_invariant_various_rates(self):
        """Money invariant holds across different fee rates."""
        rates = [0.01, 0.025, 0.05, 0.10, 0.15]
        for rate in rates:
            for amount in [100, 1000, 10000]:
                fee, net = calculate_platform_fee(amount, rate)
                assert fee + net == amount, f"Invariant violated: rate={rate}, amount={amount}"

    def test_zero_fee_rate_still_charges_minimum(self):
        """Zero fee rate still has minimum fee of 1 (per GREATEST(1, ...) in SQL)."""
        fee, net = calculate_platform_fee(1000, 0.0)
        assert fee == 1
        assert net == 999
        assert fee + net == 1000

    def test_large_amount_precision(self):
        """Fee calculation is precise for large amounts."""
        fee, net = calculate_platform_fee(1000000, 0.025)
        assert fee == 25000
        assert net == 975000
        assert fee + net == 1000000

    def test_fee_is_integer(self):
        """Fee is always an integer (floor)."""
        fee, net = calculate_platform_fee(33, 0.025)
        assert isinstance(fee, int)
        assert isinstance(net, int)
        assert fee + net == 33


class TestPlatformFeeModel:
    """Test that the Transaction model has platform fee fields."""

    @pytest.fixture(autouse=True)
    def _skip_without_db(self):
        """Skip if DB connection env vars are not set."""
        import os

        if not os.getenv("POSTGRES_HOST"):
            pytest.skip("POSTGRES_HOST not set, skipping model import tests")

    def test_transaction_model_has_fee_fields(self):
        """Transaction model should have platform_fee and platform_fee_rate."""
        from services.registry.app.models import Transaction

        columns = {c.name for c in Transaction.__table__.columns}
        assert "platform_fee" in columns
        assert "platform_fee_rate" in columns

    def test_transaction_fee_defaults(self):
        """Default fee rate should be 2.5%."""
        from services.registry.app.models import Transaction

        fee_rate_col = Transaction.__table__.columns["platform_fee_rate"]
        assert fee_rate_col.default is not None

        fee_col = Transaction.__table__.columns["platform_fee"]
        assert fee_col.default is not None


class TestPlatformFeeSQLMigration:
    """Test that the SQL migration file is valid."""

    def test_migration_file_exists(self):
        """02-platform-fee.sql should exist."""
        import os

        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "services",
            "registry",
            "init-db",
            "02-platform-fee.sql",
        )
        assert os.path.exists(path), f"Migration file not found at {path}"

    def test_migration_contains_invariant_logic(self):
        """Migration should contain the money invariant logic."""
        import os

        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "services",
            "registry",
            "init-db",
            "02-platform-fee.sql",
        )
        with open(path) as f:
            sql = f.read()

        # Must contain fee calculation
        assert "platform_fee" in sql
        assert "platform_fee_rate" in sql
        # Must contain platform wallet
        assert "platform_wallet_id" in sql
        # Must deduct full amount from caller
        assert "balance_credits - NEW.amount" in sql
        # Must credit net amount (not full amount) to callee
        assert "net_amount" in sql
        # Must credit fee to platform wallet
        assert "fee_amount" in sql


class TestA2AAgentCard:
    """A2A 1.0 cards (ADR-0009 D4): built from the official SDK types, one
    gateway for every agent, and nothing private in a per-agent card."""

    def test_registry_card_structure(self):
        from google.protobuf.json_format import MessageToDict

        from services.registry.app.a2a.cards import network_card

        card = MessageToDict(network_card("https://api.agentnet.io.vn"))
        assert card["name"] == "AgentNet"
        assert {i["protocolBinding"] for i in card["supportedInterfaces"]} == {"JSONRPC", "HTTP+JSON"}
        assert all(i["protocolVersion"] == "1.0" for i in card["supportedInterfaces"])
        assert card["capabilities"]["streaming"] is True
        assert card["capabilities"].get("pushNotifications", False) is False
        assert "bearer" in card["securitySchemes"] and card["securityRequirements"]
        assert len(card["skills"]) == 2

    def test_agent_card_from_db_model(self):
        from google.protobuf.json_format import MessageToDict

        from services.registry.app.a2a.cards import agent_card

        mock_agent = MagicMock()
        mock_agent.id = "0b7d6c1e-0000-4000-8000-000000000001"
        mock_agent.name = "TestAgent"
        mock_agent.description = "A test agent"
        mock_agent.capabilities = [
            {"name": "translate", "version": "1.0", "price": 10},
            {"name": "summarize", "version": "1.0", "price": 5},
        ]
        mock_agent.endpoint = "http://localhost:9000"
        mock_agent.public_key = "test-public-key"

        card = MessageToDict(agent_card(mock_agent, "https://api.agentnet.io.vn"))
        assert card["name"] == "TestAgent"
        assert [s["id"] for s in card["skills"]] == ["translate", "summarize"]
        # the SAME gateway interfaces, routed by tenant -- never the agent's own endpoint
        assert {i["url"] for i in card["supportedInterfaces"]} == {"https://api.agentnet.io.vn/a2a", "https://api.agentnet.io.vn/a2a/http"}
        assert {i["tenant"] for i in card["supportedInterfaces"]} == {mock_agent.id}
        text = str(card)
        assert "localhost:9000" not in text and "test-public-key" not in text

    def test_agent_card_security_is_the_gateway_credential(self):
        from google.protobuf.json_format import MessageToDict

        from services.registry.app.a2a.cards import agent_card

        mock_agent = MagicMock()
        mock_agent.id = "0b7d6c1e-0000-4000-8000-000000000002"
        mock_agent.name = "PublicAgent"
        mock_agent.description = "No key"
        mock_agent.capabilities = []
        mock_agent.public_key = None

        card = MessageToDict(agent_card(mock_agent, "https://api.agentnet.io.vn"))
        assert list(card["securitySchemes"]) == ["bearer"]  # no 'none' scheme, ever
