"""
Tests for task contract: validation, canonicalization, and state machine.

Tests:
1. Schema validation: rejects invalid payloads
2. Canonicalization: stable input_hash across field order changes
3. State machine: rejects illegal transitions
"""

import pytest
from pydantic import ValidationError
from services.registry.app.task_contract import (
    TaskCreateRequest,
    ExecuteParams,
    TaskOutput,
    TaskFailRequest,
    canonicalize_json,
    compute_input_hash,
    validate_state_transition,
    validate_task_status_update,
    get_allowed_statuses,
    TaskStatus,
    PaymentParams,
)


class TestSchemaValidation:
    """Test Pydantic schema validation."""

    def test_task_create_rejects_extra_fields(self):
        """TaskCreateRequest should reject unknown fields."""
        with pytest.raises(ValidationError) as exc_info:
            TaskCreateRequest(
                caller_agent_id="550e8400-e29b-41d4-a716-446655440000",
                callee_agent_id="550e8400-e29b-41d4-a716-446655440001",
                capability="compute",
                input={"data": "test"},
                max_budget=100,
                currency="credits",
                timeout_seconds=300,
                unknown_field="should_reject",
            )
        errors = exc_info.value.errors()
        assert any(e.get("type") == "extra_forbidden" for e in errors)

    def test_task_create_validates_uuid_format(self):
        """TaskCreateRequest should validate UUID format."""
        with pytest.raises(ValidationError) as exc_info:
            TaskCreateRequest(
                caller_agent_id="not-a-uuid",
                callee_agent_id="550e8400-e29b-41d4-a716-446655440001",
                capability="compute",
                input={"data": "test"},
                max_budget=100,
            )
        errors = exc_info.value.errors()
        assert any("pattern" in str(e.get("type", "")) for e in errors)

    def test_task_create_validates_capability_pattern(self):
        """TaskCreateRequest should validate capability name pattern."""
        with pytest.raises(ValidationError):
            TaskCreateRequest(
                caller_agent_id="550e8400-e29b-41d4-a716-446655440000",
                callee_agent_id="550e8400-e29b-41d4-a716-446655440001",
                capability="invalid capability!",
                input={"data": "test"},
                max_budget=100,
            )

    def test_task_create_validates_bounds(self):
        """TaskCreateRequest should validate numeric bounds."""
        with pytest.raises(ValidationError):
            TaskCreateRequest(
                caller_agent_id="550e8400-e29b-41d4-a716-446655440000",
                callee_agent_id="550e8400-e29b-41d4-a716-446655440001",
                capability="compute",
                input={"data": "test"},
                max_budget=2_000_000,
            )

    def test_task_create_rejects_nan_in_input(self):
        """TaskCreateRequest should reject NaN in input."""
        with pytest.raises(ValidationError) as exc_info:
            TaskCreateRequest(
                caller_agent_id="550e8400-e29b-41d4-a716-446655440000",
                callee_agent_id="550e8400-e29b-41d4-a716-446655440001",
                capability="compute",
                input={"value": float('nan')},
                max_budget=100,
            )
        errors = exc_info.value.errors()
        assert any("NaN" in str(e.get("msg", "")) for e in errors)

    def test_task_create_rejects_infinity_in_input(self):
        """TaskCreateRequest should reject Infinity in input."""
        with pytest.raises(ValidationError):
            TaskCreateRequest(
                caller_agent_id="550e8400-e29b-41d4-a716-446655440000",
                callee_agent_id="550e8400-e29b-41d4-a716-446655440001",
                capability="compute",
                input={"value": float('inf')},
                max_budget=100,
            )

    def test_execute_params_rejects_extra_fields(self):
        """ExecuteParams should reject unknown fields."""
        with pytest.raises(ValidationError):
            ExecuteParams(
                capability="compute",
                input={"data": "test"},
                payment={"max_budget": 100, "currency": "credits"},
                unknown_field="reject",
            )

    def test_payment_validates_currency(self):
        """PaymentParams should only allow credits or usdc."""
        with pytest.raises(ValidationError):
            PaymentParams(max_budget=100, currency="bitcoin")

    def test_task_output_strict_mode(self):
        """TaskOutput should reject extra fields."""
        with pytest.raises(ValidationError):
            TaskOutput(result={"ok": True}, unknown_field="reject")

    def test_task_fail_request_validates_message(self):
        """TaskFailRequest should validate error_message."""
        with pytest.raises(ValidationError):
            TaskFailRequest(error_message="")


class TestCanonicalization:
    """Test deterministic JSON canonicalization."""

    def test_same_input_different_key_order(self):
        """Same logical input with different key order should produce same hash."""
        data1 = {"b": 1, "a": 2, "c": 3}
        data2 = {"a": 2, "c": 3, "b": 1}
        assert compute_input_hash(data1) == compute_input_hash(data2)

    def test_nested_object_key_order(self):
        """Nested objects should also be canonicalized."""
        data1 = {"outer": {"z": 1, "a": 2}, "x": 3}
        data2 = {"x": 3, "outer": {"a": 2, "z": 1}}
        assert compute_input_hash(data1) == compute_input_hash(data2)

    def test_list_order_preserved(self):
        """List order should be preserved (not sorted)."""
        data1 = {"items": [1, 2, 3]}
        data2 = {"items": [3, 2, 1]}
        assert compute_input_hash(data1) != compute_input_hash(data2)

    def test_null_vs_missing(self):
        """null vs missing key should produce different hashes."""
        data1 = {"a": None, "b": 1}
        data2 = {"b": 1}
        assert compute_input_hash(data1) != compute_input_hash(data2)

    def test_canonical_form_deterministic(self):
        """Canonical JSON form should be deterministic."""
        data = {"z": 1, "a": {"c": 3, "b": 2}}
        canonical = canonicalize_json(data)
        assert '"a"' in canonical and '"z"' in canonical
        assert canonical.index('"a"') < canonical.index('"z"')

    def test_float_precision_preserved(self):
        """Float precision should be preserved."""
        data1 = {"value": 1.5}
        data2 = {"value": 1.50}
        assert compute_input_hash(data1) == compute_input_hash(data2)


class TestStateMachine:
    """Test task state machine transitions."""

    def test_initiated_to_in_progress_allowed(self):
        """INITIATED -> IN_PROGRESS should be allowed."""
        is_valid, error = validate_state_transition(
            TaskStatus.INITIATED,
            TaskStatus.IN_PROGRESS
        )
        assert is_valid is True

    def test_initiated_to_completed_not_allowed(self):
        """INITIATED -> COMPLETED should not be allowed."""
        is_valid, error = validate_state_transition(
            TaskStatus.INITIATED,
            TaskStatus.COMPLETED
        )
        assert is_valid is False
        assert "Invalid state transition" in error

    def test_in_progress_to_completed_allowed(self):
        """IN_PROGRESS -> COMPLETED should be allowed."""
        is_valid, error = validate_state_transition(
            TaskStatus.IN_PROGRESS,
            TaskStatus.COMPLETED
        )
        assert is_valid is True

    def test_in_progress_to_failed_allowed(self):
        """IN_PROGRESS -> FAILED should be allowed."""
        is_valid, error = validate_state_transition(
            TaskStatus.IN_PROGRESS,
            TaskStatus.FAILED
        )
        assert is_valid is True

    def test_completed_is_terminal(self):
        """COMPLETED should be terminal."""
        is_valid, error = validate_state_transition(
            TaskStatus.COMPLETED,
            TaskStatus.FAILED
        )
        assert is_valid is False

    def test_failed_is_terminal(self):
        """FAILED should be terminal."""
        is_valid, error = validate_state_transition(
            TaskStatus.FAILED,
            TaskStatus.COMPLETED
        )
        assert is_valid is False

    def test_timeout_is_terminal(self):
        """TIMEOUT should be terminal."""
        is_valid, error = validate_state_transition(
            TaskStatus.TIMEOUT,
            TaskStatus.COMPLETED
        )
        assert is_valid is False

    def test_helper_function_with_string_statuses(self):
        """validate_task_status_update should work with string statuses."""
        is_valid, error = validate_task_status_update("initiated", "in_progress")
        assert is_valid is True

        is_valid, error = validate_task_status_update("completed", "failed")
        assert is_valid is False

    def test_get_allowed_statuses(self):
        """get_allowed_statuses should return correct list."""
        allowed = get_allowed_statuses("initiated")
        assert "in_progress" in allowed
        assert "failed" in allowed
        assert "completed" not in allowed

        allowed = get_allowed_statuses("completed")
        assert allowed == []


class TestIntegration:
    """Integration tests for the full contract."""

    def test_valid_task_create_request(self):
        """Valid request should pass all validation."""
        req = TaskCreateRequest(
            caller_agent_id="550e8400-e29b-41d4-a716-446655440000",
            callee_agent_id="550e8400-e29b-41d4-a716-446655440001",
            capability="compute",
            input={"query": "test", "limit": 10},
            max_budget=100,
            currency="credits",
            timeout_seconds=300,
        )
        assert req.capability == "compute"
        assert req.max_budget == 100

    def test_valid_execute_params(self):
        """Valid ExecuteParams should pass validation."""
        params = ExecuteParams(
            capability="compute",
            input={"query": "test"},
            payment=PaymentParams(max_budget=100),
            timeout_seconds=300,
        )
        assert params.capability == "compute"
        assert params.payment.currency == "credits"

    def test_hash_matches_for_nested_same_order(self):
        """Nested dicts created in different order should hash same due to sorting."""
        d1 = {}
        d1["outer"] = {}
        d1["outer"]["z"] = 1
        d1["outer"]["a"] = 2
        d1["x"] = 3

        d2 = {"x": 3, "outer": {"a": 2, "z": 1}}

        assert compute_input_hash(d1) == compute_input_hash(d2)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
