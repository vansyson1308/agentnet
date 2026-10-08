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


class SocietyConfigError(RuntimeError):
    """Raised at startup for a configuration that must never run."""


def _strict() -> bool:
    """Outside development a malformed or out-of-range value is an error, not a
    silently clamped default (fail fast, never run with limits you did not set)."""
    return os.getenv("ENVIRONMENT", "development").strip().lower() != "development"


def _int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    try:
        val = int(raw) if raw not in (None, "") else default
    except ValueError:
        if _strict():
            raise SocietyConfigError(f"{name}={raw!r} is not an integer")
        logger.warning("society config: %s=%r is not an int; using %s", name, raw, default)
        val = default
    if val < minimum:
        if _strict():
            raise SocietyConfigError(f"{name}={val} is below the minimum {minimum}")
        logger.warning("society config: %s=%s is below the minimum %s; clamped", name, val, minimum)
    return max(minimum, val)


def _decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name)
    try:
        val = Decimal(raw) if raw not in (None, "") else Decimal(default)
    except Exception:  # noqa: BLE001
        if _strict():
            raise SocietyConfigError(f"{name}={raw!r} is not a number")
        logger.warning("society config: %s=%r is not a number; using %s", name, raw, default)
        val = Decimal(default)
    if val < 0:
        if _strict():
            raise SocietyConfigError(f"{name}={val} must not be negative")
        logger.warning("society config: %s=%s is negative; clamped to 0", name, val)
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
OUTPUT_FORMATS = ("auto", "json_object", "json_schema")
# Provider request-capability profile (Phase 4.1, ADR-0007). It decides which
# provider-specific request fields the OpenAI-compatible adapter may send:
#   generic  — plain OpenAI-compatible: never a ``thinking`` field;
#              ``reasoning_effort`` only when explicitly configured;
#   deepseek — DeepSeek's documented thinking toggle
#              (``thinking={"type": "enabled"|"disabled"}``) plus
#              ``reasoning_effort`` (none|low|high|max).
CAPABILITY_PROFILES = ("generic", "deepseek")
# ``auto`` never sends a thinking field (the provider's default applies).
THINKING_MODES = ("auto", "disabled", "enabled")
# ``auto`` never sends ``reasoning_effort``; ``none`` disables reasoning
# where the provider supports that spelling (DeepSeek documents it).
REASONING_EFFORTS = ("auto", "none", "low", "medium", "high", "max")
DEEPSEEK_REASONING_EFFORTS = ("auto", "none", "low", "high", "max")
MODEL_TIERS = ("fast", "strong")
PROMOTION_PROVIDERS = ("disabled", "fake", "github")
GITHUB_CREDENTIAL_PROVIDERS = ("disabled", "static", "app")
DEPLOYMENT_PROVIDERS = ("disabled", "fake")


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
    model_max_output_tokens: int = field(default_factory=lambda: _int("SOCIETY_MODEL_MAX_OUTPUT_TOKENS", 4000, minimum=100))
    # 1200 was the original default and it is NOT enough: SUBMIT_CODE_CANDIDATE
    # carries whole file contents in edits[].content, so a live Builder's JSON was
    # cut off mid-object (finish_reason=length) and the run went DEAD. This is
    # response CAPACITY, not permission -- the daily USD cap, the per-correlation
    # run cap and the typed truncation failure are all unchanged.
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
    # Output-format negotiation (Phase 3): auto probes json_schema once and
    # falls back to json_object WITHOUT spending the error-retry budget;
    # json_object is what DeepSeek documents; json_schema forces the
    # OpenAI-style structured output. The legacy bool above maps to json_schema.
    model_output_format: str = field(
        default_factory=lambda: (os.getenv("SOCIETY_MODEL_OUTPUT_FORMAT") or ("json_schema" if _bool("SOCIETY_MODEL_JSON_SCHEMA", False) else "auto")).strip().lower()
    )
    model_empty_content_retries: int = field(default_factory=lambda: _int("SOCIETY_MODEL_EMPTY_CONTENT_RETRIES", 1, minimum=0))
    # Provider request-capability profile + explicit reasoning policy
    # (Phase 4.1, ADR-0007). Provider-neutral names; the adapter's capability
    # layer maps them onto the wire fields the configured profile documents.
    # Invalid values fail fast (SocietyConfigError), they are never coerced.
    model_capability_profile: str = field(default_factory=lambda: os.getenv("SOCIETY_MODEL_CAPABILITY_PROFILE", "generic").strip().lower())
    model_thinking_mode: str = field(default_factory=lambda: os.getenv("SOCIETY_MODEL_THINKING_MODE", "auto").strip().lower())
    model_reasoning_effort: str = field(default_factory=lambda: os.getenv("SOCIETY_MODEL_REASONING_EFFORT", "auto").strip().lower())
    # Model router: logical tiers -> provider model names (never hard-coded in
    # business logic). Empty strong name = single-tier deployment.
    model_fast_name: str = field(default_factory=lambda: os.getenv("SOCIETY_MODEL_FAST_NAME") or os.getenv("SOCIETY_MODEL_NAME") or os.getenv("LLM_MODEL_NAME", "gpt-4o-mini"))
    model_strong_name: str = field(default_factory=lambda: os.getenv("SOCIETY_MODEL_STRONG_NAME", ""))
    max_correlation_cost_usd: Decimal = field(default_factory=lambda: _decimal("SOCIETY_MAX_CORRELATION_COST_USD", "0.50"))
    max_experiment_cost_usd: Decimal = field(default_factory=lambda: _decimal("SOCIETY_MAX_EXPERIMENT_COST_USD", "0.25"))
    max_promotion_cost_usd: Decimal = field(default_factory=lambda: _decimal("SOCIETY_MAX_PROMOTION_COST_USD", "0.25"))

    # ── global budgets / loop-storm limits ─────────────────────────────
    max_runs_per_hour: int = field(default_factory=lambda: _int("SOCIETY_MAX_RUNS_PER_HOUR", 120, minimum=1))
    daily_model_budget_usd: Decimal = field(default_factory=lambda: _decimal("SOCIETY_DAILY_MODEL_BUDGET", "2.0"))
    max_causation_depth: int = field(default_factory=lambda: _int("SOCIETY_MAX_CAUSATION_DEPTH", 24, minimum=1))
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
    # ── repository intelligence + iterative engineering bounds (fail closed) ──
    max_engineering_turns: int = field(default_factory=lambda: _int("SOCIETY_MAX_ENGINEERING_TURNS", 6, minimum=0))
    max_correlation_engineering_turns: int = field(default_factory=lambda: _int("SOCIETY_MAX_CORRELATION_ENGINEERING_TURNS", 12, minimum=0))
    max_repo_reads_per_run: int = field(default_factory=lambda: _int("SOCIETY_MAX_REPO_READS_PER_RUN", 5, minimum=0))
    max_repo_reads_per_correlation: int = field(default_factory=lambda: _int("SOCIETY_MAX_REPO_READS_PER_CORRELATION", 40, minimum=0))
    max_repo_bytes_per_run: int = field(default_factory=lambda: _int("SOCIETY_MAX_REPO_BYTES_PER_RUN", 120_000, minimum=0))
    max_search_results: int = field(default_factory=lambda: _int("SOCIETY_MAX_SEARCH_RESULTS", 40, minimum=1))
    max_engineering_correlation_depth: int = field(default_factory=lambda: _int("SOCIETY_MAX_ENGINEERING_CORRELATION_DEPTH", 20, minimum=1))
    # ── change budget (engineering rate limits, independent of model spend) ──
    max_autonomous_candidates_per_day: int = field(default_factory=lambda: _int("SOCIETY_MAX_AUTONOMOUS_CANDIDATES_PER_DAY", 10, minimum=0))
    max_promotions_per_day: int = field(default_factory=lambda: _int("SOCIETY_MAX_PROMOTIONS_PER_DAY", 10, minimum=0))
    max_open_autonomous_prs: int = field(default_factory=lambda: _int("SOCIETY_MAX_OPEN_AUTONOMOUS_PRS", 3, minimum=0))
    # Autonomous MERGES are budgeted separately from promotions: opening a PR is
    # reversible, landing one on main is not. Starts at one per day. Agents
    # cannot raise it -- MODIFY_BUDGET is a forbidden HIGH intent with no
    # executor, and nothing in the runtime writes this value.
    max_autonomous_merges_per_day: int = field(default_factory=lambda: _int("SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY", 1, minimum=0))
    max_red_candidates_per_day: int = field(default_factory=lambda: _int("SOCIETY_MAX_RED_CANDIDATES_PER_DAY", 2, minimum=0))
    max_files_per_candidate: int = field(default_factory=lambda: _int("SOCIETY_MAX_FILES_PER_CANDIDATE", 8, minimum=1))
    max_diff_lines: int = field(default_factory=lambda: _int("SOCIETY_MAX_DIFF_LINES", 600, minimum=1))
    # ── promotion / evaluation / deployment providers ──
    promotion_provider: str = field(default_factory=lambda: os.getenv("SOCIETY_PROMOTION_PROVIDER", "disabled").strip().lower())
    auto_merge_enabled: bool = field(default_factory=lambda: _bool("SOCIETY_AUTO_MERGE_ENABLED", False))
    github_repository: str = field(default_factory=lambda: os.getenv("SOCIETY_GITHUB_REPOSITORY", ""))
    github_base_branch: str = field(default_factory=lambda: os.getenv("SOCIETY_GITHUB_BASE_BRANCH", "main"))
    github_api_url: str = field(default_factory=lambda: os.getenv("SOCIETY_GITHUB_API_URL", "https://api.github.com"))
    # ── GitHub credential boundary (Promotion Controller process ONLY; see
    # society/github_credentials.py). Settings hold identifiers and a file
    # PATH — never key material or a token. ``disabled`` = inert.
    github_credential_provider: str = field(default_factory=lambda: os.getenv("SOCIETY_GITHUB_CREDENTIAL_PROVIDER", "disabled").strip().lower())
    github_app_id: str = field(default_factory=lambda: os.getenv("SOCIETY_GITHUB_APP_ID", "").strip())
    github_installation_id: str = field(default_factory=lambda: os.getenv("SOCIETY_GITHUB_INSTALLATION_ID", "").strip())
    github_app_private_key_file: str = field(default_factory=lambda: os.getenv("SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE", "").strip())
    github_token_refresh_margin_seconds: int = field(default_factory=lambda: _int("SOCIETY_GITHUB_TOKEN_REFRESH_MARGIN_SECONDS", 300, minimum=30))
    deployment_provider: str = field(default_factory=lambda: os.getenv("SOCIETY_DEPLOYMENT_PROVIDER", "disabled").strip().lower())
    fitness_test_timeout_seconds: int = field(default_factory=lambda: _int("SOCIETY_FITNESS_TEST_TIMEOUT_SECONDS", 300, minimum=10))
    promotion_lease_seconds: int = field(default_factory=lambda: _int("SOCIETY_PROMOTION_LEASE_SECONDS", 300, minimum=10))
    promotion_max_attempts: int = field(default_factory=lambda: _int("SOCIETY_PROMOTION_MAX_ATTEMPTS", 5, minimum=1))
    # A promotion waiting on the outside world (open PR, CI, human approval) is
    # re-polled at most once per interval; without it the controller would poll
    # the provider on every worker cycle.
    promotion_poll_interval_seconds: int = field(default_factory=lambda: _int("SOCIETY_PROMOTION_POLL_INTERVAL_SECONDS", 60, minimum=0))

    # ── A2A federation as a client (Phase 8, ADR-0009 D14) ──
    # The model can never make the platform fetch an arbitrary URL: discovery is
    # limited to hosts an operator listed here (empty = no Society discovery).
    a2a_discovery_allowed_hosts: tuple = field(
        default_factory=lambda: tuple(sorted({h.strip().lower() for h in os.getenv("A2A_SOCIETY_DISCOVERY_ALLOWED_HOSTS", "").split(",") if h.strip()}))
    )
    a2a_max_calls_per_day: int = field(default_factory=lambda: _int("A2A_SOCIETY_MAX_CALLS_PER_DAY", 20, minimum=0))
    # agent-chain breaker: outbound A2A calls one correlation may cause
    a2a_max_calls_per_correlation: int = field(default_factory=lambda: _int("A2A_SOCIETY_MAX_CALLS_PER_CORRELATION", 3, minimum=0))
    # circuit breaker: failed calls to one remote agent in the last hour
    a2a_breaker_failures: int = field(default_factory=lambda: _int("A2A_SOCIETY_BREAKER_FAILURES", 3, minimum=1))
    # ── autonomous company mode (Phase 8, ADR-0009 D15) ──
    company_cycle_enabled: bool = field(default_factory=lambda: _bool("SOCIETY_COMPANY_CYCLE_ENABLED", False))
    company_cycle_hour_utc: int = field(default_factory=lambda: _int("SOCIETY_COMPANY_CYCLE_HOUR_UTC", 1, minimum=0))
    company_max_active_hypotheses: int = field(default_factory=lambda: _int("SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES", 3, minimum=0))
    company_max_high_risk_investigations: int = field(default_factory=lambda: _int("SOCIETY_COMPANY_MAX_HIGH_RISK_INVESTIGATIONS", 1, minimum=0))
    # An APPROVED hypothesis nobody started within this window is SHELVED: it
    # stays APPROVED and visible, but no longer holds a portfolio slot
    # (company.portfolio_accounting). The cap itself is unchanged.
    company_hypothesis_shelf_hours: int = field(default_factory=lambda: _int("SOCIETY_COMPANY_HYPOTHESIS_SHELF_HOURS", 72, minimum=24))
    # ── public-surface synthetic monitor (surface_monitor.py) ──
    # Deterministic anonymous HTTP against the PUBLIC product; the model is
    # used only after a durable anomaly. OFF unless enabled (staging worker).
    public_surface_monitor_enabled: bool = field(default_factory=lambda: _bool("SOCIETY_PUBLIC_SURFACE_MONITOR_ENABLED", False))
    public_surface_monitor_interval_seconds: int = field(default_factory=lambda: _int("SOCIETY_PUBLIC_SURFACE_MONITOR_INTERVAL_SECONDS", 600, minimum=60))
    # >= 2: one timeout is noise, never a reason to write code.
    public_surface_failure_threshold: int = field(default_factory=lambda: _int("SOCIETY_PUBLIC_SURFACE_FAILURE_THRESHOLD", 2, minimum=2))
    public_surface_cooldown_seconds: int = field(default_factory=lambda: _int("SOCIETY_PUBLIC_SURFACE_COOLDOWN_SECONDS", 21600, minimum=600))
    public_surface_max_events_per_day: int = field(default_factory=lambda: _int("SOCIETY_PUBLIC_SURFACE_MAX_EVENTS_PER_DAY", 6, minimum=1))
    public_surface_timeout_seconds: int = field(default_factory=lambda: _int("SOCIETY_PUBLIC_SURFACE_TIMEOUT_SECONDS", 10, minimum=1))
    public_surface_target_label: str = field(default_factory=lambda: (os.getenv("SOCIETY_PUBLIC_SURFACE_TARGET_LABEL") or "production").strip()[:32])
    public_product_ui_origin: str = field(default_factory=lambda: (os.getenv("PUBLIC_PRODUCT_UI_ORIGIN") or "https://agentnet.io.vn").strip().rstrip("/"))
    public_product_api_origin: str = field(default_factory=lambda: (os.getenv("PUBLIC_PRODUCT_API_ORIGIN") or "https://api.agentnet.io.vn").strip().rstrip("/"))

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
        if not os.getenv("SOCIETY_MODEL_FAST_NAME"):
            # single-tier deployments: the fast tier IS the configured model
            object.__setattr__(self, "model_fast_name", self.model_name)
        if self.model_output_format not in OUTPUT_FORMATS:
            logger.warning("society config: unknown SOCIETY_MODEL_OUTPUT_FORMAT=%r; using 'auto'", self.model_output_format)
            object.__setattr__(self, "model_output_format", "auto")
        if self.promotion_provider not in PROMOTION_PROVIDERS:
            logger.warning("society config: unknown SOCIETY_PROMOTION_PROVIDER=%r; using 'disabled' (fail closed)", self.promotion_provider)
            object.__setattr__(self, "promotion_provider", "disabled")
        if self.github_credential_provider not in GITHUB_CREDENTIAL_PROVIDERS:
            logger.warning("society config: unknown SOCIETY_GITHUB_CREDENTIAL_PROVIDER=%r; using 'disabled' (fail closed)", self.github_credential_provider)
            object.__setattr__(self, "github_credential_provider", "disabled")
        if self.deployment_provider not in DEPLOYMENT_PROVIDERS:
            logger.warning("society config: unknown SOCIETY_DEPLOYMENT_PROVIDER=%r; using 'disabled' (fail closed)", self.deployment_provider)
            object.__setattr__(self, "deployment_provider", "disabled")
        if _bool("SOCIETY_PRODUCTION_DEPLOY_ENABLED", False):
            logger.warning(
                "SOCIETY_PRODUCTION_DEPLOY_ENABLED is set but production autonomous deploy is hard-disabled in v1; ignoring"
            )
        for problem in validate_settings(self):
            raise SocietyConfigError(problem)

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
            "promotion_provider": self.promotion_provider,
            "deployment_provider": self.deployment_provider,
            "auto_merge_enabled": self.auto_merge_enabled,
            "max_autonomous_merges_per_day": self.max_autonomous_merges_per_day,
        }


def validate_settings(s: "SocietySettings", *, model_caller: bool = False) -> list:
    """Fail-fast rules (see docs/DEPLOYMENT_ARCHITECTURE.md §2):

    * production never runs the society runtime or the autonomous code loop in
      this phase — the flags are refused, not ignored;
    * budgets and limits must be sane (no negative money, no impossible timeouts);
    * a live provider outside development must point at an https endpoint;
    * the model credential is required only in the process that calls the
      model (``model_caller=True``: the society worker, see ``worker.main``).
      On a split deployment the registry API mirrors the NON-secret live flags
      so ``/v1/society/status`` and ``/config`` tell the truth about the
      running worker — it never holds ``SOCIETY_MODEL_API_KEY``.
    """
    problems = []
    env = os.getenv("ENVIRONMENT", "development").strip().lower()
    if env == "production":
        if s.runtime_enabled:
            problems.append("SOCIETY_RUNTIME_ENABLED=true is refused in production (production Society activation is out of scope)")
        if s.autonomous_code_enabled:
            problems.append("SOCIETY_AUTONOMOUS_CODE_ENABLED=true is refused in production")
        if s.staging_deploy_enabled:
            problems.append("SOCIETY_STAGING_DEPLOY_ENABLED=true is meaningless in production and refused")
    if s.daily_model_budget_usd < 0:
        problems.append("SOCIETY_DAILY_MODEL_BUDGET must be >= 0")
    if s.company_cycle_hour_utc > 23:
        problems.append("SOCIETY_COMPANY_CYCLE_HOUR_UTC must be 0..23")
    if s.public_surface_monitor_enabled:
        for name, origin in (("PUBLIC_PRODUCT_UI_ORIGIN", s.public_product_ui_origin), ("PUBLIC_PRODUCT_API_ORIGIN", s.public_product_api_origin)):
            if not origin.startswith("https://") and env != "development":
                problems.append(f"{name} must be an https:// origin outside development (the monitor reads the PUBLIC product)")
            if any(c in origin for c in "@?#") or origin.count("/") > 2:
                problems.append(f"{name} must be a bare origin (scheme://host), without credentials, path, query or fragment")
        if env == "production":
            problems.append("SOCIETY_PUBLIC_SURFACE_MONITOR_ENABLED=true is refused in production (it is part of the Society, which production does not run)")
    if s.model_usd_per_1k_input < 0 or s.model_usd_per_1k_output < 0:
        problems.append("SOCIETY_MODEL_USD_PER_1K_* must be >= 0")
    if s.run_lease_seconds <= s.model_timeout_seconds // 2 and s.run_lease_seconds < 30:
        problems.append("SOCIETY_RUN_LEASE_SECONDS is too short for the model timeout (a run would lose its lease mid-call)")
    if s.auto_merge_enabled:
        # This used to refuse auto-merge with the GitHub provider outright, as a
        # phase guard, because no real promotion had ever been exercised. One has
        # now run end to end (candidate b8cee13c -> PR #30 -> required CI green),
        # so the guard is REPLACED by the conditions that actually make an
        # autonomous merge safe -- not simply deleted.
        if env == "production":
            problems.append("SOCIETY_AUTO_MERGE_ENABLED=true is refused in production (autonomous merge is staging-only)")
        if s.promotion_provider == "disabled":
            problems.append("SOCIETY_AUTO_MERGE_ENABLED=true needs a real promotion provider; 'disabled' can only pretend to merge")
        if s.promotion_provider == "github" and s.github_credential_provider == "disabled":
            problems.append("SOCIETY_AUTO_MERGE_ENABLED=true with the GitHub provider needs a real credential provider (app|static), not 'disabled'")
        if s.max_autonomous_merges_per_day < 1:
            problems.append("SOCIETY_AUTO_MERGE_ENABLED=true with SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY=0 is a contradiction; set the cap or turn auto-merge off")
        if s.max_open_autonomous_prs < 1:
            problems.append("SOCIETY_AUTO_MERGE_ENABLED=true with SOCIETY_MAX_OPEN_AUTONOMOUS_PRS=0 is a contradiction")
        if not s.autonomous_code_enabled:
            problems.append("SOCIETY_AUTO_MERGE_ENABLED=true without SOCIETY_AUTONOMOUS_CODE_ENABLED merges work the Society may not produce")
    if s.promotion_provider == "github" and (not s.github_repository.strip() or "/" not in s.github_repository):
        problems.append("SOCIETY_GITHUB_REPOSITORY (owner/repo) is required when SOCIETY_PROMOTION_PROVIDER=github")
    if s.github_credential_provider == "app":
        if not s.github_app_id or not s.github_installation_id:
            problems.append("SOCIETY_GITHUB_APP_ID and SOCIETY_GITHUB_INSTALLATION_ID are required when SOCIETY_GITHUB_CREDENTIAL_PROVIDER=app")
        if not s.github_app_private_key_file and not os.getenv("SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM"):
            problems.append("SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE (preferred, platform-mounted) or SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM must be provided to the controller process when SOCIETY_GITHUB_CREDENTIAL_PROVIDER=app")
    if s.github_app_private_key_file and s.github_app_private_key_file.lstrip().startswith("-----"):
        problems.append("SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE must be a file path, never key material")
    if s.model_capability_profile not in CAPABILITY_PROFILES:
        problems.append(f"SOCIETY_MODEL_CAPABILITY_PROFILE must be one of {'|'.join(CAPABILITY_PROFILES)}")
    if s.model_thinking_mode not in THINKING_MODES:
        problems.append(f"SOCIETY_MODEL_THINKING_MODE must be one of {'|'.join(THINKING_MODES)}")
    if s.model_reasoning_effort not in REASONING_EFFORTS:
        problems.append(f"SOCIETY_MODEL_REASONING_EFFORT must be one of {'|'.join(REASONING_EFFORTS)}")
    elif s.model_capability_profile == "deepseek" and s.model_reasoning_effort not in DEEPSEEK_REASONING_EFFORTS:
        problems.append(f"SOCIETY_MODEL_REASONING_EFFORT={s.model_reasoning_effort!r} is not documented by DeepSeek (use {'|'.join(DEEPSEEK_REASONING_EFFORTS)})")
    if s.model_thinking_mode == "disabled" and s.model_reasoning_effort not in ("auto", "none"):
        problems.append("SOCIETY_MODEL_THINKING_MODE=disabled contradicts SOCIETY_MODEL_REASONING_EFFORT=" + s.model_reasoning_effort)
    if s.model_thinking_mode == "enabled" and s.model_reasoning_effort == "none":
        problems.append("SOCIETY_MODEL_THINKING_MODE=enabled contradicts SOCIETY_MODEL_REASONING_EFFORT=none")
    if s.model_provider == LIVE_PROVIDER_NAME and env != "development":
        url = (s.model_base_url or "").strip()
        if not url.startswith("https://"):
            problems.append("SOCIETY_MODEL_BASE_URL must be an https:// URL outside development")
        if model_caller and not s.model_api_key:
            problems.append("SOCIETY_MODEL_API_KEY is required in the process that calls the model (society worker) when SOCIETY_MODEL_PROVIDER=openai_compatible")
    return problems


LIVE_PROVIDER_NAME = "openai_compatible"


@lru_cache(maxsize=1)
def get_settings() -> SocietySettings:
    return SocietySettings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
