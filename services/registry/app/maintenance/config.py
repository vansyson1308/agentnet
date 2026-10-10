"""Maintenance OS settings and kill switches (ADR-0010 D17).

Everything that acts defaults to OFF. The switches are independent:

* ``MAINTENANCE_AUTONOMY_ENABLED``   -- the ONE owner master kill. False stops
  every new autonomous repair and release. Public services keep running;
  incidents are still recorded when monitoring is on.
* ``MAINTENANCE_MONITORING_ENABLED`` -- trusted observations become incidents.
* ``MAINTENANCE_COGNITION_ENABLED``  -- repair activities may call the model.
* ``MAINTENANCE_GREEN_PROMOTION_ENABLED`` -- a verified repair may be carried
  to ``main`` by the promotion controller (GREEN auto-merge still also needs
  ``SOCIETY_AUTO_MERGE_ENABLED``).
* ``MAINTENANCE_GREEN_RELEASE_ENABLED`` -- the Release Controller may release
  MAINTENANCE_GREEN repairs to production (read by the release controller
  process, which also needs its own provider credentials).

Limits are trusted constants with documented justification
(docs/MAINTENANCE_POLICY.md). No intent, activity or model output can change
any of them: they are read from the environment of the controller process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from ..society.config import _bool, _decimal, _int


def _str(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


@dataclass
class MaintenanceSettings:
    # ── switches ───────────────────────────────────────────────────────
    autonomy_enabled: bool = field(default_factory=lambda: _bool("MAINTENANCE_AUTONOMY_ENABLED", False))
    monitoring_enabled: bool = field(default_factory=lambda: _bool("MAINTENANCE_MONITORING_ENABLED", False))
    cognition_enabled: bool = field(default_factory=lambda: _bool("MAINTENANCE_COGNITION_ENABLED", False))
    green_promotion_enabled: bool = field(default_factory=lambda: _bool("MAINTENANCE_GREEN_PROMOTION_ENABLED", False))
    green_release_enabled: bool = field(default_factory=lambda: _bool("MAINTENANCE_GREEN_RELEASE_ENABLED", False))
    target: str = field(default_factory=lambda: _str("MAINTENANCE_TARGET", "production"))

    # ── repair bounds (per case) ───────────────────────────────────────
    max_plan_revisions: int = field(default_factory=lambda: _int("MAINTENANCE_MAX_PLAN_REVISIONS", 3, minimum=1))
    max_repair_attempts: int = field(default_factory=lambda: _int("MAINTENANCE_MAX_REPAIR_ATTEMPTS", 4, minimum=1))
    max_files: int = field(default_factory=lambda: _int("MAINTENANCE_MAX_FILES", 8, minimum=1))
    max_changed_lines: int = field(default_factory=lambda: _int("MAINTENANCE_MAX_CHANGED_LINES", 400, minimum=1))
    case_max_hours: int = field(default_factory=lambda: _int("MAINTENANCE_CASE_MAX_HOURS", 24, minimum=1))
    max_case_cost_usd: Decimal = field(default_factory=lambda: _decimal("MAINTENANCE_MAX_CASE_COST_USD", "0.50"))
    builder_max_turns: int = field(default_factory=lambda: _int("MAINTENANCE_BUILDER_MAX_TURNS", 10, minimum=1))
    builder_max_read_calls: int = field(default_factory=lambda: _int("MAINTENANCE_BUILDER_MAX_READ_CALLS", 3, minimum=0))
    # best-of-N: independent AuthorPatch samples (fresh worktree each, rising
    # temperature), stopping at the first green one; they share one cost cap
    builder_samples: int = field(default_factory=lambda: _int("MAINTENANCE_BUILDER_SAMPLES", 3, minimum=1))
    # the per-turn max_tokens an AuthorPatch model call actually gets
    builder_max_output_tokens: int = field(default_factory=lambda: _int("MAINTENANCE_BUILDER_MAX_OUTPUT_TOKENS", 2500, minimum=256))
    max_test_runs_per_attempt: int = field(default_factory=lambda: _int("MAINTENANCE_MAX_TEST_RUNS_PER_ATTEMPT", 8, minimum=1))
    activity_max_tries: int = field(default_factory=lambda: _int("MAINTENANCE_ACTIVITY_MAX_TRIES", 3, minimum=1))
    activity_timeout_seconds: int = field(default_factory=lambda: _int("MAINTENANCE_ACTIVITY_TIMEOUT_SECONDS", 180, minimum=10))
    read_page_bytes: int = field(default_factory=lambda: _int("MAINTENANCE_READ_PAGE_BYTES", 12000, minimum=1000))
    max_read_bytes_per_activity: int = field(default_factory=lambda: _int("MAINTENANCE_MAX_READ_BYTES_PER_ACTIVITY", 120000, minimum=1000))

    # ── queue + budget (maintenance lane only; never the innovation portfolio) ──
    max_active_urgent: int = field(default_factory=lambda: _int("MAINTENANCE_MAX_ACTIVE_P0P1", 1, minimum=1))
    max_active_routine: int = field(default_factory=lambda: _int("MAINTENANCE_MAX_ACTIVE_P2P3", 2, minimum=0))
    daily_budget_usd: Decimal = field(default_factory=lambda: _decimal("MAINTENANCE_DAILY_MODEL_BUDGET_USD", "1.00"))
    urgent_reserve_usd: Decimal = field(default_factory=lambda: _decimal("MAINTENANCE_P0P1_RESERVE_USD", "0.40"))

    # ── reconciliation ────────────────────────────────────────────────
    lease_seconds: int = field(default_factory=lambda: _int("MAINTENANCE_LEASE_SECONDS", 900, minimum=30))
    reconcile_batch: int = field(default_factory=lambda: _int("MAINTENANCE_RECONCILE_BATCH", 5, minimum=1))
    stall_seconds: int = field(default_factory=lambda: _int("MAINTENANCE_STALL_SECONDS", 900, minimum=60))
    recovery_streak: int = field(default_factory=lambda: _int("MAINTENANCE_RECOVERY_STREAK", 3, minimum=2))
    confirm_window_seconds: int = field(default_factory=lambda: _int("MAINTENANCE_CONFIRM_WINDOW_SECONDS", 1800, minimum=60))
    case_reopen_cooldown_hours: int = field(default_factory=lambda: _int("MAINTENANCE_CASE_REOPEN_COOLDOWN_HOURS", 24, minimum=1))
    max_cases_per_incident: int = field(default_factory=lambda: _int("MAINTENANCE_MAX_CASES_PER_INCIDENT", 3, minimum=1))

    # ── release ────────────────────────────────────────────────────────
    max_auto_releases_per_day: int = field(default_factory=lambda: _int("MAINTENANCE_MAX_AUTO_RELEASES_PER_DAY", 1, minimum=0))
    post_deploy_healthy_observations: int = field(default_factory=lambda: _int("MAINTENANCE_POST_DEPLOY_HEALTHY_OBSERVATIONS", 3, minimum=2))
    post_deploy_interval_seconds: int = field(default_factory=lambda: _int("MAINTENANCE_POST_DEPLOY_INTERVAL_SECONDS", 60, minimum=5))

    def public_flags(self) -> dict:
        return {
            "autonomy_enabled": self.autonomy_enabled,
            "monitoring_enabled": self.monitoring_enabled,
            "cognition_enabled": self.cognition_enabled,
            "green_promotion_enabled": self.green_promotion_enabled,
            "green_release_enabled": self.green_release_enabled,
            "target": self.target,
        }

    def bounds(self) -> dict:
        return {
            "max_plan_revisions": self.max_plan_revisions,
            "max_repair_attempts": self.max_repair_attempts,
            "max_files": self.max_files,
            "max_changed_lines": self.max_changed_lines,
            "case_max_hours": self.case_max_hours,
            "max_case_cost_usd": str(self.max_case_cost_usd),
            "daily_budget_usd": str(self.daily_budget_usd),
            "urgent_reserve_usd": str(self.urgent_reserve_usd),
            "max_active_p0p1": self.max_active_urgent,
            "max_active_p2p3": self.max_active_routine,
            "max_auto_releases_per_day": self.max_auto_releases_per_day,
        }


def get_maintenance_settings() -> MaintenanceSettings:
    """Read at call time: the owner flips a switch and the next cycle obeys."""
    return MaintenanceSettings()


__all__ = ["MaintenanceSettings", "get_maintenance_settings"]
