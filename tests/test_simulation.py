"""
Tests for simulation service: state machine, schemas, cost calculator.

Tests:
1. SimStatus state machine: valid and invalid transitions
2. Pydantic schemas: validation rules
3. Cost calculator: estimates and edge cases

Money invariant: sim_* tables never modify wallet balances.
"""

import pytest
from pydantic import ValidationError

from services.simulation.app.models import SimStatus, validate_sim_transition
from services.simulation.app.schemas import (
    AgentFilter,
    ChatRequest,
    InjectedAgent,
    SeedConfig,
    SimulationCreate,
    SimulationParams,
    SimulationPreview,
)
from services.simulation.app.services.cost_calculator import (
    estimate_cost,
    estimate_duration_seconds,
)

# ─── State Machine Tests ────────────────────────────────────


class TestSimStatusStateMachine:
    """Test SimStatus transition validation."""

    def test_valid_forward_transitions(self):
        """Happy path: INITIALIZING -> ... -> COMPLETED."""
        assert validate_sim_transition(SimStatus.INITIALIZING, SimStatus.BUILDING_GRAPH)
        assert validate_sim_transition(SimStatus.BUILDING_GRAPH, SimStatus.GENERATING_AGENTS)
        assert validate_sim_transition(SimStatus.GENERATING_AGENTS, SimStatus.RUNNING)
        assert validate_sim_transition(SimStatus.RUNNING, SimStatus.GENERATING_REPORT)
        assert validate_sim_transition(SimStatus.GENERATING_REPORT, SimStatus.COMPLETED)

    def test_any_state_can_fail(self):
        """Any non-terminal state should be able to transition to FAILED."""
        for status in [
            SimStatus.INITIALIZING,
            SimStatus.BUILDING_GRAPH,
            SimStatus.GENERATING_AGENTS,
            SimStatus.RUNNING,
            SimStatus.GENERATING_REPORT,
        ]:
            assert validate_sim_transition(
                status, SimStatus.FAILED
            ), f"{status.value} should be able to transition to FAILED"

    def test_any_state_can_be_cancelled(self):
        """Non-terminal states should support cancellation."""
        for status in [
            SimStatus.INITIALIZING,
            SimStatus.BUILDING_GRAPH,
            SimStatus.GENERATING_AGENTS,
            SimStatus.RUNNING,
        ]:
            assert validate_sim_transition(
                status, SimStatus.CANCELLED
            ), f"{status.value} should be able to transition to CANCELLED"

    def test_invalid_backward_transitions(self):
        """Cannot go backward in the pipeline."""
        assert not validate_sim_transition(SimStatus.RUNNING, SimStatus.BUILDING_GRAPH)
        assert not validate_sim_transition(SimStatus.COMPLETED, SimStatus.RUNNING)
        assert not validate_sim_transition(SimStatus.GENERATING_REPORT, SimStatus.GENERATING_AGENTS)

    def test_terminal_states_are_final(self):
        """COMPLETED, FAILED, CANCELLED, TIMEOUT cannot transition."""
        for terminal in [
            SimStatus.COMPLETED,
            SimStatus.FAILED,
            SimStatus.CANCELLED,
            SimStatus.TIMEOUT,
        ]:
            for target in SimStatus:
                if target != terminal:
                    assert not validate_sim_transition(
                        terminal, target
                    ), f"Terminal state {terminal.value} should not transition to {target.value}"

    def test_self_transition_invalid(self):
        """A state cannot transition to itself."""
        for status in SimStatus:
            assert not validate_sim_transition(status, status), f"{status.value} should not transition to itself"

    def test_skip_stages_invalid(self):
        """Cannot skip pipeline stages."""
        assert not validate_sim_transition(SimStatus.INITIALIZING, SimStatus.RUNNING)
        assert not validate_sim_transition(SimStatus.BUILDING_GRAPH, SimStatus.COMPLETED)
        assert not validate_sim_transition(SimStatus.INITIALIZING, SimStatus.GENERATING_REPORT)


# ─── Schema Tests ────────────────────────────────────────────


class TestSimulationSchemas:
    """Test Pydantic schema validation."""

    def test_agent_filter_defaults(self):
        """AgentFilter should have reasonable defaults."""
        f = AgentFilter()
        assert f.capabilities is None
        assert f.min_reputation_tier is None
        assert f.limit == 50

    def test_seed_config_defaults(self):
        """SeedConfig should default to an empty filter."""
        sc = SeedConfig()
        assert sc.agent_filter is not None
        assert sc.include_interactions is True
        assert sc.include_task_history is True

    def test_simulation_params_defaults(self):
        """SimulationParams should have sensible defaults."""
        sp = SimulationParams()
        assert sp.platform == "twitter"
        assert sp.num_steps == 100
        assert sp.num_steps >= 10

    def test_simulation_params_platform_validation(self):
        """SimulationParams should reject invalid platforms."""
        with pytest.raises(ValidationError):
            SimulationParams(platform="invalid_platform")

    def test_simulation_params_valid_platforms(self):
        """All valid platforms should be accepted."""
        for platform in ["twitter", "reddit"]:
            sp = SimulationParams(platform=platform)
            assert sp.platform == platform

    def test_simulation_create_minimal(self):
        """SimulationCreate should work with minimal fields."""
        sc = SimulationCreate(name="Test Sim")
        assert sc.name == "Test Sim"
        assert sc.seed_config is not None
        assert sc.simulation_config is not None

    def test_injected_agent_validation(self):
        """InjectedAgent requires name."""
        ia = InjectedAgent(name="Evil Bot", description="disruptor agent")
        assert ia.name == "Evil Bot"
        assert ia.personality_traits is None

    def test_chat_request_validation(self):
        """ChatRequest requires agent_index and message."""
        cr = ChatRequest(agent_index=0, message="Hello")
        assert cr.agent_index == 0
        assert cr.message == "Hello"

    def test_chat_request_negative_index(self):
        """ChatRequest should reject negative agent_index."""
        with pytest.raises(ValidationError):
            ChatRequest(agent_index=-1, message="Hello")

    def test_simulation_preview_minimal(self):
        """SimulationPreview should work with defaults."""
        sp = SimulationPreview()
        assert sp.seed_config is not None
        assert sp.simulation_config is not None


# ─── Cost Calculator Tests ───────────────────────────────────


class TestCostCalculator:
    """Test simulation cost estimation."""

    def test_basic_cost(self):
        """Basic cost should be positive."""
        seed_config = SeedConfig()
        sim_config = SimulationParams()
        cost = estimate_cost(seed_config, sim_config, num_seed_agents=10)
        assert cost > 0

    def test_cost_increases_with_steps(self):
        """More steps should cost more."""
        seed_config = SeedConfig()
        cost_10 = estimate_cost(seed_config, SimulationParams(num_steps=10), num_seed_agents=10)
        cost_50 = estimate_cost(seed_config, SimulationParams(num_steps=50), num_seed_agents=10)
        assert cost_50 > cost_10

    def test_cost_increases_with_agents(self):
        """More agents should cost more."""
        seed_config = SeedConfig()
        sim_config = SimulationParams()
        cost_5 = estimate_cost(seed_config, sim_config, num_seed_agents=5)
        cost_50 = estimate_cost(seed_config, sim_config, num_seed_agents=50)
        assert cost_50 > cost_5

    def test_cost_with_injected_agents(self):
        """Injected agents should increase cost."""
        seed_config = SeedConfig()
        sim_config_no_inject = SimulationParams()
        sim_config_with_inject = SimulationParams(
            injected_agents=[
                InjectedAgent(name="Bot1", description="tester"),
                InjectedAgent(name="Bot2", description="tester"),
            ]
        )
        cost_no = estimate_cost(seed_config, sim_config_no_inject, num_seed_agents=10)
        cost_yes = estimate_cost(seed_config, sim_config_with_inject, num_seed_agents=10)
        assert cost_yes > cost_no

    def test_cost_zero_agents(self):
        """Zero agents should still have base cost."""
        seed_config = SeedConfig()
        sim_config = SimulationParams()
        cost = estimate_cost(seed_config, sim_config, num_seed_agents=0)
        assert cost >= 0

    def test_duration_estimate(self):
        """Duration estimate should be positive."""
        duration = estimate_duration_seconds(num_agents=10, num_steps=20)
        assert duration > 0

    def test_duration_increases_with_scale(self):
        """More agents and steps should take longer."""
        d_small = estimate_duration_seconds(num_agents=5, num_steps=10)
        d_large = estimate_duration_seconds(num_agents=50, num_steps=100)
        assert d_large > d_small
