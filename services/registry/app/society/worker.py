"""Society runtime worker: the closed loop.

    durable event -> dispatch -> claim (lease) -> context -> model ->
    typed intents -> policy -> execute -> new events -> complete -> sleep

Run as ``python -m app.society.worker`` inside the registry image (it
shares the registry's models, task_service and alembic-managed schema).
Multiple replicas are safe: dispatch and claim use ``FOR UPDATE SKIP
LOCKED`` and the lease protocol in runs.py.

Wake semantics: the worker blocks on Postgres ``LISTEN society_wake``
(NOTIFY is emitted transactionally by ``emit_event``) with a bounded poll
fallback (``SOCIETY_WAKE_POLL_SECONDS``) for lease expiry / retry
deadlines. It does not busy-poll.

Crash safety: a run's intents are persisted (policy-adjudicated) BEFORE
any is executed. If the worker dies mid-run, the re-claimed run skips the
model call and resumes the persisted, still-pending intents; executed
ones are never re-run (execution_status), and events they emitted carry
intent-derived idempotency keys.
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import (
    Agent,
    AgentCapabilityGrant,
    AgentIntent,
    AgentRun,
    AgentRunStatus,
    IntentExecutionStatus,
    PolicyDecision,
    SocietyEvent,
    Span,
    SpanStatus,
)
from .cognition import CognitiveModel, ModelTimeout, get_model
from .config import SocietySettings, get_settings
from .context import build_context
from .events import WAKE_CHANNEL, EventType, emit_event, utcnow
from .executor import ExecContext, ExecutionError, execute
from .intents import DecisionValidationError, ValidatedIntent, payload_to_dict, validate_intents
from .policy import check_run_budget, evaluate_intent
from .roles import load_role_definitions, subscriptions_by_event
from .runs import (
    claim_next_run,
    complete_run,
    dispatch_pending_events,
    extend_lease,
    fail_run,
    mark_running,
    next_wake_deadline,
    pending_work_exists,
    skip_run,
)

logger = logging.getLogger(__name__)

# ── metrics (optional dependency) ─────────────────────────────────────
try:  # pragma: no cover - metrics plumbing
    from prometheus_client import Counter, Gauge

    def _counter(name, doc, labels=()):
        try:
            return Counter(name, doc, list(labels))
        except ValueError:  # already registered (module reloaded)
            from prometheus_client import REGISTRY

            return REGISTRY._names_to_collectors[name]  # type: ignore[attr-defined]

    def _gauge(name, doc):
        try:
            return Gauge(name, doc)
        except ValueError:
            from prometheus_client import REGISTRY

            return REGISTRY._names_to_collectors[name]  # type: ignore[attr-defined]

    M_RUNS = _counter("agentnet_society_runs_total", "Agent runs by terminal status", ["status"])
    M_INTENTS = _counter("agentnet_society_intents_total", "Intents by policy decision", ["decision"])
    M_INTENT_EXEC = _counter("agentnet_society_intent_executions_total", "Intent executions by result", ["result"])
    M_EVENTS = _counter("agentnet_society_events_dispatched_total", "Events dispatched to runs")
    M_DUPES = _counter("agentnet_society_duplicates_prevented_total", "Duplicate runs prevented by UNIQUE(agent,event)")
    M_LOOP = _counter("agentnet_society_loop_breaker_total", "Loop breaker activations")
    M_COST = _counter("agentnet_society_model_cost_usd_total", "Accumulated model cost (USD)")
    M_PENDING = _gauge("agentnet_society_pending_events", "Pending society events at last dispatch")
except Exception:  # pragma: no cover

    class _N:  # noqa: D401 - null metric
        def labels(self, **_):
            return self

        def inc(self, *_):
            return None

        def set(self, *_):
            return None

    M_RUNS = M_INTENTS = M_INTENT_EXEC = M_EVENTS = M_DUPES = M_LOOP = M_COST = M_PENDING = _N()


@dataclass
class CycleStats:
    dispatched_events: int = 0
    runs_created: int = 0
    runs_processed: int = 0
    runs_completed: int = 0
    runs_skipped: int = 0
    runs_failed: int = 0
    runs_dead: int = 0
    intents_executed: int = 0
    intents_denied: int = 0
    intents_invalid: int = 0
    intents_failed: int = 0
    duplicates_prevented: int = 0
    loop_breaks: int = 0
    cycles: int = 0
    processed_run_ids: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        d["processed_run_ids"] = list(self.processed_run_ids)
        return d


class WakeListener:
    """Blocking LISTEN on the wake channel using a dedicated autocommit
    connection. ``wait`` returns True when a notification arrived."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn = None

    def _connect(self):
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

        conn = psycopg2.connect(self.dsn)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()
        cur.execute(f"LISTEN {WAKE_CHANNEL}")
        self.conn = conn

    def wait(self, timeout: float) -> bool:
        try:
            if self.conn is None or self.conn.closed:
                self._connect()
            ready = select.select([self.conn], [], [], max(0.0, timeout))
            if not ready[0]:
                return False
            self.conn.poll()
            got = bool(self.conn.notifies)
            del self.conn.notifies[:]
            return got
        except Exception as exc:  # noqa: BLE001
            logger.warning("wake listener error (%s); falling back to poll", exc)
            try:
                if self.conn is not None:
                    self.conn.close()
            finally:
                self.conn = None
            time.sleep(min(timeout, 1.0))
            return False

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None


class SocietyWorker:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        settings: Optional[SocietySettings] = None,
        model: Optional[CognitiveModel] = None,
        roles=None,
        worker_id: Optional[str] = None,
        listen_dsn: Optional[str] = None,
    ):
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.model = model or get_model(self.settings)
        self.roles = roles or load_role_definitions()
        self.routing = subscriptions_by_event(self.roles)
        self.worker_id = worker_id or self.settings.worker_id
        self.listener = WakeListener(listen_dsn) if listen_dsn else None
        self._stop = False

    # ── dispatch ───────────────────────────────────────────────────────

    def dispatch(self, stats: Optional[CycleStats] = None) -> None:
        db = self.session_factory()
        try:
            ds = dispatch_pending_events(db, settings=self.settings, routing=self.routing)
        finally:
            db.close()
        M_EVENTS.inc(ds.events_dispatched)
        M_DUPES.inc(ds.duplicates_prevented)
        M_LOOP.inc(ds.loop_breaks)
        if stats is not None:
            stats.dispatched_events += ds.events_dispatched
            stats.runs_created += ds.runs_created
            stats.duplicates_prevented += ds.duplicates_prevented
            stats.loop_breaks += ds.loop_breaks

    # ── one run ────────────────────────────────────────────────────────

    async def _decide(self, context) -> Any:
        return await asyncio.wait_for(self.model.decide(context), timeout=self.settings.model_timeout_seconds)

    def _persist_decision(self, db: Session, run: AgentRun, agent: Agent, grant: AgentCapabilityGrant, context, response) -> List[AgentIntent]:
        run.model_provider = response.provider
        run.model_name = response.model_name
        run.prompt_version = self.settings.prompt_version
        run.context_digest = context.digest()
        run.context_summary = context.summary()
        run.decision_summary = response.decision.decision_summary[:1000]
        run.tokens_in = response.tokens_in
        run.tokens_out = response.tokens_out
        run.cost_usd = Decimal(str(response.cost_usd or 0))
        run.sleep_until = utcnow() + timedelta(seconds=int(response.decision.sleep_for_seconds or 0))
        validated = validate_intents(response.decision, run.id)
        rows: List[AgentIntent] = []
        max_intents = min(int(grant.max_intents_per_run), self.settings.max_intents_per_run)
        for v in validated:
            verdict = evaluate_intent(v, grant=grant, settings=self.settings, agent=agent)
            decision = verdict.decision
            reason = verdict.reason
            if v.seq >= max_intents and decision == PolicyDecision.ALLOW:
                decision = PolicyDecision.DENY
                reason = f"exceeds max_intents_per_run ({max_intents})"
            exec_status = {
                PolicyDecision.ALLOW: IntentExecutionStatus.PENDING,
                PolicyDecision.DENY: IntentExecutionStatus.DENIED,
                PolicyDecision.INVALID: IntentExecutionStatus.DENIED,
                PolicyDecision.APPROVAL_REQUIRED: IntentExecutionStatus.AWAITING_APPROVAL,
            }[decision]
            row = AgentIntent(
                id=uuid.uuid4(),
                run_id=run.id,
                agent_id=agent.id,
                seq=v.seq,
                intent_type=v.type_name,
                payload=payload_to_dict(v.payload) if v.valid else payload_to_dict(v.raw_payload),
                idempotency_key=v.idempotency_key,
                risk_class=verdict.risk,
                policy_decision=decision,
                policy_reason=reason[:2000],
                execution_status=exec_status,
                result={},
            )
            db.add(row)
            rows.append(row)
            M_INTENTS.labels(decision=decision.value).inc()
            if decision in (PolicyDecision.DENY, PolicyDecision.INVALID):
                emit_event(
                    db,
                    event_type=EventType.INTENT_DENIED,
                    payload={"run_id": str(run.id), "agent": agent.name, "intent_type": v.type_name, "reason": reason[:300], "risk": verdict.risk.value},
                    actor_type="agent",
                    actor_id=agent.id,
                    correlation_id=run.correlation_id,
                    idempotency_key=f"denied:{v.idempotency_key}",
                    source_run_id=run.id,
                    notify=False,
                )
            elif decision == PolicyDecision.APPROVAL_REQUIRED:
                emit_event(
                    db,
                    event_type=EventType.INTENT_APPROVAL_REQUIRED,
                    payload={"run_id": str(run.id), "agent": agent.name, "intent_type": v.type_name, "intent_id": str(row.id)},
                    actor_type="agent",
                    actor_id=agent.id,
                    correlation_id=run.correlation_id,
                    idempotency_key=f"approval:{v.idempotency_key}",
                    notify=False,
                )
        run.intents_count = len(rows)
        db.commit()
        M_COST.inc(float(run.cost_usd or 0))
        return rows

    def _execute_pending_intents(self, db: Session, run: AgentRun, agent: Agent, grant: AgentCapabilityGrant, event: SocietyEvent, stats: Optional[CycleStats]) -> None:
        pending = (
            db.query(AgentIntent)
            .filter(AgentIntent.run_id == run.id, AgentIntent.execution_status == IntentExecutionStatus.PENDING)
            .order_by(AgentIntent.seq)
            .all()
        )
        for row in pending:
            validated = self._revalidate(row, run)
            verdict = evaluate_intent(validated, grant=grant, settings=self.settings, agent=agent)
            if not verdict.allowed:
                row.execution_status = IntentExecutionStatus.DENIED
                row.policy_decision = verdict.decision
                row.policy_reason = f"re-check at execution: {verdict.reason}"[:2000]
                db.commit()
                if stats:
                    stats.intents_denied += 1
                continue
            started = time.monotonic()
            ctx = ExecContext(
                db=db,
                settings=self.settings,
                agent=agent,
                grant=grant,
                run=run,
                event=event,
                intent_row=row,
                validated=validated,
                heartbeat=lambda: extend_lease(db, run, lease_seconds=self.settings.run_lease_seconds),
            )
            try:
                outcome = execute(ctx)
                row.execution_status = IntentExecutionStatus.EXECUTED
                row.result = {"result": outcome.result, "events": outcome.events}
                row.executed_at = utcnow()
                self._span(db, run, agent, f"society.intent.{row.intent_type}", SpanStatus.SUCCESS, started, {"intent_id": str(row.id)})
                db.commit()
                M_INTENT_EXEC.labels(result="executed").inc()
                if stats:
                    stats.intents_executed += 1
            except ExecutionError as exc:
                db.rollback()
                row = db.merge(row)
                row.execution_status = IntentExecutionStatus.FAILED
                row.error = str(exc)[:2000]
                row.executed_at = utcnow()
                self._span(db, run, agent, f"society.intent.{row.intent_type}", SpanStatus.FAILED, started, {"intent_id": str(row.id), "error": str(exc)[:200]})
                db.commit()
                M_INTENT_EXEC.labels(result="failed").inc()
                if stats:
                    stats.intents_failed += 1
                logger.warning("intent %s (%s) refused: %s", row.id, row.intent_type, exc)
            except Exception as exc:  # noqa: BLE001 — never let one intent kill the run
                db.rollback()
                row = db.merge(row)
                row.execution_status = IntentExecutionStatus.FAILED
                row.error = f"{type(exc).__name__}: {exc}"[:2000]
                row.executed_at = utcnow()
                self._span(db, run, agent, f"society.intent.{row.intent_type}", SpanStatus.FAILED, started, {"intent_id": str(row.id), "error": str(exc)[:200]})
                db.commit()
                M_INTENT_EXEC.labels(result="error").inc()
                if stats:
                    stats.intents_failed += 1
                logger.exception("intent %s (%s) crashed", row.id, row.intent_type)
            # keep the lease fresh between intents
            extend_lease(db, run, lease_seconds=self.settings.run_lease_seconds)

    @staticmethod
    def _revalidate(row: AgentIntent, run: AgentRun) -> ValidatedIntent:
        from .intents import PAYLOAD_MODELS, IntentType

        try:
            itype = IntentType(row.intent_type)
            payload = PAYLOAD_MODELS[itype].model_validate(row.payload or {})
            return ValidatedIntent(seq=row.seq, type_name=row.intent_type, intent_type=itype, valid=True, payload=payload, raw_payload=row.payload or {}, idempotency_key=row.idempotency_key)
        except Exception as exc:  # noqa: BLE001
            return ValidatedIntent(seq=row.seq, type_name=row.intent_type, intent_type=None, valid=False, error=str(exc)[:500], raw_payload=row.payload or {}, idempotency_key=row.idempotency_key)

    @staticmethod
    def _span(db: Session, run: AgentRun, agent: Agent, event_name: str, status: SpanStatus, started_monotonic: float, extra: Dict[str, Any]) -> None:
        db.add(
            Span(
                id=uuid.uuid4(),
                trace_id=run.trace_id or run.correlation_id,
                span_id=uuid.uuid4(),
                parent_span_id=run.span_id,
                agent_id=agent.id,
                event=event_name,
                capability="society",
                duration_ms=int((time.monotonic() - started_monotonic) * 1000),
                status=status,
                extra_data={"run_id": str(run.id), "correlation_id": str(run.correlation_id), **extra},
            )
        )

    async def process_run(self, run_id: uuid.UUID, stats: Optional[CycleStats] = None) -> str:
        db = self.session_factory()
        started = time.monotonic()
        try:
            run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
            if run is None:
                return "missing"
            agent = db.query(Agent).filter(Agent.id == run.agent_id).first()
            grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == run.agent_id).first()
            event = db.query(SocietyEvent).filter(SocietyEvent.id == run.event_id).first()
            if agent is None or event is None:
                skip_run(db, run, "agent or event missing")
                M_RUNS.labels(status="skipped").inc()
                return "skipped"
            if run.attempt > run.max_attempts:
                fail_run(db, run, "attempt budget exhausted (lease expired repeatedly)", settings=self.settings, retryable=False)
                M_RUNS.labels(status="dead").inc()
                if stats:
                    stats.runs_dead += 1
                return "dead"
            gate = check_run_budget(db, agent=agent, grant=grant, settings=self.settings, run=run)
            if not gate.ok:
                skip_run(db, run, gate.reason)
                M_RUNS.labels(status="skipped").inc()
                if stats:
                    stats.runs_skipped += 1
                return "skipped"
            mark_running(db, run)

            resumed = db.query(AgentIntent.id).filter(AgentIntent.run_id == run.id).first() is not None
            if not resumed:
                context = build_context(db, agent=agent, grant=grant, event=event, run=run, settings=self.settings)
                try:
                    response = await self._decide(context)
                except (asyncio.TimeoutError, ModelTimeout) as exc:
                    status = fail_run(db, run, f"model timeout: {exc}", settings=self.settings)
                    return self._count_fail(status, stats)
                except DecisionValidationError as exc:
                    status = fail_run(db, run, f"invalid structured output: {exc}", settings=self.settings)
                    return self._count_fail(status, stats)
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    run = db.merge(run)
                    status = fail_run(db, run, f"model provider error: {type(exc).__name__}: {exc}", settings=self.settings)
                    return self._count_fail(status, stats)
                self._persist_decision(db, run, agent, grant, context, response)
            else:
                logger.info("run %s resumed with persisted intents (attempt %s)", run.id, run.attempt)

            self._execute_pending_intents(db, run, agent, grant, event, stats)
            self._span(db, run, agent, "society.run", SpanStatus.SUCCESS, started, {"event_type": event.event_type, "intents": run.intents_count, "resumed": resumed})
            complete_run(db, run)
            M_RUNS.labels(status="completed").inc()
            if stats:
                stats.runs_completed += 1
            return "completed"
        except Exception as exc:  # noqa: BLE001 — worker-level safety net
            logger.exception("run %s crashed in worker", run_id)
            db.rollback()
            run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
            if run is not None:
                status = fail_run(db, run, f"worker error: {type(exc).__name__}: {exc}", settings=self.settings)
                return self._count_fail(status, stats)
            return "error"
        finally:
            if stats:
                stats.runs_processed += 1
                stats.processed_run_ids.append(str(run_id))
            db.close()

    @staticmethod
    def _count_fail(status: AgentRunStatus, stats: Optional[CycleStats]) -> str:
        if status == AgentRunStatus.DEAD:
            M_RUNS.labels(status="dead").inc()
            if stats:
                stats.runs_dead += 1
            return "dead"
        M_RUNS.labels(status="retry").inc()
        if stats:
            stats.runs_failed += 1
        return "requeued"

    # ── loops ──────────────────────────────────────────────────────────

    async def process_claimable(self, stats: Optional[CycleStats] = None, *, max_runs: int = 100) -> int:
        processed = 0
        while processed < max_runs and not self._stop:
            db = self.session_factory()
            try:
                run = claim_next_run(db, worker_id=self.worker_id, lease_seconds=self.settings.run_lease_seconds)
                run_id = run.id if run else None
            finally:
                db.close()
            if run_id is None:
                break
            await self.process_run(run_id, stats)
            processed += 1
        return processed

    async def run_until_idle(self, *, max_cycles: int = 50, max_runs: int = 200, wait_for_backoff: bool = True) -> CycleStats:
        """Drive the loop until no pending events/claimable runs remain.
        Used by tests and the demo; the same code path as run_forever."""
        stats = CycleStats()
        total = 0
        for _ in range(max_cycles):
            stats.cycles += 1
            self.dispatch(stats)
            n = await self.process_claimable(stats, max_runs=max_runs - total)
            total += n
            db = self.session_factory()
            try:
                if pending_work_exists(db):
                    continue
                deadline = next_wake_deadline(db) if wait_for_backoff else None
            finally:
                db.close()
            if deadline is None:
                break
            delay = (deadline - utcnow()).total_seconds()
            if delay > 0:
                await asyncio.sleep(min(delay, 30) + 0.05)
        return stats

    def stop(self) -> None:
        self._stop = True

    async def run_forever(self) -> None:
        logger.info("society worker %s starting (enabled=%s provider=%s)", self.worker_id, self.settings.runtime_enabled, self.settings.model_provider)
        while not self._stop:
            settings = get_settings()  # re-read cheap flags each loop
            self.settings = settings
            if not settings.runtime_enabled:
                await asyncio.sleep(max(settings.wake_poll_seconds, 15))
                continue
            try:
                self.dispatch()
                await self.process_claimable()
            except Exception:  # noqa: BLE001
                logger.exception("society worker loop error")
                await asyncio.sleep(settings.wake_poll_seconds)
                continue
            db = self.session_factory()
            try:
                if pending_work_exists(db):
                    continue
                M_PENDING.set(0)
                deadline = next_wake_deadline(db)
            finally:
                db.close()
            timeout = float(settings.wake_poll_seconds)
            if deadline is not None:
                timeout = max(0.1, min(timeout * 6, (deadline - utcnow()).total_seconds()))
            if self.listener is not None:
                await asyncio.to_thread(self.listener.wait, timeout)
            else:
                await asyncio.sleep(timeout)
        if self.listener is not None:
            self.listener.close()


def main() -> None:  # pragma: no cover — process entrypoint
    from ..config import DATABASE_URL
    from ..database import SessionLocal
    from ..logging_config import setup_logging

    setup_logging("society-worker")
    settings = get_settings()
    port = int(os.getenv("SOCIETY_METRICS_PORT", "0") or 0)
    if port:
        try:
            from prometheus_client import start_http_server

            start_http_server(port)
            logger.info("society metrics on :%s/metrics", port)
        except Exception as exc:  # noqa: BLE001
            logger.warning("metrics server not started: %s", exc)
    worker = SocietyWorker(SessionLocal, settings=settings, listen_dsn=DATABASE_URL)
    if not settings.runtime_enabled:
        logger.warning("SOCIETY_RUNTIME_ENABLED=false — worker idles until enabled")
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        logger.info("society worker stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
