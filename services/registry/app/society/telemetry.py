"""Trusted world-telemetry producers: meaningful internal metrics become
``platform.metric.anomaly`` events with threshold + cooldown + idempotency.

Each producer reads persisted state only, emits at most one event per
(metric, bucket) and only when a threshold is crossed. Nothing here creates
"activity for activity's sake": a quiet system produces no events, and a
metric that stays anomalous produces one event per cooldown window, not one
per cycle. Every payload carries the evidence a Scout needs (baseline,
observed value, window, sample size) so a proposal can cite it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    Agent,
    AgentCapabilityGrant,
    AgentRun,
    AgentRunStatus,
    CodeCandidate,
    CodeCandidateStatus,
    CodePromotion,
    ImprovementProposal,
    ProposalStatus,
    SocietyEvent,
    TaskSession,
    TaskStatus,
    Transaction,
    TransactionType,
)
from .config import SocietySettings
from .events import EventType, emit_event, utcnow

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_COOLDOWN_SECONDS = 3600
MIN_SAMPLE = 5


@dataclass
class Anomaly:
    metric: str
    observed: float
    threshold: float
    baseline: float
    sample_size: int
    window_seconds: int
    description: str
    severity_score: int = 60
    subject_type: Optional[str] = None
    subject_id: Optional[object] = None


def _bucket(now: datetime, cooldown: int) -> int:
    return int(now.timestamp()) // max(1, cooldown)


def _task_failure_rate(db: Session, since: datetime) -> Optional[Anomaly]:
    total = db.query(func.count(TaskSession.id)).filter(TaskSession.created_at >= since).scalar() or 0
    if total < MIN_SAMPLE:
        return None
    failed = db.query(func.count(TaskSession.id)).filter(TaskSession.created_at >= since, TaskSession.status.in_([TaskStatus.FAILED, TaskStatus.TIMEOUT])).scalar() or 0
    rate = failed / total
    if rate < 0.25:
        return None
    return Anomaly("task_failure_rate", rate, 0.25, 0.05, int(total), DEFAULT_WINDOW_SECONDS, f"{failed}/{total} tasks failed or timed out in the last hour", severity_score=min(95, 50 + int(rate * 50)))


def _dead_runs(db: Session, since: datetime) -> Optional[Anomaly]:
    total = db.query(func.count(AgentRun.id)).filter(AgentRun.created_at >= since).scalar() or 0
    dead = db.query(func.count(AgentRun.id)).filter(AgentRun.created_at >= since, AgentRun.status == AgentRunStatus.DEAD).scalar() or 0
    if total < MIN_SAMPLE or dead == 0:
        return None
    rate = dead / total
    if rate < 0.2 and dead < 3:
        return None
    return Anomaly("run_dead_rate", rate, 0.2, 0.0, int(total), DEFAULT_WINDOW_SECONDS, f"{dead}/{total} agent runs died (retries exhausted) in the last hour", severity_score=70)


def _retry_rate(db: Session, since: datetime) -> Optional[Anomaly]:
    total = db.query(func.count(AgentRun.id)).filter(AgentRun.created_at >= since).scalar() or 0
    retried = db.query(func.count(AgentRun.id)).filter(AgentRun.created_at >= since, AgentRun.attempt > 1).scalar() or 0
    if total < MIN_SAMPLE:
        return None
    rate = retried / total
    if rate < 0.4:
        return None
    return Anomaly("run_retry_rate", rate, 0.4, 0.05, int(total), DEFAULT_WINDOW_SECONDS, f"{retried}/{total} runs needed more than one attempt", severity_score=55)


def _loop_breaker_rate(db: Session, since: datetime) -> Optional[Anomaly]:
    n = db.query(func.count(SocietyEvent.id)).filter(SocietyEvent.created_at >= since, SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED).scalar() or 0
    if n < 3:
        return None
    return Anomaly("loop_breaker_rate", float(n), 3.0, 0.0, int(n), DEFAULT_WINDOW_SECONDS, f"loop breaker tripped {n} times in the last hour", severity_score=65)


def _model_cost(db: Session, since: datetime, settings: SocietySettings) -> Optional[Anomaly]:
    spend = Decimal(str(db.query(func.coalesce(func.sum(AgentRun.cost_usd), 0)).filter(AgentRun.created_at >= since).scalar() or 0))
    runs = db.query(func.count(AgentRun.id)).filter(AgentRun.created_at >= since, AgentRun.cost_usd > 0).scalar() or 0
    threshold = settings.daily_model_budget_usd / Decimal(4)
    if runs < MIN_SAMPLE or spend < threshold:
        return None
    return Anomaly("model_cost_per_hour_usd", float(spend), float(threshold), 0.0, int(runs), DEFAULT_WINDOW_SECONDS, f"model spend {spend} USD in the last hour exceeds a quarter of the daily budget", severity_score=60)


def _escrow_anomaly(db: Session, since: datetime) -> Optional[Anomaly]:
    refunds = db.query(func.count(Transaction.id)).filter(Transaction.created_at >= since, Transaction.type == TransactionType.REFUND).scalar() or 0
    payments = db.query(func.count(Transaction.id)).filter(Transaction.created_at >= since, Transaction.type == TransactionType.PAYMENT).scalar() or 0
    if payments < MIN_SAMPLE:
        return None
    rate = refunds / payments
    if rate < 0.5:
        return None
    return Anomaly("escrow_refund_rate", rate, 0.5, 0.1, int(payments), DEFAULT_WINDOW_SECONDS, f"{refunds}/{payments} escrow payments were refunded in the last hour", severity_score=70)


def _candidate_rejections(db: Session, since: datetime) -> Optional[Anomaly]:
    total = db.query(func.count(CodeCandidate.id)).filter(CodeCandidate.updated_at >= since, CodeCandidate.status.in_([CodeCandidateStatus.READY, CodeCandidateStatus.REJECTED, CodeCandidateStatus.FAILED])).scalar() or 0
    rejected = db.query(func.count(CodeCandidate.id)).filter(CodeCandidate.updated_at >= since, CodeCandidate.status.in_([CodeCandidateStatus.REJECTED, CodeCandidateStatus.FAILED])).scalar() or 0
    if total < 3 or rejected < 3:
        return None
    rate = rejected / total
    if rate < 0.6:
        return None
    return Anomaly("candidate_rejection_rate", rate, 0.6, 0.2, int(total), DEFAULT_WINDOW_SECONDS, f"{rejected}/{total} candidates were rejected in the last hour", severity_score=60)


def _agent_inactivity(db: Session, since: datetime) -> Optional[Anomaly]:
    cutoff = utcnow() - timedelta(days=1)
    rows = (
        db.query(Agent.name)
        .join(AgentCapabilityGrant, AgentCapabilityGrant.agent_id == Agent.id)
        .filter(AgentCapabilityGrant.enabled.is_(True))
        .filter(~db.query(AgentRun.id).filter(AgentRun.agent_id == Agent.id, AgentRun.created_at >= cutoff).exists())
        .filter(Agent.created_at < cutoff)
        .all()
    )
    if not rows:
        return None
    names = sorted(r[0] for r in rows)
    return Anomaly("agent_inactivity", float(len(names)), 1.0, 0.0, len(names), 86400, f"{len(names)} society agent(s) had no run in 24h: {names[:6]}", severity_score=40)


def _repeated_proposals(db: Session, since: datetime) -> Optional[Anomaly]:
    rows = db.query(ImprovementProposal.title, func.count(ImprovementProposal.id)).filter(ImprovementProposal.created_at >= since).group_by(ImprovementProposal.title).having(func.count(ImprovementProposal.id) >= 3).all()
    if not rows:
        return None
    return Anomaly("repeated_proposals", float(max(n for _, n in rows)), 3.0, 1.0, int(sum(n for _, n in rows)), DEFAULT_WINDOW_SECONDS, f"{len(rows)} proposal title(s) repeated 3+ times in the last hour (busywork)", severity_score=50)


def _ci_failures(db: Session, since: datetime) -> Optional[Anomaly]:
    failed = db.query(func.count(CodePromotion.id)).filter(CodePromotion.updated_at >= since, CodePromotion.ci_state == "failed").scalar() or 0
    if failed < 2:
        return None
    return Anomaly("promotion_ci_failures", float(failed), 2.0, 0.0, int(failed), DEFAULT_WINDOW_SECONDS, f"{failed} autonomous promotions failed CI in the last hour", severity_score=65)


PRODUCERS: Dict[str, Callable] = {
    "task_failure_rate": lambda db, since, s: _task_failure_rate(db, since),
    "run_dead_rate": lambda db, since, s: _dead_runs(db, since),
    "run_retry_rate": lambda db, since, s: _retry_rate(db, since),
    "loop_breaker_rate": lambda db, since, s: _loop_breaker_rate(db, since),
    "model_cost_per_hour_usd": lambda db, since, s: _model_cost(db, since, s),
    "escrow_refund_rate": lambda db, since, s: _escrow_anomaly(db, since),
    "candidate_rejection_rate": lambda db, since, s: _candidate_rejections(db, since),
    "agent_inactivity": lambda db, since, s: _agent_inactivity(db, since),
    "repeated_proposals": lambda db, since, s: _repeated_proposals(db, since),
    "promotion_ci_failures": lambda db, since, s: _ci_failures(db, since),
}


def produce_anomalies(db: Session, settings: SocietySettings, *, now: Optional[datetime] = None, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS, only: Optional[List[str]] = None) -> int:
    """Evaluate every producer once; emit at most one event per metric per
    cooldown bucket. Commits. Returns the number of NEW events."""
    now = now or utcnow()
    since = now - timedelta(seconds=DEFAULT_WINDOW_SECONDS)
    created = 0
    for name, fn in PRODUCERS.items():
        if only and name not in only:
            continue
        try:
            anomaly = fn(db, since, settings)
        except Exception:  # noqa: BLE001 — one broken producer must not stop the others
            db.rollback()
            logger.exception("telemetry producer %s failed", name)
            continue
        if anomaly is None:
            continue
        key = f"telemetry:{anomaly.metric}:{_bucket(now, cooldown_seconds)}"
        ev = emit_event(
            db,
            event_type=EventType.PLATFORM_METRIC_ANOMALY,
            payload={
                "metric": anomaly.metric,
                "value": round(anomaly.observed, 6),
                "threshold": anomaly.threshold,
                "baseline": anomaly.baseline,
                "sample_size": anomaly.sample_size,
                "window_seconds": anomaly.window_seconds,
                "description": anomaly.description,
                "severity_score": anomaly.severity_score,
                "source": "telemetry",
                "observed_at": now.isoformat(),
            },
            actor_type="system",
            idempotency_key=key,
        )
        if not getattr(ev, "deduplicated", False):
            created += 1
    if created:
        db.commit()
    return created


__all__ = ["Anomaly", "PRODUCERS", "produce_anomalies"]
