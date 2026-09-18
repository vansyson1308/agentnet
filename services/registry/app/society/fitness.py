"""Fitness / evaluation engine: did the change IMPROVE the system?

"Tests passed" is not "system improved". An experiment compares the trusted
BASE revision with the candidate on the same targets and decides with:

    HARD SAFETY GATES  →  MULTI-OBJECTIVE METRIC DELTAS  →  DECISION

Hard gates always win; soft metrics are compared only when every gate
passes; safety is never traded for speed or cost.

Trusted criteria
----------------
``TRUSTED_CRITERIA`` lives in THIS module of the RUNNING revision. A
candidate may edit ``fitness.py`` in its worktree; the experiment still uses
the criteria of the base copy and snapshots them on the experiment row
(``criteria_snapshot``) before anything runs. The Evaluator agent can only
request an experiment and record a recommendation — it cannot change
thresholds or the decision (tests/society/test_fitness*.py).

Evaluation mode
---------------
``offline`` (this phase): an ephemeral worktree at the candidate's base sha
versus the candidate worktree, running the candidate's acceptance + regression
targets under pytest (argv, scrubbed env, timeout), plus deterministic diff
scans. Real-traffic metrics are NOT fabricated; ``staging_live`` waits for a
host.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    AgentIntent,
    AgentRun,
    ChangeExperiment,
    CodeCandidate,
    CodePromotion,
    ExperimentStatus,
    IntentExecutionStatus,
    MemoryItem,
    MemoryScope,
    PolicyDecision,
    PromotionStatus,
    RiskTier,
    SocietyEvent,
)
from .config import SocietySettings
from .engineering import workspace as ws_mod
from .engineering.qa import RISKY_CODE_PATTERNS, SECRET_PATTERNS, scrubbed_env
from .events import EventType, emit_event, utcnow
from .risk import assess as assess_risk

logger = logging.getLogger(__name__)

CRITERIA_VERSION = "fitness-v1"

# Trusted, versioned criteria. Thresholds are deliberately conservative.
TRUSTED_CRITERIA: Dict[str, Any] = {
    "version": CRITERIA_VERSION,
    "hard_gates": [
        "risk_tier_not_never",
        "no_test_regression",
        "no_test_removal",
        "no_security_regression",
        "no_never_findings",
        "no_metric_collection_disabled",
        "candidate_tests_completed",
    ],
    "soft": {
        # metric: (direction, improve_threshold, regress_threshold) — absolute deltas unless noted
        "correctness.tests_passed": {"higher_is_better": True, "improve": 1, "regress": 1},
        "correctness.tests_failed": {"higher_is_better": False, "improve": 1, "regress": 1},
        "reliability.timeouts": {"higher_is_better": False, "improve": 1, "regress": 1},
        "performance.test_duration_s": {"higher_is_better": False, "regress_ratio": 0.5, "regress_min_abs": 1.0, "improve_ratio": 0.3, "improve_min_abs": 1.0},
        "economics.diff_lines": {"higher_is_better": False, "regress_abs_over": 400},
        "economics.model_cost_usd": {"higher_is_better": False, "regress_abs_over": 0.20},
        "safety.risky_primitives": {"higher_is_better": False, "regress": 1},
        "autonomy.qa_attempts": {"higher_is_better": False, "regress": 2},
        "autonomy.intents_denied": {"higher_is_better": False, "regress": 3},
    },
    "confidence": {"high_if_no_timeouts": True},
}

_METRIC_DISABLE_RE = re.compile(r"(prometheus_client|start_http_server|_counter\(|_gauge\(|M_[A-Z_]+\.(?:inc|set)\(|\.labels\(|metrics)", re.I)


class FitnessError(Exception):
    pass


@dataclass
class TestOutcome:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    collected: int = 0
    duration_s: float = 0.0
    timed_out: bool = False
    exit_code: int = 0
    per_test: Dict[str, str] = field(default_factory=dict)  # nodeid -> passed|failed|error|skipped
    tail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["per_test"] = dict(list(self.per_test.items())[:400])
        return d


def _run_pytest(root: pathlib.Path, targets: Sequence[str], *, timeout: int) -> TestOutcome:
    existing = [t for t in targets if (root / t.split("::")[0]).exists()]
    if not existing:
        return TestOutcome(exit_code=5, tail="no targets exist at this revision")
    report = root / f".fitness-{uuid.uuid4().hex[:8]}.xml"
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--timeout", str(min(timeout, 600)), f"--junitxml={report}", *existing]
    env = scrubbed_env(ws_mod.Workspace(candidate_id=uuid.uuid4(), path=root, branch="", base_sha="", repo_root=root))
    started = time.monotonic()
    out = TestOutcome()
    try:
        proc = subprocess.run(argv, cwd=str(root), env=env, capture_output=True, text=True, timeout=timeout + 30, check=False)
        out.exit_code = proc.returncode
        out.tail = (proc.stdout + "\n" + proc.stderr)[-2000:]
        if proc.returncode != 0 and "unrecognized arguments: --timeout" in out.tail:
            argv = [a for a in argv if a not in ("--timeout", str(min(timeout, 600)))]
            proc = subprocess.run(argv, cwd=str(root), env=env, capture_output=True, text=True, timeout=timeout + 30, check=False)
            out.exit_code = proc.returncode
            out.tail = (proc.stdout + "\n" + proc.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        out.timed_out = True
        out.exit_code = -1
        out.tail = f"timed out after {timeout}s"
    except OSError as exc:
        out.exit_code = -2
        out.tail = f"could not start pytest: {exc}"
    out.duration_s = round(time.monotonic() - started, 3)
    try:
        if report.exists():
            tree = ET.parse(report)
            for case in tree.iter("testcase"):
                nodeid = f"{case.get('classname', '')}::{case.get('name', '')}"
                status = "passed"
                for child in case:
                    if child.tag == "failure":
                        status = "failed"
                    elif child.tag == "error":
                        status = "error"
                    elif child.tag == "skipped":
                        status = "skipped"
                out.per_test[nodeid] = status
            out.collected = len(out.per_test)
            out.passed = sum(1 for s in out.per_test.values() if s == "passed")
            out.failed = sum(1 for s in out.per_test.values() if s == "failed")
            out.errors = sum(1 for s in out.per_test.values() if s == "error")
            out.skipped = sum(1 for s in out.per_test.values() if s == "skipped")
    finally:
        try:
            report.unlink(missing_ok=True)
        except OSError:
            pass
    return out


def _diff_safety(diff_text: str) -> Dict[str, int]:
    added = [ln[1:] for ln in diff_text.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    removed = [ln[1:] for ln in diff_text.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    secrets = sum(1 for ln in added if any(p.search(ln) for p in SECRET_PATTERNS))
    risky = sum(1 for ln in added if any(p.search(ln) for p in RISKY_CODE_PATTERNS))
    metrics_removed = sum(1 for ln in removed if _METRIC_DISABLE_RE.search(ln)) - sum(1 for ln in added if _METRIC_DISABLE_RE.search(ln))
    return {"secret_findings": secrets, "risky_primitives": risky, "metric_lines_removed": max(0, metrics_removed)}


def _correlation_costs(db: Session, correlation_id: uuid.UUID) -> Dict[str, Any]:
    cost = db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0)).filter(AgentRun.correlation_id == correlation_id).scalar() or 0
    denied = (
        db.query(func.count(AgentIntent.id))
        .join(AgentRun, AgentRun.id == AgentIntent.run_id)
        .filter(AgentRun.correlation_id == correlation_id, AgentIntent.policy_decision.in_([PolicyDecision.DENY, PolicyDecision.INVALID]))
        .scalar()
        or 0
    )
    dead = db.query(func.count(AgentRun.id)).filter(AgentRun.correlation_id == correlation_id, AgentRun.status == "dead").scalar() or 0
    loops = db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.correlation_id == correlation_id, SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED).scalar() or 0
    return {"model_cost_usd": float(Decimal(str(cost))), "intents_denied": int(denied), "runs_dead": int(dead), "loop_breaks": int(loops)}


def _targets(candidate: CodeCandidate) -> List[str]:
    spec = candidate.spec or {}
    t = list(spec.get("acceptance_tests") or []) + list(spec.get("regression_tests") or [])
    out: List[str] = []
    for x in t:
        if isinstance(x, str) and x and x not in out:
            out.append(x)
    return out[:40]


# ── experiment lifecycle ───────────────────────────────────────────────


def request_experiment(db: Session, *, settings: SocietySettings, promotion: CodePromotion, candidate: CodeCandidate, agent: Optional[Agent], causation, source_run_id: Optional[uuid.UUID]) -> tuple[ChangeExperiment, bool]:
    """Create a PLANNED experiment with the TRUSTED criteria snapshot. Idempotent
    per (promotion, candidate sha) while non-terminal. Flushes, does not commit."""
    existing = (
        db.query(ChangeExperiment)
        .filter(ChangeExperiment.promotion_id == promotion.id, ChangeExperiment.candidate_sha == candidate.head_sha)
        .order_by(ChangeExperiment.created_at.desc())
        .first()
    )
    if existing is not None:
        return existing, False
    exp = ChangeExperiment(
        id=uuid.uuid4(),
        candidate_id=candidate.id,
        promotion_id=promotion.id,
        correlation_id=candidate.correlation_id,
        baseline_sha=candidate.base_sha,
        candidate_sha=candidate.head_sha,
        environment="offline",
        evaluation_mode="offline",
        status=ExperimentStatus.PLANNED,
        criteria_snapshot=json.loads(json.dumps(TRUSTED_CRITERIA)),
        requested_by_agent_id=agent.id if agent else None,
        evidence={"requested_by": agent.name if agent else "system", "targets": _targets(candidate)},
    )
    db.add(exp)
    db.flush()
    emit_event(db, event_type=EventType.EXPERIMENT_REQUESTED, payload={"experiment_id": str(exp.id), "promotion_id": str(promotion.id), "candidate_id": str(candidate.id), "mode": "offline"}, actor_type="agent" if agent else "system", actor_id=agent.id if agent else None, subject_type="change_experiment", subject_id=exp.id, causation=causation, correlation_id=candidate.correlation_id, idempotency_key=f"experiment:{exp.id}:requested", source_run_id=source_run_id, notify=True)
    return exp, True


_CLAIM_SQL = text(
    """
    WITH candidate AS (
        SELECT id FROM change_experiments
        WHERE status IN ('planned','baseline','candidate','evaluating')
          AND (lease_expires_at IS NULL OR lease_expires_at < :now)
        ORDER BY created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE change_experiments e
       SET worker_id = :worker_id, lease_expires_at = :lease_until, attempt = e.attempt + 1
      FROM candidate
     WHERE e.id = candidate.id
 RETURNING e.id
    """
)


def claim_next_experiment(db: Session, *, worker_id: str, lease_seconds: int) -> Optional[ChangeExperiment]:
    now = utcnow()
    row = db.execute(_CLAIM_SQL, {"now": now, "worker_id": worker_id, "lease_until": now + timedelta(seconds=lease_seconds)}).fetchone()
    db.commit()
    if row is None:
        return None
    exp = db.query(ChangeExperiment).filter(ChangeExperiment.id == row[0]).first()
    if exp is not None:
        db.refresh(exp)
    return exp


def _base_worktree(settings: SocietySettings, base_sha: str) -> pathlib.Path:
    repo_root = pathlib.Path(settings.repo_root).resolve()
    ws_root = pathlib.Path(settings.workspace_root).resolve()
    ws_root.mkdir(parents=True, exist_ok=True)
    path = ws_root / f"fitness-base-{base_sha[:12]}"
    if not (path / ".git").exists():
        ws_mod._git(["worktree", "prune"], cwd=repo_root)
        ws_mod._git(["worktree", "add", "--detach", str(path), base_sha], cwd=repo_root, timeout=120)
    return path


def _remove_base_worktree(settings: SocietySettings, path: pathlib.Path) -> None:
    repo_root = pathlib.Path(settings.repo_root).resolve()
    try:
        ws_mod._git(["worktree", "remove", "--force", str(path)], cwd=repo_root)
    except ws_mod.WorkspaceError:
        ws_mod._git(["worktree", "prune"], cwd=repo_root)


def _compare(criteria: Dict[str, Any], base: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
    deltas: Dict[str, Any] = {}
    for key, rule in criteria["soft"].items():
        group, name = key.split(".", 1)
        b = float((base.get(group) or {}).get(name) or 0)
        c = float((cand.get(group) or {}).get(name) or 0)
        delta = c - b
        hib = rule.get("higher_is_better", True)
        good = delta if hib else -delta
        verdict = "neutral"
        if "regress_abs_over" in rule:
            if c > float(rule["regress_abs_over"]):
                verdict = "regression"
        elif "regress_ratio" in rule:
            ratio = (delta / b) if b else (1.0 if delta > 0 else 0.0)
            if (-good if hib else good) < 0:  # candidate worse
                worse_abs = abs(delta)
                worse_ratio = abs(ratio)
                if worse_ratio >= rule["regress_ratio"] and worse_abs >= rule.get("regress_min_abs", 0):
                    verdict = "regression"
            else:
                better_abs = abs(delta)
                better_ratio = abs(ratio)
                if better_ratio >= rule.get("improve_ratio", 1e9) and better_abs >= rule.get("improve_min_abs", 0):
                    verdict = "improvement"
        else:
            if good >= float(rule.get("improve", 1e9)):
                verdict = "improvement"
            elif -good >= float(rule.get("regress", 1e9)):
                verdict = "regression"
        deltas[key] = {"baseline": b, "candidate": c, "delta": round(delta, 6), "verdict": verdict}
    return deltas


def evaluate_offline(db: Session, *, settings: SocietySettings, exp: ChangeExperiment, candidate: CodeCandidate, promotion: Optional[CodePromotion]) -> ChangeExperiment:
    """Run the offline experiment end to end on a claimed row. Commits between
    phases (crash → resumable); the decision is computed ONLY from the
    persisted criteria snapshot (never re-read from a candidate file)."""
    criteria = exp.criteria_snapshot or TRUSTED_CRITERIA
    targets = _targets(candidate)
    exp.started_at = exp.started_at or utcnow()
    try:
        cand_ws = ws_mod.ensure_workspace(settings, candidate.id)
        changed = ws_mod.changed_files(cand_ws)
        diff = ws_mod.diff_text(cand_ws, max_chars=400_000)
    except ws_mod.WorkspaceError as exc:
        return _finish(db, settings, exp, candidate, promotion, decision="fail", confidence="low", gates=[{"gate": "candidate_workspace", "passed": False, "detail": str(exc)[:300]}], deltas={}, base_metrics={}, cand_metrics={})
    # ── baseline ──
    exp.status = ExperimentStatus.BASELINE
    db.commit()
    base_path = _base_worktree(settings, exp.baseline_sha or candidate.base_sha or ws_mod.main_branch_head(settings))
    try:
        base_tests = _run_pytest(base_path, targets, timeout=settings.fitness_test_timeout_seconds)
        base_metrics = {
            "correctness": {"tests_passed": base_tests.passed, "tests_failed": base_tests.failed + base_tests.errors, "tests_collected": base_tests.collected},
            "reliability": {"timeouts": int(base_tests.timed_out), "exit_code": base_tests.exit_code},
            "safety": {"secret_findings": 0, "risky_primitives": 0, "never_findings": 0, "metric_lines_removed": 0},
            "economics": {"diff_lines": 0, "files_changed": 0, "model_cost_usd": 0.0},
            "performance": {"test_duration_s": base_tests.duration_s},
            "autonomy": {"qa_attempts": 0, "intents_denied": 0, "loop_breaks": 0, "runs_dead": 0},
            "_tests": base_tests.to_dict(),
        }
    finally:
        _remove_base_worktree(settings, base_path)
    exp.baseline_metrics = base_metrics
    exp.status = ExperimentStatus.CANDIDATE
    db.commit()
    # ── candidate ──
    cand_tests = _run_pytest(cand_ws.path, targets, timeout=settings.fitness_test_timeout_seconds)
    safety = _diff_safety(diff)
    risk = assess_risk(changed, diff, spec_kind=str((candidate.spec or {}).get("kind") or ""))
    corr = _correlation_costs(db, candidate.correlation_id)
    cand_metrics = {
        "correctness": {"tests_passed": cand_tests.passed, "tests_failed": cand_tests.failed + cand_tests.errors, "tests_collected": cand_tests.collected},
        "reliability": {"timeouts": int(cand_tests.timed_out), "exit_code": cand_tests.exit_code},
        "safety": {**safety, "never_findings": len(risk.never_findings)},
        "economics": {"diff_lines": int(candidate.diff_lines or 0), "files_changed": len(changed), "model_cost_usd": corr["model_cost_usd"]},
        "performance": {"test_duration_s": cand_tests.duration_s},
        "autonomy": {"qa_attempts": int((candidate.qa_report or {}).get("attempts") or 0), "intents_denied": corr["intents_denied"], "loop_breaks": corr["loop_breaks"], "runs_dead": corr["runs_dead"]},
        "_tests": cand_tests.to_dict(),
    }
    exp.candidate_metrics = cand_metrics
    exp.status = ExperimentStatus.EVALUATING
    db.commit()
    # ── hard gates ──
    regressed = sorted(t for t, s in base_tests.per_test.items() if s == "passed" and cand_tests.per_test.get(t) in ("failed", "error"))
    removed = sorted(t for t in base_tests.per_test if t not in cand_tests.per_test)
    gates = [
        {"gate": "risk_tier_not_never", "passed": risk.tier != RiskTier.NEVER, "detail": risk.tier.value},
        {"gate": "no_test_regression", "passed": not regressed, "detail": "; ".join(regressed[:5]) or "no test that passed on base fails on the candidate"},
        {"gate": "no_test_removal", "passed": not removed and cand_tests.collected >= base_tests.collected, "detail": "; ".join(removed[:5]) or f"{cand_tests.collected} tests collected (base {base_tests.collected})"},
        {"gate": "no_security_regression", "passed": safety["secret_findings"] == 0 and safety["risky_primitives"] == 0, "detail": json.dumps(safety)},
        {"gate": "no_never_findings", "passed": not risk.never_findings, "detail": "; ".join(risk.never_findings[:5]) or "none"},
        {"gate": "no_metric_collection_disabled", "passed": safety["metric_lines_removed"] == 0, "detail": f"{safety['metric_lines_removed']} metric line(s) removed"},
        {"gate": "candidate_tests_completed", "passed": not cand_tests.timed_out and cand_tests.exit_code in (0, 1) and cand_tests.collected > 0, "detail": cand_tests.tail[-300:]},
    ]
    gates = [g for g in gates if g["gate"] in set(criteria.get("hard_gates") or [])] or gates
    hard_ok = all(g["passed"] for g in gates)
    deltas = _compare(criteria, base_metrics, cand_metrics)
    improvements = [k for k, v in deltas.items() if v["verdict"] == "improvement"]
    regressions = [k for k, v in deltas.items() if v["verdict"] == "regression"]
    if not hard_ok:
        decision = "fail"
    elif regressions:
        decision = "fail"
    elif improvements:
        decision = "pass"
    else:
        decision = "inconclusive"
    confidence = "high" if (not base_tests.timed_out and not cand_tests.timed_out and base_tests.exit_code in (0, 1, 5) and cand_tests.exit_code in (0, 1)) else "low"
    return _finish(db, settings, exp, candidate, promotion, decision=decision, confidence=confidence, gates=gates, deltas=deltas, base_metrics=base_metrics, cand_metrics=cand_metrics, improvements=improvements, regressions=regressions)


def _finish(db: Session, settings: SocietySettings, exp: ChangeExperiment, candidate: CodeCandidate, promotion: Optional[CodePromotion], *, decision: str, confidence: str, gates, deltas, base_metrics, cand_metrics, improvements=(), regressions=()) -> ChangeExperiment:
    exp.hard_gate_results = list(gates)
    exp.metric_deltas = deltas
    exp.baseline_metrics = base_metrics or exp.baseline_metrics
    exp.candidate_metrics = cand_metrics or exp.candidate_metrics
    exp.decision = decision
    exp.confidence = confidence
    exp.status = {"pass": ExperimentStatus.PASS, "fail": ExperimentStatus.FAIL}.get(decision, ExperimentStatus.INCONCLUSIVE)
    merged = promotion is not None and getattr(promotion.status, "value", promotion.status) == PromotionStatus.MERGED.value
    exp.rollback_recommended = bool(decision == "fail" and merged)
    exp.evidence = {**(exp.evidence or {}), "improvements": list(improvements), "regressions": list(regressions), "criteria_version": (exp.criteria_snapshot or {}).get("version")}
    exp.finished_at = utcnow()
    exp.worker_id = None
    exp.lease_expires_at = None
    db.flush()
    cause = db.query(SocietyEvent).filter(SocietyEvent.subject_type == "change_experiment", SocietyEvent.subject_id == exp.id).order_by(SocietyEvent.created_at.desc()).first()
    emit_event(db, event_type=EventType.EXPERIMENT_FINISHED, causation=cause, payload={"experiment_id": str(exp.id), "promotion_id": str(exp.promotion_id) if exp.promotion_id else None, "candidate_id": str(candidate.id), "title": candidate.title, "decision": decision, "confidence": confidence, "hard_gates_failed": [g["gate"] for g in gates if not g["passed"]], "improvements": list(improvements), "regressions": list(regressions), "rollback_recommended": exp.rollback_recommended, "mode": exp.evaluation_mode}, actor_type="system", subject_type="change_experiment", subject_id=exp.id, correlation_id=candidate.correlation_id, idempotency_key=f"experiment:{exp.id}:finished", notify=True)
    if exp.rollback_recommended and promotion is not None:
        cause = db.query(SocietyEvent).filter(SocietyEvent.subject_type == "change_experiment", SocietyEvent.subject_id == exp.id).order_by(SocietyEvent.created_at.desc()).first()
        emit_event(db, event_type=EventType.ROLLBACK_RECOMMENDED, causation=cause, payload={"experiment_id": str(exp.id), "promotion_id": str(promotion.id), "merged_sha": promotion.merged_sha, "previous_good_sha": promotion.previous_good_sha, "reason": [g["gate"] for g in gates if not g["passed"]] + list(regressions)}, actor_type="system", subject_type="code_promotion", subject_id=promotion.id, correlation_id=candidate.correlation_id, idempotency_key=f"rollback:{exp.id}", notify=True)
    # Durable, TRUSTED memory of the outcome (validated provenance).
    mem = MemoryItem(
        id=uuid.uuid4(),
        agent_id=None,
        scope=MemoryScope.SOCIETY,
        title=f"Fitness {decision.upper()}: {candidate.title}"[:255],
        content=(f"Experiment {exp.id} ({exp.evaluation_mode}) for candidate {candidate.id} decided {decision} with {confidence} confidence. Failed gates: {[g['gate'] for g in gates if not g['passed']]}. Improvements: {list(improvements)}. Regressions: {list(regressions)}. Rollback recommended: {exp.rollback_recommended}.")[:4000],
        tags=["fitness", decision, "experiment"],
        importance=80 if decision == "fail" else 65,
        source_type="experiment",
        source_id=exp.id,
        correlation_id=candidate.correlation_id,
        author_agent_id=None,
        confidence=90 if confidence == "high" else 55,
        validation_state="validated",
    )
    db.add(mem)
    db.commit()
    return exp


def process_experiments(db_factory, *, settings: SocietySettings, worker_id: str, max_items: int = 5) -> Dict[str, int]:
    stats: Dict[str, int] = {}
    for _ in range(max_items):
        db = db_factory()
        try:
            exp = claim_next_experiment(db, worker_id=worker_id, lease_seconds=settings.fitness_test_timeout_seconds * 2 + 60)
            if exp is None:
                break
            candidate = db.query(CodeCandidate).filter(CodeCandidate.id == exp.candidate_id).first()
            promotion = db.query(CodePromotion).filter(CodePromotion.id == exp.promotion_id).first() if exp.promotion_id else None
            try:
                if candidate is None:
                    _finish(db, settings, exp, CodeCandidate(id=exp.candidate_id, title="?", correlation_id=exp.correlation_id), promotion, decision="fail", confidence="low", gates=[{"gate": "candidate_exists", "passed": False, "detail": "missing"}], deltas={}, base_metrics={}, cand_metrics={})
                    result = "fail"
                elif int(exp.attempt or 0) > 3:
                    _finish(db, settings, exp, candidate, promotion, decision="inconclusive", confidence="low", gates=[{"gate": "attempts", "passed": False, "detail": "attempt budget exhausted"}], deltas={}, base_metrics={}, cand_metrics={})
                    result = "inconclusive"
                else:
                    evaluate_offline(db, settings=settings, exp=exp, candidate=candidate, promotion=promotion)
                    result = exp.decision or "?"
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.exception("experiment %s crashed", exp.id)
                exp = db.merge(exp)
                exp.error = f"{type(exc).__name__}: {exc}"[:2000]
                exp.worker_id = None
                exp.lease_expires_at = None
                db.commit()
                result = "error"
            stats[result] = stats.get(result, 0) + 1
        finally:
            db.close()
    return stats


__all__ = ["TRUSTED_CRITERIA", "CRITERIA_VERSION", "request_experiment", "claim_next_experiment", "evaluate_offline", "process_experiments", "TestOutcome"]
