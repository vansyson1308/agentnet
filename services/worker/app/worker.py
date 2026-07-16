import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta

import httpx
import redis.asyncio as redis
from prometheus_client import Counter, Gauge, start_http_server
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .database import SessionLocal, engine
from .logging_config import setup_logging
from .models import (
    Agent,
    AgentStatus,
    CurrencyType,
    Span,
    SpanStatus,
    TaskSession,
    TaskStatus,
    Transaction,
    TransactionStatus,
    Wallet,
)
from .reflection_loop import convert_proposals_to_backlog, run_reflection_loop
from .tracing import configure_tracing, get_tracer

# ---------------------------------------------------------------------------
# Prometheus metrics for the worker. Worker has no HTTP framework so we
# expose them via the prometheus_client built-in server (default port 9100).
# ---------------------------------------------------------------------------
worker_loop_iterations_total = Counter(
    "agentnet_worker_loop_iterations_total",
    "Number of main-loop iterations the worker has completed.",
)
worker_timed_out_tasks_total = Counter(
    "agentnet_worker_timed_out_tasks_total",
    "Tasks transitioned to TIMEOUT and refunded.",
    ["currency"],
)
worker_refund_failures_total = Counter(
    "agentnet_worker_refund_failures_total",
    "Errors raised inside process_timed_out_tasks.",
)
worker_pending_tasks = Gauge(
    "agentnet_worker_pending_tasks",
    "Tasks the worker just observed in INITIATED/IN_PROGRESS state past timeout.",
)
WORKER_METRICS_PORT = int(os.getenv("WORKER_METRICS_PORT", "9100"))

# Configure structured logging — JSON in prod, console in dev.
setup_logging("worker")
logger = logging.getLogger(__name__)

# Configure tracing
tracer_provider = configure_tracing(engine)
tracer = get_tracer(__name__)

# Redis configuration — fail-fast in non-dev if REDIS_PASSWORD is missing
from .config import REDIS_URL  # noqa: E402

# Telegram Bot API URL (if needed)
TELEGRAM_BOT_API_URL = os.getenv("TELEGRAM_BOT_API_URL", "http://telegram-bot:8002")

# Registry API URL
REGISTRY_API_URL = os.getenv("REGISTRY_API_URL", "http://registry:8000")


async def init_redis():
    """Initialize Redis connection."""
    return await redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)


async def send_notification(redis_client, user_id, message):
    """Send a notification to a user via Redis."""
    if not redis_client:
        return

    try:
        await redis_client.publish(
            "notifications",
            json.dumps({"user_id": str(user_id), "message": message, "type": "task_timeout"}),
        )
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")


async def process_timed_out_tasks(db: Session, redis_client):
    """Process timed-out tasks and issue refunds atomically.

    Single-transaction per task: status flip, reserved release, transaction
    cancel, span insert all under one commit. Row-level FOR UPDATE locks
    serialize against REST/WS callee paths so a concurrent confirm and
    timeout cannot both decrement reserved_*.
    """
    with tracer.start_as_current_span("process_timed_out_tasks"):
        now = datetime.utcnow()
        timed_out_tasks = (
            db.query(TaskSession)
            .filter(
                and_(
                    TaskSession.status.in_([TaskStatus.INITIATED, TaskStatus.IN_PROGRESS]),
                    TaskSession.timeout_at < now,
                )
            )
            .all()
        )

        logger.info(f"Found {len(timed_out_tasks)} timed out tasks")
        worker_pending_tasks.set(len(timed_out_tasks))

        for task_preview in timed_out_tasks:
            with tracer.start_as_current_span("process_task", attributes={"task_id": str(task_preview.id)}):
                try:
                    # Re-load with FOR UPDATE so the rest of this iteration
                    # can mutate state safely; abort if API/WS already moved
                    # the task to a terminal status.
                    task = db.query(TaskSession).filter(TaskSession.id == task_preview.id).with_for_update().first()
                    if task is None:
                        continue
                    if task.status not in (TaskStatus.INITIATED, TaskStatus.IN_PROGRESS):
                        logger.info(f"Task {task.id} already in terminal state {task.status}, skipping")
                        db.rollback()
                        continue

                    transaction = (
                        db.query(Transaction)
                        .filter(
                            and_(
                                Transaction.task_session_id == task.id,
                                Transaction.status == TransactionStatus.PENDING,
                            )
                        )
                        .with_for_update()
                        .first()
                    )
                    caller_wallet = (
                        db.query(Wallet)
                        .filter(
                            Wallet.owner_type == "agent",
                            Wallet.owner_id == task.caller_agent_id,
                        )
                        .with_for_update()
                        .first()
                    )

                    if transaction is not None and caller_wallet is not None:
                        if task.currency == CurrencyType.CREDITS:
                            if caller_wallet.reserved_credits < task.escrow_amount:
                                # Should never happen post-FOR UPDATE; bail loudly.
                                raise RuntimeError(
                                    f"reserved_credits underflow for wallet {caller_wallet.id} "
                                    f"(reserved={caller_wallet.reserved_credits}, "
                                    f"refund={task.escrow_amount})"
                                )
                            caller_wallet.reserved_credits -= task.escrow_amount
                        else:
                            reserved = float(caller_wallet.reserved_usdc)
                            if reserved < task.escrow_amount:
                                raise RuntimeError(f"reserved_usdc underflow for wallet {caller_wallet.id}")
                            caller_wallet.reserved_usdc = reserved - task.escrow_amount

                        transaction.status = TransactionStatus.CANCELLED
                        transaction.completed_at = now

                    task.status = TaskStatus.TIMEOUT
                    task.refund_at = now
                    task.error_message = "Task timed out"
                    task.completed_at = now

                    duration_ms = int((now - task.created_at).total_seconds() * 1000)
                    db.add(
                        Span(
                            id=uuid.uuid4(),
                            trace_id=task.trace_id,
                            span_id=uuid.uuid4(),
                            parent_span_id=task.span_id,
                            agent_id=task.callee_agent_id,
                            event="task_timeout",
                            capability=task.capability,
                            duration_ms=duration_ms,
                            status=SpanStatus.TIMEOUT,
                            extra_data={"error_message": "Task timed out"},
                        )
                    )

                    # Increment callee's timeout count in the same txn so
                    # we cannot end up with a refunded task whose callee
                    # never had its reputation/metric debited.
                    callee_agent = db.query(Agent).filter(Agent.id == task.callee_agent_id).with_for_update().first()
                    if callee_agent:
                        callee_agent.timeout_count += 1
                        if callee_agent.timeout_count >= 5:
                            callee_agent.status = AgentStatus.SUSPENDED
                            logger.error(
                                f"Agent {callee_agent.id} ({callee_agent.name}) " f"SUSPENDED due to too many timeouts"
                            )

                    db.commit()
                    worker_timed_out_tasks_total.labels(
                        currency=task.currency.value if hasattr(task.currency, "value") else str(task.currency)
                    ).inc()
                    logger.warning(f"Escrow {task.escrow_amount} refunded for timed-out task {task.id}")

                    # Send notifications
                    if redis_client:
                        # Get the caller agent's user
                        caller_agent = db.query(Agent).filter(Agent.id == task.caller_agent_id).first()

                        if caller_agent and caller_agent.user_id:
                            await send_notification(
                                redis_client,
                                caller_agent.user_id,
                                f"Task {task.id} with {callee_agent.name if callee_agent else 'unknown agent'} timed out. Your escrow has been refunded.",
                            )

                        # Get the callee agent's user
                        if callee_agent and callee_agent.user_id:
                            await send_notification(
                                redis_client,
                                callee_agent.user_id,
                                f"Your agent {callee_agent.name} has a task that timed out. Your timeout count is now {callee_agent.timeout_count}.",
                            )

                    logger.info(f"Processed timed out task {task.id}")

                except Exception as e:
                    logger.error(f"Error processing timed out task {task_preview.id}: {e}")
                    worker_refund_failures_total.inc()
                    db.rollback()


async def reset_daily_metrics(db: Session):
    """Reset daily metrics for all agents."""
    with tracer.start_as_current_span("reset_daily_metrics"):
        try:
            # Reset timeout_count for all agents
            db.query(Agent).update({"timeout_count": 0})

            # Reset daily_spent for all wallets
            db.query(Wallet).update({"daily_spent": 0, "daily_reset_at": datetime.utcnow()})

            db.commit()
            logger.info("Daily metrics reset")
        except Exception as e:
            logger.error(f"Error resetting daily metrics: {e}")
            db.rollback()


def _compute_reputation_tier(success_rate: float, total_completed: int, avg_response_time_ms: int) -> str:
    """Compute reputation tier from metrics."""
    if total_completed >= 50 and success_rate >= 0.95 and avg_response_time_ms < 2000:
        return "diamond"
    elif total_completed >= 25 and success_rate >= 0.90 and avg_response_time_ms < 5000:
        return "gold"
    elif total_completed >= 10 and success_rate >= 0.80:
        return "silver"
    elif total_completed >= 5 and success_rate >= 0.60:
        return "bronze"
    return "unranked"


async def update_all_reputations(db: Session):
    """Recompute reputation for all agents. Runs every 5 minutes."""
    with tracer.start_as_current_span("update_all_reputations"):
        try:
            agents = db.query(Agent).all()
            updated = 0

            for agent in agents:
                # Count tasks by status (as callee)
                task_counts = (
                    db.query(TaskSession.status, func.count(TaskSession.id))
                    .filter(TaskSession.callee_agent_id == agent.id)
                    .group_by(TaskSession.status)
                    .all()
                )
                counts = {str(s.value) if hasattr(s, "value") else str(s): c for s, c in task_counts}

                completed = counts.get("completed", 0)
                failed = counts.get("failed", 0)
                timeout = counts.get("timeout", 0)
                total = completed + failed + timeout
                success_rate = completed / total if total > 0 else 0.0

                # Average response time
                avg_time = (
                    db.query(func.avg(Span.duration_ms))
                    .filter(Span.agent_id == agent.id, Span.duration_ms.isnot(None))
                    .scalar()
                )
                avg_ms = int(avg_time) if avg_time else 0

                # Total volume
                volume = (
                    db.query(func.sum(TaskSession.escrow_amount))
                    .filter(TaskSession.callee_agent_id == agent.id, TaskSession.status == TaskStatus.COMPLETED)
                    .scalar()
                )
                total_volume = int(volume) if volume else 0

                # Update agent
                agent.total_tasks_completed = completed
                agent.total_tasks_failed = failed
                agent.total_tasks_timeout = timeout
                agent.success_rate = round(success_rate, 4)
                agent.avg_response_time_ms = avg_ms
                agent.total_volume_credits = total_volume
                agent.reputation_tier = _compute_reputation_tier(success_rate, completed, avg_ms)
                agent.reputation_updated_at = datetime.utcnow()
                updated += 1

            db.commit()
            logger.info(f"Updated reputation for {updated} agents")

        except Exception as e:
            logger.error(f"Error updating reputations: {e}")
            db.rollback()


async def process_timed_out_simulations(db: Session, redis_client):
    """
    Check for simulations that have exceeded their timeout and mark them as failed.

    Invariant: Does NOT modify wallet tables. Escrow refund is handled
    via task_session timeout (existing process_timed_out_tasks logic).
    """
    with tracer.start_as_current_span("process_timed_out_simulations"):
        try:
            from .models import TaskSession as TS

            now = datetime.utcnow()
            timeout_seconds = int(os.getenv("SIMULATION_TIMEOUT_SECONDS", "3600"))
            cutoff = now - timedelta(seconds=timeout_seconds)

            # Find running simulations that started before the cutoff
            # We query task_sessions with capability 'swarm_simulation' that are still active
            stuck_sims = (
                db.query(TS)
                .filter(
                    and_(
                        TS.capability == "swarm_simulation",
                        TS.status.in_([TaskStatus.INITIATED, TaskStatus.IN_PROGRESS]),
                        TS.created_at < cutoff,
                    )
                )
                .all()
            )

            if stuck_sims:
                logger.info(f"Found {len(stuck_sims)} timed-out simulation task sessions")

            for task in stuck_sims:
                try:
                    task.status = TaskStatus.TIMEOUT
                    task.error_message = "Simulation timed out"
                    task.refund_at = now
                    db.commit()

                    if redis_client:
                        await redis_client.publish(
                            "simulation_updates",
                            json.dumps(
                                {
                                    "simulation_id": str(task.id),
                                    "user_id": "",
                                    "progress_pct": -1,
                                    "message": "Simulation timed out",
                                    "status": "timeout",
                                }
                            ),
                        )

                    logger.info(f"Marked simulation task {task.id} as timed out")
                except Exception as e:
                    logger.error(f"Error timing out simulation task {task.id}: {e}")
                    db.rollback()

        except Exception as e:
            logger.debug(f"Simulation timeout check skipped: {e}")


async def crawl_agent_cards(db: Session):
    """
    Passive Discovery Crawler (Phase 3C).

    Periodically fetches /.well-known/agent-card.json from all
    registered agent endpoints to detect capability changes.

    Invariant: Only updates capabilities. Never touches wallet/escrow.
    """
    with tracer.start_as_current_span("crawl_agent_cards"):
        agents = db.query(Agent).filter(Agent.endpoint.isnot(None)).all()
        updated = 0

        for agent in agents:
            try:
                card_url = f"{agent.endpoint.rstrip('/')}/.well-known/agent-card.json"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(card_url)

                if resp.status_code != 200:
                    continue

                card = resp.json()
                if not card.get("skills"):
                    continue

                # Convert A2A skills to capabilities format
                new_capabilities = []
                for skill in card.get("skills", []):
                    new_capabilities.append(
                        {
                            "name": skill.get("id", skill.get("name", "unknown")),
                            "version": card.get("version", "1.0"),
                            "input_schema": {"type": "object"},
                            "output_schema": {"type": "object"},
                            "price": 0,
                        }
                    )

                # Only update if capabilities changed
                old_names = sorted(c.get("name") for c in (agent.capabilities or []))
                new_names = sorted(c.get("name") for c in new_capabilities)

                if old_names != new_names:
                    agent.capabilities = new_capabilities
                    updated += 1
                    logger.info(f"Updated capabilities for agent {agent.name}: {new_names}")

            except Exception as e:
                logger.debug(f"Could not crawl agent {agent.name}: {e}")

        if updated > 0:
            db.commit()
            logger.info(f"Crawler updated {updated} agent capabilities")


async def process_offline_agents(db: Session):
    """Mark agents as offline if they haven't sent a heartbeat in 60s
    and have no active WebSocket connection.

    This covers the case where an agent's heartbeat HTTP endpoint
    is called but the agent crashed without disconnecting.
    """
    with tracer.start_as_current_span("process_offline_agents"):
        try:
            cutoff = datetime.utcnow() - timedelta(seconds=60)
            affected = (
                db.query(Agent)
                .filter(
                    Agent.is_online.is_(True),
                    Agent.last_seen_at.isnot(None),
                    Agent.last_seen_at < cutoff,
                )
                .update(
                    {"is_online": False},
                    synchronize_session=False,
                )
            )
            if affected > 0:
                db.commit()
                logger.info(f"Auto-offline: marked {affected} agent(s) as offline (no heartbeat > 60s)")
        except Exception as e:
            logger.error(f"Error processing offline agents: {e}")
            db.rollback()


async def main():
    """Main worker loop."""
    logger.info("Auto-Refund Worker started")

    # Expose Prometheus metrics on a dedicated port — the worker has no
    # HTTP framework, so prometheus_client spawns its own daemon thread.
    # Fail hard if the port is taken: it almost always means an old
    # worker process is still alive (e.g. botched rolling redeploy)
    # and continuing would publish stale metrics. Letting the process
    # exit lets the orchestrator restart us and keeps metrics correct.
    try:
        start_http_server(WORKER_METRICS_PORT)
        logger.info(f"Prometheus metrics: http://0.0.0.0:{WORKER_METRICS_PORT}/metrics")
    except OSError as e:
        logger.error(
            f"Could not bind metrics port {WORKER_METRICS_PORT}: {e}. "
            f"Refusing to start — old worker process likely still alive."
        )
        raise

    # Initialize Redis
    redis_client = None
    try:
        redis_client = await init_redis()
        logger.info("Connected to Redis")
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")

    # Last time daily metrics were reset
    last_reset_time = datetime.utcnow()
    # Last time reputation was computed (every 5 minutes)
    last_reputation_time = datetime.utcnow()
    # Last time agent cards were crawled (every hour)
    last_crawl_time = datetime.utcnow()
    # Last time simulation timeouts were checked (every 60s)
    last_sim_timeout_time = datetime.utcnow()
    # Last time reflection loop generated improvement proposals
    # (Phase: agent-goals-and-self-improvement; default 5 min cadence)
    reflection_interval_sec = max(30, int(os.getenv("REFLECTION_LOOP_INTERVAL_SEC", "300")))
    last_reflection_time = datetime.utcnow() - timedelta(seconds=reflection_interval_sec)

    while True:
        try:
            # Create a new database session for each iteration
            db = SessionLocal()

            try:
                # Process timed out tasks
                await process_timed_out_tasks(db, redis_client)

                now = datetime.utcnow()

                # Check if we need to reset daily metrics (once per day)
                if (now - last_reset_time).days >= 1:
                    await reset_daily_metrics(db)
                    last_reset_time = now

                # Recompute reputation every 5 minutes
                if (now - last_reputation_time).total_seconds() >= 300:
                    await update_all_reputations(db)
                    last_reputation_time = now

                # Check simulation timeouts every 60 seconds
                if (now - last_sim_timeout_time).total_seconds() >= 60:
                    await process_timed_out_simulations(db, redis_client)
                    last_sim_timeout_time = now

                # Auto-offline: mark agents offline if no heartbeat > 60s (runs every loop ~30s)
                await process_offline_agents(db)

                # Crawl agent cards every hour
                if (now - last_crawl_time).total_seconds() >= 3600:
                    await crawl_agent_cards(db)
                    last_crawl_time = now

                # Reflection loop: turn fresh failed/timeout/refunded
                # tasks into ImprovementProposal rows for the lab.
                # Idempotent — never duplicates a proposal for a task.
                if (now - last_reflection_time).total_seconds() >= reflection_interval_sec:
                    try:
                        with tracer.start_as_current_span("reflection_loop"):
                            created = run_reflection_loop(db)
                        if created:
                            logger.info(f"reflection_loop: generated {created} proposals")
                        # YAML backlog export is legacy-only and disabled by
                        # default. Paperclip is the managed Goal/WorkItem SoT.
                        if os.getenv("LEGACY_BACKLOG_EXPORT_ENABLED", "false").lower() == "true":
                            backlogged = convert_proposals_to_backlog(db)
                            if backlogged:
                                logger.info(f"reflection_loop: converted {backlogged} proposals to backlog items")
                    except Exception as e:
                        logger.error(f"reflection_loop error: {e}")
                        db.rollback()
                    last_reflection_time = now

            finally:
                # Close the database session
                db.close()

        except Exception as e:
            logger.error(f"Error in main loop: {e}")

        # Sleep for 30 seconds
        await asyncio.sleep(30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker stopped by keyboard interrupt")
    except Exception as e:
        logger.error(f"Worker crashed: {e}")
        raise
