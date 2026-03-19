import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta

import httpx
import redis.asyncio as redis
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .database import SessionLocal, engine
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
    TransactionType,
    Wallet,
)
from .tracing import configure_tracing, get_tracer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Configure tracing
tracer_provider = configure_tracing(engine)
tracer = get_tracer(__name__)

# Redis configuration
REDIS_URL = f"redis://:{os.getenv('REDIS_PASSWORD', 'your_redis_password')}@{os.getenv('REDIS_HOST', 'redis')}:{os.getenv('REDIS_PORT', '6379')}/0"

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
    """Process timed out tasks and issue refunds."""
    with tracer.start_as_current_span("process_timed_out_tasks"):
        # Get all tasks that are in_progress and have timed out
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

        for task in timed_out_tasks:
            with tracer.start_as_current_span("process_task", attributes={"task_id": str(task.id)}):
                try:
                    # Update task status to timeout
                    task.status = TaskStatus.TIMEOUT
                    task.refund_at = now
                    task.error_message = "Task timed out"
                    db.commit()

                    # Get the pending transaction for this task
                    transaction = (
                        db.query(Transaction)
                        .filter(
                            and_(
                                Transaction.task_session_id == task.id,
                                Transaction.status == TransactionStatus.PENDING,
                            )
                        )
                        .first()
                    )

                    if transaction:
                        # Cancel the pending transaction
                        transaction.status = TransactionStatus.CANCELLED
                        transaction.completed_at = now
                        db.commit()

                        # Get the caller's wallet
                        caller_wallet = (
                            db.query(Wallet)
                            .join(Agent, Agent.id == task.caller_agent_id)
                            .filter(
                                Wallet.owner_type == "agent",
                                Wallet.owner_id == task.caller_agent_id,
                            )
                            .first()
                        )

                        if caller_wallet:
                            # Release the reserved funds back to the caller
                            if task.currency == CurrencyType.CREDITS:
                                caller_wallet.reserved_credits -= task.escrow_amount
                            else:
                                caller_wallet.reserved_usdc -= task.escrow_amount

                            db.commit()

                    # Increment callee's timeout count
                    callee_agent = db.query(Agent).filter(Agent.id == task.callee_agent_id).first()

                    if callee_agent:
                        callee_agent.timeout_count += 1

                        # If timeout_count is too high, suspend the agent
                        if callee_agent.timeout_count >= 5:
                            callee_agent.status = AgentStatus.SUSPENDED
                            logger.warning(
                                f"Agent {callee_agent.id} ({callee_agent.name}) suspended due to too many timeouts"
                            )

                        db.commit()

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
                    logger.error(f"Error processing timed out task {task.id}: {e}")
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
                logger.info(
                    f"Found {len(stuck_sims)} timed-out simulation task sessions"
                )

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


async def main():
    """Main worker loop."""
    logger.info("Auto-Refund Worker started")

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

                # Crawl agent cards every hour
                if (now - last_crawl_time).total_seconds() >= 3600:
                    await crawl_agent_cards(db)
                    last_crawl_time = now

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
