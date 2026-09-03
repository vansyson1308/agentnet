"""Feature flags and limits for the Autonomous Society Runtime.

Everything dangerous defaults to OFF. Values are read from the environment
at call time (``get_settings()``) so tests can override with ``monkeypatch``
and ``reset_settings_cache()``; the worker reads them once at start-up and
again on every loop iteration for the cheap flags (enabled / budgets) so an
operator can pause the society by flipping ``SOCIETY_RUNTIME_ENABLED``.

Production autonomous deploy is not a setting — it is hard-coded OFF in v1
(``production_deploy_enabled`` is a read-only ``False``; setting the env var
logs a warning and is ignored). See docs/SOCIETY_RUNTIME.md.
"""

from __future__ import annotations

import logging
import os
import pathlib
import socket
from dataclasses import dataclass, field, fields
from decimal import Decimal
from functools import lru_cache

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUE


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    try:
        val = int(raw) if raw not in (None, "") else default
    except ValueError:
        logger.warning("society config: %s=%r is not an int; using %s", name, raw, default)
        val = default
    return max(minimum, val)


def _decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name)
    try:
        val = Decimal(raw) if raw not in (None, "") else Decimal(default)
    except Exception:  # noqa: BLE001
        logger.warning("society config: %s=%r is not a number; using %s", name, raw, default)
        val = Decimal(default)
    return max(Decimal("0"), val)


def _detect_repo_root() -> str:
    """Best-effort repo root: walk up from this file until a .git dir is found."""
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return str(parent)
    return str(here.parents[3]) if len(here.parents) > 3 else str(here.parent)


PROMPT_VERSION = "society-v1"

MODEL_PROVIDERS = ("scripted", "openai_compatible", "fake")


@dataclass(frozen=True)
class SocietySettings:
    # ── master switches ────────────────────────────────────────────────
    runtime_enabled: bool = field(default_factory=lambda: _bool("SOCIETY_RUNTIME_ENABLED", False))
    autonomous_code_enabled: bool = field(default_factory=lambda: _bool("SOCIETY_AUTONOMOUS_CODE_ENABLED", False))
    staging_deploy_enabled: bool = field(default_factory=lambda: _bool("SOCIETY_STAGING_DEPLOY_ENABLED", False))
    # Hard OFF in v1. Not configurable.
    production_deploy_enabled: bool = False

    # ── model ──────────────────────────────────────────────────────────
    model_provider: str = field(default_factory=lambda: os.getenv("SOCIETY_MODEL_PROVIDER", "scripted").strip().lower())
    model_name: str = field(
        default_factory=lambda: os.getenv("SOCIETY_MODEL_NAME") or os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")
    )
    model_base_url: str = field(default_factory=lambda: os.getenv("SOCIETY_MODEL_BASE_URL") or os.getenv("LLM_BASE_URL", ""))
    model_api_key: str = field(default_factory=lambda: os.getenv("SOCIETY_MODEL_API_KEY") or os.getenv("LLM_API_KEY", ""))
    model_timeout_seconds: int = field(default_factory=lambda: _int("SOCIETY_MODEL_TIMEOUT_SECONDS", 45, minimum=5))
    model_max_output_tokens: int = field(default_factory=lambda: _int("SOCIETY_MODEL_MAX_OUTPUT_TOKENS", 1200, minimum=100))
    # USD per 1K tokens used for budget accounting when the provider does not
    # return cost. Conservative defaults; override per deployment.
    model_usd_per_1k_input: Decimal = field(default_factory=lambda: _decimal("SOCIETY_MODEL_USD_PER_1K_INPUT", "0.0005"))
    model_usd_per_1k_output: Decimal = field(default_factory=lambda: _decimal("SOCIETY_MODEL_USD_PER_1K_OUTPUT", "0.0015"))
    # Model REQUEST retries (network / 429 / 5xx / timeout) are bounded and
    # distinct from run attempts. Cognition has no side effects, so replaying a
    # request is safe; the only cost is money, which is accounted per request.
    model_request_retries: int = field(default_factory=lambda: _int("SOCIETY_MODEL_REQUEST_RETRIES", 1, minimum=0))
    model_retry_backoff_seconds: int = field(default_factory=lambda: _int("SOCIETY_MODEL_RETRY_BACKOFF_SECONDS", 2, minimum=0))
    # Prefer native JSON-schema structured output when the provider supports
    # it (OpenAI-style ``response_format.json_schema``); falls back to
    # ``json_object`` + strict parsing when off or rejected by the provider.
    model_json_schema: bool = field(default_factory=lambda: _bool("SOCIETY_MODEL_JSON_SCHEMA", False))

    # ── global budgets / loop-storm limits ─────────────────────────────
    max_runs_per_hour: int = field(default_factory=lambda: _int("SOCIETY_MAX_RUNS_PER_HOUR", 120, minimum=1))
    daily_model_budget_usd: Decimal = field(default_factory=lambda: _decimal("SOCIETY_DAILY_MODEL_BUDGET", "2.0"))
    max_causation_depth: int = field(default_factory=lambda: _int("SOCIETY_MAX_CAUSATION_DEPTH", 12, minimum=1))
    max_runs_per_correlation: int = field(default_factory=lambda: _int("SOCIETY_MAX_RUNS_PER_CORRELATION", 40, minimum=1))
    max_intents_per_run: int = field(default_factory=lambda: _int("SOCIETY_MAX_INTENTS_PER_RUN", 5, minimum=1))
    repeat_message_window_seconds: int = field(
        default_factory=lambda: _int("SOCIETY_REPEAT_MESSAGE_WINDOW_SECONDS", 3600, minimum=0)
    )
    event_ttl_seconds: int = field(default_factory=lambda: _int("SOCIETY_EVENT_TTL_SECONDS", 86400, minimum=60))
    max_task_escrow_credits: int = field(default_factory=lambda: _int("SOCIETY_MAX_TASK_ESCROW_CREDITS", 100, minimum=0))

    # ── run lifecycle ──────────────────────────────────────────────────
    run_lease_seconds: int = field(default_factory=lambda: _int("SOCIETY_RUN_LEASE_SECONDS", 120, minimum=5))
    run_max_attempts: int = field(default_factory=lambda: _int("SOCIETY_RUN_MAX_ATTEMPTS", 3, minimum=1))
    retry_backoff_base_seconds: int = field(default_factory=lambda: _int("SOCIETY_RETRY_BACKOFF_BASE_SECONDS", 5, minimum=0))
    circuit_breaker_failures: int = field(default_factory=lambda: _int("SOCIETY_CIRCUIT_BREAKER_FAILURES", 3, minimum=1))
    circuit_breaker_pause_seconds: int = field(
        default_factory=lambda: _int("SOCIETY_CIRCUIT_BREAKER_PAUSE_SECONDS", 900, minimum=1)
    )
    wake_poll_seconds: int = field(default_factory=lambda: _int("SOCIETY_WAKE_POLL_SECONDS", 5, minimum=1))
    heartbeat_interval_seconds: int = field(default_factory=lambda: _int("SOCIETY_HEARTBEAT_INTERVAL_SECONDS", 3600, minimum=0))
    ingest_task_outcomes: bool = field(default_factory=lambda: _bool("SOCIETY_INGEST_TASK_OUTCOMES", True))
    ingest_lookback_seconds: int = field(default_factory=lambda: _int("SOCIETY_INGEST_LOOKBACK_SECONDS", 3600, minimum=60))
    dispatch_batch_size: int = field(default_factory=lambda: _int("SOCIETY_DISPATCH_BATCH_SIZE", 50, minimum=1))

    # ── approvals / ingress ────────────────────────────────────────────
    approval_resume_max_attempts: int = field(default_factory=lambda: _int("SOCIETY_APPROVAL_RESUME_MAX_ATTEMPTS", 3, minimum=1))
    ingress_event_allowlist: tuple = field(
        default_factory=lambda: tuple(
            sorted(
                {
                    "platform.metric.anomaly",
                    "platform.health.degraded",
                    "user.feedback.received",
                    "staging.canary.signal",
                }
                | {e.strip() for e in os.getenv("SOCIETY_INGRESS_EVENT_ALLOWLIST", "").split(",") if e.strip()}
            )
        )
    )
    ingress_max_payload_bytes: int = field(default_factory=lambda: _int("SOCIETY_INGRESS_MAX_PAYLOAD_BYTES", 8192, minimum=256))
    ingress_max_events_per_actor_per_hour: int = field(default_factory=lambda: _int("SOCIETY_INGRESS_MAX_PER_ACTOR_PER_HOUR", 30, minimum=1))
    ingress_max_events_per_hour: int = field(default_factory=lambda: _int("SOCIETY_INGRESS_MAX_PER_HOUR", 120, minimum=1))

    # ── engineering loop ───────────────────────────────────────────────
    repo_root: str = field(default_factory=lambda: os.getenv("SOCIETY_REPO_ROOT") or _detect_repo_root())
    workspace_root: str = field(
        default_factory=lambda: os.getenv("SOCIETY_WORKSPACE_ROOT") or "/tmp/agentnet-society-workspaces"
    )
    qa_test_timeout_seconds: int = field(default_factory=lambda: _int("SOCIETY_QA_TEST_TIMEOUT_SECONDS", 300, minimum=10))
    branch_prefix: str = field(default_factory=lambda: os.getenv("SOCIETY_BRANCH_PREFIX", "agentnet-auto"))

    # ── identity ───────────────────────────────────────────────────────
    worker_id: str = field(
        default_factory=lambda: os.getenv("SOCIETY_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    )
    prompt_version: str = PROMPT_VERSION

    def __post_init__(self) -> None:
        if self.model_provider not in MODEL_PROVIDERS:
            logger.warning(
                "society config: unknown SOCIETY_MODEL_PROVIDER=%r; falling back to 'scripted'", self.model_provider
            )
            object.__setattr__(self, "model_provider", "scripted")
        if _bool("SOCIETY_PRODUCTION_DEPLOY_ENABLED", False):
            logger.warning(
                "SOCIETY_PRODUCTION_DEPLOY_ENABLED is set but production autonomous deploy is hard-disabled in v1; ignoring"
            )

    def public_dict(self) -> dict:
        """Settings safe to expose over the API / logs (no API key)."""
        out = {}
        for f in fields(self):
            if f.name in ("model_api_key",):
                out[f.name] = "***" if getattr(self, f.name) else ""
            else:
                v = getattr(self, f.name)
                out[f.name] = str(v) if isinstance(v, Decimal) else (list(v) if isinstance(v, tuple) else v)
        return out

    def public_flags(self) -> dict:
        """The only settings a public (unauthenticated) surface may show."""
        return {
            "runtime_enabled": self.runtime_enabled,
            "autonomous_code_enabled": self.autonomous_code_enabled,
            "staging_deploy_enabled": self.staging_deploy_enabled,
            "production_deploy_enabled": self.production_deploy_enabled,
            "model_provider": self.model_provider,
        }


@lru_cache(maxsize=1)
def get_settings() -> SocietySettings:
    return SocietySettings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
