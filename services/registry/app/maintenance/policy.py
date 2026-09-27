"""Trusted Maintenance Policy (ADR-0010 D9-D12). Decision only.

Policy DECIDES here; enforcement lives where the action happens (the
reconciler, the promotion controller's freeze check, the release controller).
Every function is a pure function of trusted inputs: paths, diffs, durable
rows and constants. Nothing a model writes is an input to a decision.

Risk classes
------------
The existing path classifier (``society/risk.py``) is the BASE. On top of it:

* CONSTITUTIONAL -- NEVER-tier findings (secrets, deleted/disabled tests,
  gate disabling) or anti-reward-hacking findings: refused outright.
* RED -- the base RED surfaces, the Maintenance OS itself, any trusted
  evaluation artefact (a contract, detector or verification test), and money
  / auth semantics found in the diff. Prepared and escalated, never released
  autonomously.
* AMBER -- ordinary product code (e.g. dashboard route handlers), oversized
  diffs, dependency hints. Prepared, PR opened, escalated for the owner.
* MAINTENANCE_GREEN -- only when EVERY condition holds (see
  :func:`classify_patch`): base tier GREEN (templates, static assets, docs),
  bounded diff, no sensitive semantics, no evaluation artefact, no
  anti-reward-hacking finding.

A GREEN *class* is necessary, not sufficient, for an autonomous release:
:func:`green_release_blockers` adds the runtime conditions (tests, CI,
preview, freeze, error budget, caps) that the Release Controller recomputes.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import CodeCandidate, IncidentFreeze, RiskTier
from ..society import risk as base_risk
from ..society.events import utcnow
from . import state_machine as sm
from .config import MaintenanceSettings
from .contracts import load_registry
from .orm import MaintenanceIncident, RepairActivity, RepairCase, RepairPlanRevision
from .taxonomy import IncidentClass, MaintenanceRiskClass as MRC, Priority, risk_max

# ── sensitive semantics (diff content, not only paths) ─────────────────────

_MONEY_RE = re.compile(r"\b(wallet|escrow|balance_credits|balance_usdc|reserved_credits|transaction|platform_fee|spending_cap|refund)\b", re.I)
_AUTH_RE = re.compile(r"\b(session\[|login_user|logout|password|jwt|access_token|refresh_token|csrf|set_cookie|SESSION_COOKIE|verify_token|authorization|X-API-Key)\b", re.I)
_INFRA_PATHS = ("*/Dockerfile", "Dockerfile", "docker-compose*.yml", ".railway/*", "deploy/*", ".github/*", "*requirements*.txt", "services/*/gunicorn.conf.py")
_CLOUDFLARE_RE = re.compile(r"\bcloudflare\b|\bCF_API\b", re.I)

# ── anti-reward-hacking (ADR-0010 D11): a repair may not make the detector
#    stop seeing the defect instead of removing the defect ────────────────
_HACK_ADDED: Sequence[Tuple[re.Pattern, str]] = (
    (re.compile(r"^\s*except\s*(?:Exception|BaseException)?\s*(?:as\s+\w+)?\s*:\s*(?:pass|return\b.*)?\s*$"), "catch_all_exception_added"),
    (re.compile(r"url_build_error_handlers"), "url_build_fallback_added"),
    (re.compile(r"""return\s+["']#["']"""), "placeholder_link_fallback_added"),
    (re.compile(r"errorhandler\(\s*(?:404|500|Exception)\s*\)"), "error_handler_rewritten"),
    (re.compile(r"""\.(?:flash|flash-message|alert)\b[^{]*\{[^}]*display\s*:\s*none""", re.I), "error_banner_hidden"),
    (re.compile(r"""class=["'][^"']*\balert\b[^"']*["'][^>]*style=["'][^"']*display\s*:\s*none""", re.I), "error_banner_hidden"),
    (re.compile(r"""\bFEATURE_\w+_ENABLED\s*=\s*False\b|["']disabled["']\s*:\s*True"""), "feature_disabled"),
)
_HACK_REMOVED: Sequence[Tuple[re.Pattern, str]] = (
    (re.compile(r"^\s*flash\("), "error_message_removed"),
)


@dataclass
class RiskDecision:
    risk_class: MRC
    reasons: List[str] = field(default_factory=list)
    base_tier: str = "green"
    findings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"risk_class": self.risk_class.value, "reasons": self.reasons[:30], "base_tier": self.base_tier, "findings": self.findings[:30]}


def _match(path: str, patterns: Iterable[str]) -> bool:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    for pat in patterns:
        if pat.endswith("/**"):
            if p.startswith(pat[:-3] + "/") or p == pat[:-3]:
                return True
        elif pat.endswith("/"):
            if p.startswith(pat):
                return True
        elif fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(p, "*/" + pat):
            return True
    return False


def evaluation_paths() -> Tuple[str, ...]:
    return tuple(sorted(load_registry().evaluation_paths()))


def is_evaluation_path(path: str) -> bool:
    return _match(path, evaluation_paths())


def classify_paths(paths: Sequence[str], *, settings: Optional[MaintenanceSettings] = None) -> RiskDecision:
    """Plan-time classification from the files a plan revision allows."""
    return classify_patch(paths, "", settings=settings, plan_time=True)


def reward_hacking_findings(diff_text: str) -> List[str]:
    out: List[str] = []
    current = "?"
    for ln in (diff_text or "").splitlines():
        if ln.startswith("+++ "):
            current = ln[4:].strip()
            current = current[2:] if current.startswith("b/") else current
            continue
        if ln.startswith("--- "):
            continue
        if ln.startswith("+") and not ln.startswith("+++"):
            for pat, label in _HACK_ADDED:
                if pat.search(ln[1:]):
                    out.append(f"{label}: {current}")
                    break
        elif ln.startswith("-") and not ln.startswith("---"):
            for pat, label in _HACK_REMOVED:
                if pat.search(ln[1:]):
                    out.append(f"{label}: {current}")
                    break
    seen, res = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            res.append(f)
    return res


def _changed_line_count(diff_text: str) -> int:
    return sum(1 for ln in (diff_text or "").splitlines() if (ln.startswith("+") and not ln.startswith("+++")) or (ln.startswith("-") and not ln.startswith("---")))


def classify_patch(paths: Sequence[str], diff_text: str, *, settings: Optional[MaintenanceSettings] = None, plan_time: bool = False) -> RiskDecision:
    """Trusted maintenance risk of a change. Deterministic; fails upward."""
    settings = settings or MaintenanceSettings()
    base = base_risk.assess(list(paths), diff_text or "")
    reasons: List[str] = list(base.reasons)
    findings: List[str] = []
    cls = {
        RiskTier.GREEN: MRC.MAINTENANCE_GREEN,
        RiskTier.AMBER: MRC.AMBER,
        RiskTier.RED: MRC.RED,
        RiskTier.NEVER: MRC.CONSTITUTIONAL,
    }[base.tier]
    if base.never_findings:
        findings.extend(base.never_findings)
    if not paths:
        return RiskDecision(MRC.AMBER, ["empty scope"], base.tier.value, findings)
    for p in paths:
        if base_risk.is_never_writable(p):
            cls = MRC.CONSTITUTIONAL
            findings.append(f"never-writable path: {p}")
        if is_evaluation_path(p):
            cls = risk_max(cls, MRC.RED)
            reasons.append(f"trusted evaluation / kernel path: {p}")
        if _match(p, _INFRA_PATHS):
            cls = risk_max(cls, MRC.RED)
            reasons.append(f"infrastructure path: {p}")
    product = [p for p in paths if not is_evaluation_path(p)]
    evaluation = [p for p in paths if is_evaluation_path(p)]
    if product and evaluation:
        cls = risk_max(cls, MRC.RED)
        reasons.append("evaluation laundering: product and the contract/detector/test that judges it in one change")
    if not plan_time:
        added = "\n".join(ln[1:] for ln in (diff_text or "").splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        removed = "\n".join(ln[1:] for ln in (diff_text or "").splitlines() if ln.startswith("-") and not ln.startswith("---"))
        if _MONEY_RE.search(added) or _MONEY_RE.search(removed):
            cls = risk_max(cls, MRC.RED)
            reasons.append("money/economics semantics in the diff")
        if _AUTH_RE.search(added) or _AUTH_RE.search(removed):
            cls = risk_max(cls, MRC.AMBER if cls is MRC.MAINTENANCE_GREEN else cls)
            reasons.append("auth/session semantics in the diff")
        if _CLOUDFLARE_RE.search(added):
            cls = risk_max(cls, MRC.RED)
            reasons.append("DNS/edge change")
        hacks = reward_hacking_findings(diff_text)
        if hacks:
            cls = MRC.CONSTITUTIONAL
            findings.extend(hacks)
            reasons.append("anti-reward-hacking finding")
        lines = _changed_line_count(diff_text)
        if lines > settings.max_changed_lines:
            cls = risk_max(cls, MRC.AMBER)
            reasons.append(f"diff exceeds the GREEN bound ({lines} > {settings.max_changed_lines} lines)")
    if len(paths) > settings.max_files:
        cls = risk_max(cls, MRC.AMBER)
        reasons.append(f"scope exceeds the GREEN bound ({len(paths)} > {settings.max_files} files)")
    return RiskDecision(cls, reasons[:30], base.tier.value, findings[:30])


def escalates(previous: Optional[str], new: MRC) -> bool:
    """A re-scope that crosses a risk boundary upward (GREEN->AMBER, *->RED)."""
    if previous is None:
        return False
    from .taxonomy import RISK_ORDER

    return RISK_ORDER[new] > RISK_ORDER[MRC(previous)]


# ── capacity + budget (maintenance lane only) ─────────────────────────────

URGENT = (Priority.P0.value, Priority.P1.value)
#: States that hold a maintenance "slot" (they consume Builder/QA capacity).
BUSY_STATES = tuple(s.value for s in (sm.CaseState.TRIAGED, sm.CaseState.DIAGNOSING, sm.CaseState.PLAN_READY, sm.CaseState.BUILDING, sm.CaseState.VERIFYING, sm.CaseState.NEEDS_RESCOPE))


def has_capacity(db: Session, settings: MaintenanceSettings, priority: str, *, exclude_case=None) -> bool:
    """Separate bounded queue: P0/P1 slots and P2/P3 slots. An urgent case may
    also take a routine slot (reliability preempts polish, never the reverse)."""
    q = db.query(RepairCase.priority, func.count(RepairCase.id)).filter(RepairCase.state.in_(BUSY_STATES))
    if exclude_case is not None:
        q = q.filter(RepairCase.id != exclude_case)
    counts = dict(q.group_by(RepairCase.priority).all())
    urgent = sum(int(counts.get(p, 0)) for p in URGENT)
    routine = sum(int(v) for k, v in counts.items() if k not in URGENT)
    if priority in URGENT:
        return urgent < settings.max_active_urgent or routine + max(0, urgent - settings.max_active_urgent) < settings.max_active_routine
    return routine + max(0, urgent - settings.max_active_urgent) < settings.max_active_routine


def spend_today(db: Session, now: Optional[datetime] = None) -> Decimal:
    now = now or utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    v = db.query(func.coalesce(func.sum(RepairActivity.cost_usd), 0)).filter(RepairActivity.started_at >= start).scalar()
    return Decimal(str(v or 0))


def budget_allows(db: Session, settings: MaintenanceSettings, case: RepairCase, *, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """The maintenance model budget. P2/P3 may never eat into the P0/P1 reserve;
    no case exceeds its own cap. Agents cannot raise either."""
    cap = settings.max_case_cost_usd * (2 if case.priority in URGENT else 1)
    if Decimal(str(case.model_cost_usd or 0)) >= cap:
        return False, "case_budget_exhausted"
    spent = spend_today(db, now)
    ceiling = settings.daily_budget_usd if case.priority in URGENT else max(Decimal("0"), settings.daily_budget_usd - settings.urgent_reserve_usd)
    if spent >= ceiling:
        return False, "daily_budget_exhausted" if case.priority in URGENT else "routine_budget_exhausted"
    return True, ""


# ── freeze repair exception (ADR-0010 D14) ────────────────────────────────


def repair_exception(db: Session, candidate: CodeCandidate, freeze: IncidentFreeze) -> bool:
    """May THIS candidate merge through THIS open incident freeze?

    Only a maintenance repair whose case is linked to the incident behind the
    freeze, whose changed files lie inside its immutable plan revision, and
    whose trusted class allows merging. Everything else stays frozen."""
    spec = candidate.spec or {}
    m = spec.get("maintenance") or {}
    case_id = m.get("case_id")
    if not case_id:
        return False
    case = db.get(RepairCase, case_id)
    if case is None or case.state != sm.CaseState.PROMOTING.value or str(case.candidate_id) != str(candidate.id):
        return False
    incident = db.get(MaintenanceIncident, case.incident_id)
    if incident is None:
        return False
    ev = freeze.evidence or {}
    linked = ev.get("incident_id") == str(incident.id)
    if not linked and freeze.source == "public_surface":
        linked = incident.source == "public_surface_monitor" and incident.incident_class == IncidentClass.AVAILABILITY.value
    if not linked:
        return False
    plan = db.query(RepairPlanRevision).filter(RepairPlanRevision.case_id == case.id, RepairPlanRevision.revision == case.current_plan_revision).first()
    if plan is None:
        return False
    allowed = set(plan.files_allowed or [])
    changed = set(candidate.changed_files or [])
    if not changed or not changed <= allowed:
        return False
    return case.risk_class in (MRC.MAINTENANCE_GREEN.value, MRC.AMBER.value)


def innovation_freeze_reasons(db: Session, *, target: str, candidate: CodeCandidate, now: Optional[datetime] = None) -> List[str]:
    """Reasons an innovation (non-maintenance) candidate may not merge now:
    exhausted error budget, or an active P0 maintenance case (reliability
    preempts feature work)."""
    if (candidate.spec or {}).get("maintenance"):
        return []
    from .slo import error_budget_exhausted  # noqa: PLC0415

    reasons = [f"error_budget_exhausted({','.join(x)})" for x in [error_budget_exhausted(db, target=target, now=now)] if x]
    p0 = db.query(RepairCase.id).filter(RepairCase.priority == Priority.P0.value, RepairCase.state.in_([s.value for s in sm.NON_TERMINAL])).first()
    if p0 is not None:
        reasons.append("p0_maintenance_active")
    return reasons


__all__ = [
    "RiskDecision",
    "classify_paths",
    "classify_patch",
    "reward_hacking_findings",
    "evaluation_paths",
    "is_evaluation_path",
    "escalates",
    "has_capacity",
    "spend_today",
    "budget_allows",
    "repair_exception",
    "innovation_freeze_reasons",
    "BUSY_STATES",
    "URGENT",
]
