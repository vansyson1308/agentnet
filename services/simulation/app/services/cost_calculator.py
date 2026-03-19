"""
Cost calculator for simulations.

Estimates the credit cost of a simulation based on:
- Number of seed agents
- Number of simulation steps
- Platform complexity (Reddit has more action types)
- Injected agents
"""

from ..config import SimulationConfig
from ..schemas import SeedConfig, SimulationParams


def estimate_cost(
    seed_config: SeedConfig,
    simulation_config: SimulationParams,
    num_seed_agents: int,
) -> int:
    """
    Estimate the cost of a simulation in credits.

    Formula: base_cost + (steps * cost_per_step) + (injected_agents * 10)
    Platform multiplier: Reddit = 1.2x (more action types = more LLM calls)
    """
    base = SimulationConfig.COST_BASE
    step_cost = simulation_config.num_steps * SimulationConfig.COST_PER_STEP

    # Platform multiplier
    platform_multiplier = 1.2 if simulation_config.platform == "reddit" else 1.0

    # Injected agents cost extra
    injected_cost = 0
    if simulation_config.injected_agents:
        injected_cost = len(simulation_config.injected_agents) * 10

    # Agent count multiplier (more agents = more LLM calls per step)
    agent_multiplier = max(1.0, num_seed_agents / 20.0)

    total = int((base + step_cost + injected_cost) * platform_multiplier * agent_multiplier)
    return total


def estimate_duration_seconds(
    num_steps: int,
    num_agents: int,
) -> int:
    """
    Rough estimate of simulation duration in seconds.

    Based on ~0.5-2 seconds per agent per step for LLM calls.
    """
    seconds_per_step = max(1, num_agents * 0.5)
    return int(num_steps * seconds_per_step)
