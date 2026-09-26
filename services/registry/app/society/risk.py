"""Trusted change-risk classifier: GREEN / AMBER / RED / NEVER.

This module is TRUSTED BASE code. The promotion controller, QA and the
fitness engine import it from the running (installed) revision and apply it
to a candidate's changed paths and diff. A candidate branch may *propose*
edits to this file, but the proposal is classified by the copy of this
module that is already running — never by the copy inside the worktree
(tests/society/test_risk_and_meta_change.py proves the self-edit attack).

Tier semantics (docs/SELF_DEVELOPMENT.md):

* GREEN  — docs, non-sensitive UI/templates, isolated telemetry, harmless
           tests and fixtures: may eventually auto-merge after every gate.
* AMBER  — ordinary backend/worker/API logic, SDK, examples, ordinary
           dependencies: QA + Security + CI + human merge initially.
* RED    — Society runtime, cognition/policy/risk/fitness/promotion, auth /
           authz, payment / wallet / escrow, sandbox, deployment, GitHub
           automation, migrations / schema, dependency & security policy,
           evaluation criteria, model routing / budgets: agents MAY produce
           candidates, always human-approved, never auto-merged.
* NEVER  — secret values, secret stores, keys, git internals, workflow
           files that disable CI, deleted/disabled tests, unrestricted shell
           in the runtime: cannot be promoted at all (and most of these
           cannot even be written by the Builder workspace).

The classifier is deliberately simple, deterministic and path/diff based —
there is nothing here a model could persuade.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

from ..models import RiskTier

# Paths the Builder workspace refuses to WRITE at all (secret material, git
# internals). Everything else is writable and classified by tier below.
NEVER_WRITE_PATTERNS: Sequence[str] = (
    ".env*",
    "*/.env*",
    ".git/*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*secret*",
    "*/secrets/*",
    "*credential*",
    "id_rsa*",
    "*.keystore",
)

# Read deny-list for repository intelligence: secret-bearing or generated
# credential files, git internals, local overrides.
NEVER_READ_PATTERNS: Sequence[str] = NEVER_WRITE_PATTERNS + (
    "*.sqlite",
    "*.db",
    "*/node_modules/*",
    "*/__pycache__/*",
    "*.pyc",
    "*.log",
    "*/.venv/*",
    "*/.service-envs/*",
    "*/.sdk-envs/*",
)

RED_PATTERNS: Sequence[str] = (
    "services/registry/app/society/*",
    "services/registry/app/society/**",
    "tests/society/*",
    "tests/society/**",
    "services/*/app/auth.py",
    "services/*/app/authz.py",
    "services/*/app/security.py",
    "services/*/app/config.py",
    "services/*/app/api/rate_limiter.py",
    "services/registry/app/task_service.py",
    "services/registry/app/task_contract.py",
    "services/registry/app/websocket_manager.py",
    "services/registry/app/api/routes/websocket.py",
    "services/registry/app/api/routes/auth.py",
    "services/registry/app/api/routes/tokens.py",
    "services/registry/app/api/routes/society.py",
    "services/registry/app/db_bootstrap.py",
    "services/registry/app/schema_app_sql.py",
    "services/payment/*",
    "services/payment/**",
    "services/registry/init-db/*",
    "services/registry/migrations/*",
    "services/registry/migrations/**",
    "*/Dockerfile",
    "Dockerfile",
    "*/entrypoint.sh",
    "docker-compose*.yml",
    "deploy/*",
    "deploy/**",
    ".github/*",
    ".github/**",
    "*requirements*.txt",
    "requirements-dev.txt",
    "pytest.ini",
    "scripts/ci/*",
    "scripts/ci/**",
    "Makefile",
    # The public-surface acceptance gates are evaluation criteria (the
    # contract itself lives under society/ above): a candidate may not
    # change a page and the test that judges it in one autonomous change.
    "services/dashboard/tests/test_public_surface.py",
)

GREEN_PATTERNS: Sequence[str] = (
    "docs/*",
    "docs/**",
    "*.md",
    "*.rst",
    "*.txt",
    "services/dashboard/app/templates/*",
    "services/dashboard/app/templates/**",
    "services/dashboard/app/static/**",
    "tests/fixtures/**",
    "examples/**/*.md",
)

# Exceptions evaluated BEFORE the RED list: harmless acceptance tests for
# documentation candidates live under tests/society/ but are GREEN.
GREEN_OVERRIDE_PATTERNS: Sequence[str] = ("tests/society/acceptance/*",)

# Diff-level NEVER findings (reward hacking / gate disabling), matched on
# ADDED or REMOVED lines of the unified diff.
_NEVER_ADDED_RE: Sequence[Tuple[re.Pattern, str]] = (
    (re.compile(r"^\s*@pytest\.mark\.skip\b"), "test skipped"),
    (re.compile(r"^\s*pytest\.skip\("), "test skipped"),
    (re.compile(r"^\s*@pytest\.mark\.xfail\b"), "test marked xfail"),
    (re.compile(r"^\s*ignore::"), "warning gate ignored"),
    (re.compile(r"^\s*(?:addopts|filterwarnings)\s*="), "pytest gate rewritten"),
    (re.compile(r"^\s*ALLOWED_INTENT_TYPES\s*=\s*frozenset\(\s*\(?\s*t\s+for\s+t\s+in\s+IntentType\s*\)?\s*\)"), "forbidden intents unblocked"),
    (re.compile(r"\bsubprocess\.(?:run|Popen|call|check_output)\s*\(.*shell\s*=\s*True"), "unrestricted shell added"),
    (re.compile(r"\bos\.system\s*\("), "unrestricted shell added"),
    (re.compile(r"\bGITHUB_TOKEN\b|\bSOCIETY_GITHUB_TOKEN\b|\bSOCIETY_MODEL_API_KEY\b"), "credential reference added outside the secret boundary"),
    (re.compile(r"\bcontinue-on-error:\s*true\b"), "CI job made non-blocking"),
    (re.compile(r"^\s*if:\s*false\b"), "CI job disabled"),
)
_NEVER_REMOVED_RE: Sequence[Tuple[re.Pattern, str]] = (
    (re.compile(r"^\s*def test_\w+\s*\("), "test removed"),
    (re.compile(r"^\s*error::"), "warning gate removed"),
    (re.compile(r"^\s*assert\b"), "assertion removed"),
)

_ORDER = {RiskTier.GREEN: 0, RiskTier.AMBER: 1, RiskTier.RED: 2, RiskTier.NEVER: 3}


def _norm(path: str) -> str:
    p = path.replace(os.sep, "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lower()


def _match(path: str, patterns: Iterable[str]) -> bool:
    p = _norm(path)
    for raw in patterns:
        pat = raw.lower()
        if fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(p, "*/" + pat):
            return True
        # "dir/*" and "dir/**" also cover nested files
        if pat.endswith("/*") and p.startswith(pat[:-1]):
            return True
        if pat.endswith("/**") and p.startswith(pat[:-2]):
            return True
    return False


def is_never_writable(path: str) -> bool:
    return _match(path, NEVER_WRITE_PATTERNS)


def is_never_readable(path: str) -> bool:
    return _match(path, NEVER_READ_PATTERNS)


def tier_for_path(path: str) -> RiskTier:
    if is_never_writable(path):
        return RiskTier.NEVER
    if _match(path, GREEN_OVERRIDE_PATTERNS):
        return RiskTier.GREEN
    if _match(path, RED_PATTERNS):
        return RiskTier.RED
    if _match(path, GREEN_PATTERNS):
        return RiskTier.GREEN
    return RiskTier.AMBER


def max_tier(tiers: Iterable[RiskTier]) -> RiskTier:
    best = RiskTier.GREEN
    for t in tiers:
        if _ORDER[t] > _ORDER[best]:
            best = t
    return best


def tier_at_least(tier: RiskTier, floor: RiskTier) -> bool:
    return _ORDER[tier] >= _ORDER[floor]


@dataclass
class RiskAssessment:
    tier: RiskTier
    per_path: dict = field(default_factory=dict)
    never_findings: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "per_path": {k: v.value for k, v in self.per_path.items()},
            "never_findings": list(self.never_findings),
            "reasons": list(self.reasons),
        }


def diff_never_findings(diff_text: str, changed_paths: Sequence[str] = ()) -> List[str]:
    """Scan a unified diff for gate-disabling / reward-hacking edits."""
    findings: List[str] = []
    current_file = ""
    deleted_files = set()
    for ln in (diff_text or "").splitlines():
        if ln.startswith("+++ "):
            current_file = ln[4:].strip()
            if current_file.startswith("b/"):
                current_file = current_file[2:]
            if current_file == "/dev/null":
                findings.append("file deleted")
            continue
        if ln.startswith("--- "):
            name = ln[4:].strip()
            if name.startswith("a/"):
                name = name[2:]
            if name != "/dev/null":
                deleted_files.add(name)
            continue
        if ln.startswith("+") and not ln.startswith("+++"):
            body = ln[1:]
            for pat, label in _NEVER_ADDED_RE:
                if pat.search(body):
                    findings.append(f"{label}: {current_file or '?'}")
                    break
        elif ln.startswith("-") and not ln.startswith("---"):
            body = ln[1:]
            for pat, label in _NEVER_REMOVED_RE:
                if pat.search(body) and (current_file.startswith("tests/") or current_file.endswith("pytest.ini") or label != "assertion removed"):
                    findings.append(f"{label}: {current_file or '?'}")
                    break
    for p in changed_paths:
        if _norm(p).startswith("tests/") and f"file deleted" in findings and p in deleted_files:
            findings.append(f"test file deleted: {p}")
    # de-duplicate, keep order
    seen = set()
    out = []
    for f in findings:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out[:40]


def assess(changed_paths: Sequence[str], diff_text: str = "", *, spec_kind: str = "") -> RiskAssessment:
    """Classify a change from its paths + diff. Pure function of trusted code."""
    per_path = {p: tier_for_path(p) for p in changed_paths}
    tier = max_tier(per_path.values()) if per_path else RiskTier.GREEN
    reasons = [f"{p}: {t.value}" for p, t in per_path.items() if t != RiskTier.GREEN][:20]
    never = diff_never_findings(diff_text, changed_paths)
    if never:
        tier = RiskTier.NEVER
        reasons.extend(never)
    if spec_kind == "code" and tier == RiskTier.GREEN:
        # Application code is never GREEN even when it lives under a green path.
        tier = RiskTier.AMBER
        reasons.append("kind=code floors the tier at AMBER")
    return RiskAssessment(tier=tier, per_path=per_path, never_findings=never, reasons=reasons)


__all__ = [
    "RiskTier",
    "RiskAssessment",
    "NEVER_WRITE_PATTERNS",
    "NEVER_READ_PATTERNS",
    "RED_PATTERNS",
    "GREEN_PATTERNS",
    "is_never_writable",
    "is_never_readable",
    "tier_for_path",
    "max_tier",
    "tier_at_least",
    "diff_never_findings",
    "assess",
]
