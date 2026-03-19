"""
Simulation service configuration.

Loads LLM, Zep, and simulation settings from environment variables.
"""

import os

from dotenv import load_dotenv

load_dotenv()


class SimulationConfig:
    """Configuration for the simulation service."""

    # LLM API (OpenAI SDK format — works with any compatible provider)
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "gpt-4")

    # Zep Cloud (knowledge graph memory)
    ZEP_API_KEY = os.getenv("ZEP_API_KEY", "")

    # Simulation defaults
    DEFAULT_MAX_STEPS = int(os.getenv("SIM_DEFAULT_MAX_STEPS", "100"))
    DEFAULT_PLATFORM = os.getenv("SIM_DEFAULT_PLATFORM", "twitter")
    SIMULATION_TIMEOUT_SECONDS = int(os.getenv("SIMULATION_TIMEOUT_SECONDS", "600"))

    # Cost per simulation step (in credits)
    COST_PER_STEP = int(os.getenv("SIM_COST_PER_STEP", "5"))
    COST_BASE = int(os.getenv("SIM_COST_BASE", "50"))

    # Report Agent
    REPORT_MAX_TOOL_CALLS = int(os.getenv("REPORT_AGENT_MAX_TOOL_CALLS", "5"))
    REPORT_MAX_REFLECTION_ROUNDS = int(os.getenv("REPORT_AGENT_MAX_REFLECTION_ROUNDS", "2"))
    REPORT_TEMPERATURE = float(os.getenv("REPORT_AGENT_TEMPERATURE", "0.5"))

    # Registry / Payment service URLs (for escrow integration)
    REGISTRY_URL = os.getenv("REGISTRY_URL", "http://registry:8000")
    PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8001")

    @classmethod
    def is_llm_configured(cls) -> bool:
        """Check if LLM API is configured."""
        return bool(cls.LLM_API_KEY)

    @classmethod
    def is_zep_configured(cls) -> bool:
        """Check if Zep is configured."""
        return bool(cls.ZEP_API_KEY)

    @classmethod
    def validate(cls) -> list:
        """Validate required configuration. Returns list of errors."""
        errors = []
        if not cls.LLM_API_KEY:
            errors.append("LLM_API_KEY not configured")
        if not cls.ZEP_API_KEY:
            errors.append("ZEP_API_KEY not configured (knowledge graph disabled)")
        return errors
