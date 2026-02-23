import os
import logging
import asyncio
import time
import uuid
import json
import redis.asyncio as redis
import httpx
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from .database import SessionLocal, engine
from .models import (
    TaskSession, TaskStatus, Transaction, TransactionStatus, TransactionType,
    Agent, AgentStatus, Wallet, CurrencyType
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
            json.dumps({
                "user_id": str(user_id),
                "message": message,
                "type": "task_timeout"
            })
        )
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")

async def process_timed_out_tasks(db: Session, redis_client):
    """Process timed out tasks and issue refunds."""
    with tracer.start_as_current_span("process_timed_out_tasks"):
        # Get all tasks that are in_progress and have timed out
        now = datetime.utcnow()
        timed_out_tasks = db.query(TaskSession).filter(
            and_(
                TaskSession.status.in_([TaskStatus.INITIATED, TaskStatus.IN_PROGRESS]),
                TaskSession.timeout_at < now
            )
        ).all()
        
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
                    transaction = db.query(Transaction).filter(
                        and_(
                            Transaction.task_session_id == task.id,
                            Transaction.status == TransactionStatus.PENDING
                        )
                    ).first()
                    
                    if transaction:
                        # Cancel the pending transaction
                        transaction.status = TransactionStatus.CANCELLED
                        transaction.completed_at = now
                        db.commit()
                        
                        # Get the caller's wallet
                        caller_wallet = db.query(Wallet).join(
                            Agent, Agent.id == task.caller_agent_id
                        ).filter(
                            Wallet.owner_type == "agent",
                            Wallet.owner_id == task.caller_agent_id
                        ).first()
                        
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
                            logger.warning(f"Agent {callee_agent.id} ({callee_agent.name}) suspended due to too many timeouts")
                        
                        db.commit()
                    
                    # Send notifications
                    if redis_client:
                        # Get the caller agent's user
                        caller_agent = db.query(Agent).filter(Agent.id == task.caller_agent_id).first()
                        
                        if caller_agent and caller_agent.user_id:
                            await send_notification(
                                redis_client,
                                caller_agent.user_id,
                                f"Task {task.id} with {callee_agent.name if callee_agent else 'unknown agent'} timed out. Your escrow has been refunded."
                            )
                        
                        # Get the callee agent's user
                        if callee_agent and callee_agent.user_id:
                            await send_notification(
                                redis_client,
                                callee_agent.user_id,
                                f"Your agent {callee_agent.name} has a task that timed out. Your timeout count is now {callee_agent.timeout_count}."
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
    
    while True:
        try:
            # Create a new database session for each iteration
            db = SessionLocal()
            
            try:
                # Process timed out tasks
                await process_timed_out_tasks(db, redis_client)
                
                # Check if we need to reset daily metrics (once per day)
                now = datetime.utcnow()
                if (now - last_reset_time).days >= 1:
                    await reset_daily_metrics(db)
                    last_reset_time = now
                
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