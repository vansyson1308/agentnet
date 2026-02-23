import json
import logging
from typing import Dict, Optional
import uuid
import redis.asyncio as redis
import os
import asyncio
from datetime import datetime, timedelta

from fastapi import WebSocket
from sqlalchemy.orm import Session
from sqlalchemy import func as sql_func

from .models import (
    Agent, AgentStatus, TaskSession, TaskStatus, CurrencyType,
    Wallet, WalletOwnerType, Transaction, TransactionStatus, TransactionType,
    Offer, OfferStatus, Referral, ReferralStatus
)
from .schemas import WebSocketMessage, WebSocketResponse
from .database import get_db
from .auth import verify_token, TokenData, hash_input

logger = logging.getLogger(__name__)

# Rate limit constants
EXECUTE_RATE_LIMIT = 100       # max transactions per hour per agent
OFFER_RATE_LIMIT = 10          # max offers per hour per agent
MAX_REFERRALS_PER_AGENT = 5    # max referrals per inviter
OFFER_ELIGIBILITY_MIN_TASKS = 10
OFFER_ELIGIBILITY_MIN_SUCCESS = 0.90
OFFER_ELIGIBILITY_MIN_QUALITY = 3.5
OFFER_TASK_RATIO_THRESHOLD = 0.80
APPROVAL_TTL_SECONDS = 300     # 5-minute approval timeout


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.agent_connections: Dict[str, str] = {}  # agent_id -> connection_id
        self.redis_client = None
        self.pubsub = None

    async def init_redis(self):
        """Initialize Redis connection and pubsub."""
        redis_url = (
            f"redis://:{os.getenv('REDIS_PASSWORD', 'your_redis_password')}"
            f"@{os.getenv('REDIS_HOST', 'redis')}:{os.getenv('REDIS_PORT', '6379')}/0"
        )
        self.redis_client = await redis.from_url(
            redis_url, encoding="utf-8", decode_responses=True
        )
        self.pubsub = self.redis_client.pubsub()
        await self.pubsub.subscribe("agent_messages")
        asyncio.create_task(self.listen_for_messages())

    async def listen_for_messages(self):
        """Listen for messages from Redis and forward them to WebSocket connections."""
        if not self.pubsub:
            return

        try:
            async for message in self.pubsub.listen():
                if message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        to_agent_id = data.get("to")

                        if to_agent_id and to_agent_id in self.agent_connections:
                            connection_id = self.agent_connections[to_agent_id]
                            if connection_id in self.active_connections:
                                websocket = self.active_connections[connection_id]
                                await websocket.send_json(data)
                    except json.JSONDecodeError:
                        logger.error(f"Failed to decode message: {message}")
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
        except Exception as e:
            logger.error(f"Error in Redis listener: {e}")
            await asyncio.sleep(5)
            await self.init_redis()

    async def connect(self, websocket: WebSocket, token: str, db: Session):
        """Accept a WebSocket connection and authenticate the agent."""
        await websocket.accept()

        try:
            token_data = verify_token(token)

            if token_data.agent_id is None:
                await websocket.close(code=1008, reason="Invalid authentication")
                return None

            agent = db.query(Agent).filter(Agent.id == token_data.agent_id).first()

            if agent is None:
                await websocket.close(code=1008, reason="Agent not found")
                return None

            connection_id = str(uuid.uuid4())
            self.active_connections[connection_id] = websocket
            self.agent_connections[str(agent.id)] = connection_id

            logger.info(f"Agent {agent.id} connected with connection ID {connection_id}")
            return connection_id

        except Exception as e:
            logger.error(f"Error connecting agent: {e}")
            await websocket.close(code=1008, reason="Authentication error")
            return None

    def disconnect(self, connection_id: str):
        """Disconnect a WebSocket connection."""
        if connection_id in self.active_connections:
            agent_id = None
            for aid, cid in self.agent_connections.items():
                if cid == connection_id:
                    agent_id = aid
                    break

            del self.active_connections[connection_id]

            if agent_id:
                del self.agent_connections[agent_id]
                logger.info(f"Agent {agent_id} disconnected")

    async def send_personal_message(self, message: dict, connection_id: str):
        """Send a message to a specific connection."""
        if connection_id in self.active_connections:
            await self.active_connections[connection_id].send_json(message)

    async def broadcast(self, message: dict):
        """Broadcast a message to all connected agents."""
        for connection in self.active_connections.values():
            await connection.send_json(message)

    async def send_to_agent(self, message: dict, agent_id: str):
        """Send a message to a specific agent, falling back to Redis pub/sub."""
        if agent_id in self.agent_connections:
            connection_id = self.agent_connections[agent_id]
            await self.send_personal_message(message, connection_id)
        else:
            if self.redis_client:
                await self.redis_client.publish("agent_messages", json.dumps(message))

    # ─── Rate Limiting Helpers ───────────────────────────────────────────

    async def _check_rate_limit(self, agent_id: str, action: str, limit: int) -> bool:
        """Sliding window rate limit via Redis. Returns True if within limit."""
        if not self.redis_client:
            return True  # No Redis → skip rate limiting (dev mode)

        key = f"rate:{action}:{agent_id}"
        now = datetime.utcnow().timestamp()
        window_start = now - 3600  # 1-hour window

        pipe = self.redis_client.pipeline()
        pipe.zremrangebyscore(key, "-inf", window_start)
        pipe.zadd(key, {str(uuid.uuid4()): now})
        pipe.zcard(key)
        pipe.expire(key, 3600)
        results = await pipe.execute()

        count = results[2]
        return count <= limit

    # ─── Wallet Helpers ──────────────────────────────────────────────────

    def _get_agent_wallet(self, db: Session, agent_id: str) -> Optional[Wallet]:
        """Get wallet for an agent."""
        return db.query(Wallet).filter(
            Wallet.owner_type == WalletOwnerType.AGENT,
            Wallet.owner_id == agent_id
        ).first()

    def _lock_escrow(self, db: Session, wallet: Wallet, amount: int) -> bool:
        """Reserve credits in wallet for escrow. Returns False if insufficient."""
        available = wallet.balance_credits - wallet.reserved_credits
        if available < amount:
            return False

        # Check spending cap
        if wallet.daily_spent + amount > wallet.spending_cap:
            return False

        wallet.reserved_credits += amount
        db.flush()
        return True

    def _release_escrow(self, db: Session, wallet: Wallet, amount: int):
        """Release reserved credits back to wallet."""
        wallet.reserved_credits = max(0, wallet.reserved_credits - amount)
        db.flush()

    # ─── Input Validation ────────────────────────────────────────────────

    def _validate_input(self, input_data: dict, capability: dict) -> Optional[str]:
        """Validate input against capability's input_schema. Returns error message or None."""
        input_schema = capability.get("input_schema")
        if not input_schema:
            return None

        try:
            import jsonschema
            jsonschema.validate(instance=input_data, schema=input_schema)
            return None
        except ImportError:
            logger.warning("jsonschema not installed, skipping input validation")
            return None
        except jsonschema.ValidationError as e:
            return f"Input validation failed: {e.message}"

    # ─── Message Dispatcher ──────────────────────────────────────────────

    async def handle_message(self, message: dict, sender_agent_id: str, db: Session):
        """Handle an incoming WebSocket message."""
        try:
            if "jsonrpc" not in message or message["jsonrpc"] != "2.0":
                return self._error(message.get("id"), -32600, "Invalid JSON-RPC format")

            if "id" not in message:
                return self._error(None, -32600, "Missing message ID")

            if "method" not in message:
                return self._error(message["id"], -32601, "Method not specified")

            method = message["method"]
            handlers = {
                "execute": self.handle_execute,
                "stream_execute": self.handle_stream_execute,
                "offer": self.handle_offer,
                "referral_invite": self.handle_referral_invite,
                "approve_payment": self.handle_approve_payment,
            }

            handler = handlers.get(method)
            if not handler:
                return self._error(message["id"], -32601, f"Method {method} not found")

            return await handler(message, sender_agent_id, db)

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            return self._error(message.get("id"), -32603, f"Internal error: {str(e)}")

    def _error(self, msg_id, code: int, message: str) -> dict:
        """Build a JSON-RPC error response."""
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def _result(self, msg_id, result: dict) -> dict:
        """Build a JSON-RPC success response."""
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # ─── execute ─────────────────────────────────────────────────────────

    async def handle_execute(self, message: dict, sender_agent_id: str, db: Session):
        """Handle an execute method call with full escrow locking."""
        msg_id = message["id"]

        # Validate required params
        params = message.get("params")
        if not params:
            return self._error(msg_id, -32602, "Missing parameters")

        for field in ("capability", "input", "payment"):
            if field not in params:
                return self._error(msg_id, -32602, f"Missing '{field}' parameter")

        if "to" not in message:
            return self._error(msg_id, -32602, "Missing 'to' field")

        # Rate limit check
        if not await self._check_rate_limit(sender_agent_id, "execute", EXECUTE_RATE_LIMIT):
            return self._error(msg_id, -32000, "Rate limit exceeded: max 100 transactions/hour")

        # Resolve callee agent
        callee_agent_id = message["to"]
        callee_agent = db.query(Agent).filter(Agent.id == callee_agent_id).first()

        if callee_agent is None:
            return self._error(msg_id, -32602, "Callee agent not found")

        if callee_agent.status != AgentStatus.ACTIVE:
            return self._error(msg_id, -32602, f"Callee agent is {callee_agent.status.value}, not active")

        # Find the requested capability
        requested_capability = params["capability"]
        capability_meta = None
        for cap in (callee_agent.capabilities or []):
            if cap["name"] == requested_capability:
                capability_meta = cap
                break

        if capability_meta is None:
            return self._error(msg_id, -32602, f"Callee does not have capability '{requested_capability}'")

        capability_price = capability_meta.get("price", 0)

        # Validate input against schema
        validation_err = self._validate_input(params["input"], capability_meta)
        if validation_err:
            return self._error(msg_id, -32602, validation_err)

        # Check payment
        payment = params["payment"]
        max_budget = payment.get("max_budget", 0)
        if max_budget < capability_price:
            return self._error(
                msg_id, -32602,
                f"Insufficient payment: capability costs {capability_price}, provided {max_budget}"
            )

        # ── Escrow locking (critical security fix) ──
        caller_wallet = self._get_agent_wallet(db, sender_agent_id)
        if caller_wallet is None:
            return self._error(msg_id, -32602, "Caller agent has no wallet")

        escrow_amount = int(capability_price)

        if not self._lock_escrow(db, caller_wallet, escrow_amount):
            available = caller_wallet.balance_credits - caller_wallet.reserved_credits
            return self._error(
                msg_id, -32602,
                f"Insufficient funds or spending cap exceeded. "
                f"Available: {available}, needed: {escrow_amount}, "
                f"daily spent: {caller_wallet.daily_spent}/{caller_wallet.spending_cap}"
            )

        # Compute real input hash
        input_hash = hash_input(params["input"])

        # Build task session
        trace_id = message.get("trace_id", str(uuid.uuid4()))
        span_id = str(uuid.uuid4())
        timeout_seconds = params.get("timeout_seconds", 300)
        timeout_at = datetime.utcnow() + timedelta(seconds=timeout_seconds)

        callee_wallet = self._get_agent_wallet(db, str(callee_agent_id))

        task_session = TaskSession(
            id=uuid.uuid4(),
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=message.get("parent_span_id"),
            caller_agent_id=sender_agent_id,
            callee_agent_id=callee_agent_id,
            capability=requested_capability,
            input_hash=input_hash,
            escrow_amount=escrow_amount,
            currency=CurrencyType.CREDITS,
            status=TaskStatus.INITIATED,
            timeout_at=timeout_at,
        )
        db.add(task_session)

        # Create pending escrow transaction
        transaction = Transaction(
            id=uuid.uuid4(),
            from_wallet=caller_wallet.id,
            to_wallet=callee_wallet.id if callee_wallet else None,
            amount=escrow_amount,
            currency=CurrencyType.CREDITS,
            status=TransactionStatus.PENDING,
            type=TransactionType.PAYMENT,
            task_session_id=task_session.id,
        )
        db.add(transaction)

        db.commit()
        db.refresh(task_session)

        # Forward to callee
        forward_message = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "trace_id": str(trace_id),
            "method": "execute",
            "from": sender_agent_id,
            "params": {
                **params,
                "payment": {
                    **payment,
                    "escrow_session_id": str(task_session.id),
                },
            },
        }
        await self.send_to_agent(forward_message, str(callee_agent_id))

        return self._result(msg_id, {
            "task_session_id": str(task_session.id),
            "trace_id": str(trace_id),
            "span_id": span_id,
            "escrow_amount": escrow_amount,
            "message": "Task session created with escrow locked",
        })

    # ─── stream_execute ──────────────────────────────────────────────────

    async def handle_stream_execute(self, message: dict, sender_agent_id: str, db: Session):
        """Handle stream_execute — same as execute but marks streaming flag."""
        # Reuse execute logic; the actual streaming is handled at the
        # transport layer (callee sends chunked responses via WS).
        message["params"] = message.get("params", {})
        message["params"]["_streaming"] = True
        return await self.handle_execute(message, sender_agent_id, db)

    # ─── offer ───────────────────────────────────────────────────────────

    async def handle_offer(self, message: dict, sender_agent_id: str, db: Session):
        """Handle an offer method call with eligibility checks and rate limiting."""
        msg_id = message["id"]
        params = message.get("params")

        if not params:
            return self._error(msg_id, -32602, "Missing parameters")

        for field in ("to_agent_id", "core_task_id", "title", "price"):
            if field not in params:
                return self._error(msg_id, -32602, f"Missing '{field}' parameter")

        # Rate limit: max 10 offers/hour
        if not await self._check_rate_limit(sender_agent_id, "offer", OFFER_RATE_LIMIT):
            return self._error(msg_id, -32000, "Rate limit exceeded: max 10 offers/hour")

        # ── Eligibility checks ──
        sender_agent = db.query(Agent).filter(Agent.id == sender_agent_id).first()
        if not sender_agent:
            return self._error(msg_id, -32602, "Sender agent not found")

        # Must have completed at least N core tasks
        completed_tasks = db.query(sql_func.count(TaskSession.id)).filter(
            TaskSession.callee_agent_id == sender_agent_id,
            TaskSession.status == TaskStatus.COMPLETED,
        ).scalar() or 0

        if completed_tasks < OFFER_ELIGIBILITY_MIN_TASKS:
            return self._error(
                msg_id, -32602,
                f"Must complete at least {OFFER_ELIGIBILITY_MIN_TASKS} tasks before sending offers "
                f"(current: {completed_tasks})"
            )

        # Must have ≥ 90% success rate
        total_tasks = db.query(sql_func.count(TaskSession.id)).filter(
            TaskSession.callee_agent_id == sender_agent_id,
            TaskSession.status.in_([
                TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMEOUT
            ]),
        ).scalar() or 1

        success_rate = completed_tasks / total_tasks
        if success_rate < OFFER_ELIGIBILITY_MIN_SUCCESS:
            return self._error(
                msg_id, -32602,
                f"Success rate too low: {success_rate:.1%} (minimum {OFFER_ELIGIBILITY_MIN_SUCCESS:.0%})"
            )

        # Quality score check
        if sender_agent.verify_score < OFFER_ELIGIBILITY_MIN_QUALITY:
            return self._error(
                msg_id, -32602,
                f"Quality score too low: {sender_agent.verify_score} "
                f"(minimum {OFFER_ELIGIBILITY_MIN_QUALITY})"
            )

        # Offer/task ratio penalty
        total_offers_7d = db.query(sql_func.count(Offer.id)).filter(
            Offer.from_agent_id == sender_agent_id,
            Offer.created_at >= datetime.utcnow() - timedelta(days=7),
        ).scalar() or 0

        total_tasks_7d = db.query(sql_func.count(TaskSession.id)).filter(
            TaskSession.callee_agent_id == sender_agent_id,
            TaskSession.created_at >= datetime.utcnow() - timedelta(days=7),
        ).scalar() or 1

        offer_rate = total_offers_7d / total_tasks_7d
        if offer_rate > OFFER_TASK_RATIO_THRESHOLD:
            # Penalize reputation
            sender_agent.verify_score = max(0, sender_agent.verify_score - 1)
            sender_agent.offer_rate_7d = offer_rate
            logger.warning(
                f"Agent {sender_agent_id} offer/task ratio {offer_rate:.2f} exceeds threshold, "
                f"verify_score decremented to {sender_agent.verify_score}"
            )

        # Validate target agent exists
        to_agent = db.query(Agent).filter(Agent.id == params["to_agent_id"]).first()
        if not to_agent:
            return self._error(msg_id, -32602, "Target agent not found")

        # Validate core task exists
        core_task = db.query(TaskSession).filter(TaskSession.id == params["core_task_id"]).first()
        if not core_task:
            return self._error(msg_id, -32602, "Core task not found")

        # Create offer
        expires_at = params.get("expires_at")
        if expires_at:
            expires_at = datetime.fromisoformat(expires_at)
        else:
            expires_at = datetime.utcnow() + timedelta(hours=24)

        offer = Offer(
            id=uuid.uuid4(),
            from_agent_id=sender_agent_id,
            to_agent_id=params["to_agent_id"],
            core_task_id=params["core_task_id"],
            title=params["title"],
            description=params.get("description"),
            price=params["price"],
            currency=CurrencyType(params.get("currency", "credits")),
            expires_at=expires_at,
            status=OfferStatus.PENDING,
            baseline_quality_score=sender_agent.verify_score,
        )
        db.add(offer)
        db.commit()
        db.refresh(offer)

        # Notify recipient via WS
        notification = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "notification",
            "params": {
                "type": "offer_received",
                "offer_id": str(offer.id),
                "from_agent_id": sender_agent_id,
                "title": offer.title,
                "price": offer.price,
                "expires_at": offer.expires_at.isoformat(),
            },
        }
        await self.send_to_agent(notification, str(params["to_agent_id"]))

        # Publish to Redis for Telegram bot
        if self.redis_client:
            await self.redis_client.publish("notifications", json.dumps({
                "type": "offer_received",
                "offer_id": str(offer.id),
                "to_agent_id": str(params["to_agent_id"]),
                "from_agent_id": sender_agent_id,
                "title": offer.title,
                "price": offer.price,
            }))

        return self._result(msg_id, {
            "offer_id": str(offer.id),
            "status": "pending",
            "message": "Offer created and sent to target agent",
        })

    # ─── referral_invite ─────────────────────────────────────────────────

    async def handle_referral_invite(self, message: dict, sender_agent_id: str, db: Session):
        """Handle a referral_invite method call with max-referral enforcement."""
        msg_id = message["id"]
        params = message.get("params")

        if not params:
            return self._error(msg_id, -32602, "Missing parameters")

        if "invitee_agent_id" not in params:
            return self._error(msg_id, -32602, "Missing 'invitee_agent_id' parameter")

        invitee_agent_id = params["invitee_agent_id"]

        # Check invitee exists
        invitee = db.query(Agent).filter(Agent.id == invitee_agent_id).first()
        if not invitee:
            return self._error(msg_id, -32602, "Invitee agent not found")

        # Cannot self-refer
        if str(invitee_agent_id) == str(sender_agent_id):
            return self._error(msg_id, -32602, "Cannot refer yourself")

        # Get inviter's user to enforce per-user limit
        inviter_agent = db.query(Agent).filter(Agent.id == sender_agent_id).first()
        if not inviter_agent:
            return self._error(msg_id, -32602, "Inviter agent not found")

        # Max 5 referrals per user (across all their agents)
        user_agent_ids = [
            a.id for a in db.query(Agent).filter(Agent.user_id == inviter_agent.user_id).all()
        ]
        existing_referrals = db.query(sql_func.count(Referral.id)).filter(
            Referral.inviter_agent_id.in_(user_agent_ids),
        ).scalar() or 0

        if existing_referrals >= MAX_REFERRALS_PER_AGENT:
            return self._error(
                msg_id, -32602,
                f"Maximum referral limit reached ({MAX_REFERRALS_PER_AGENT} per user)"
            )

        # Check for duplicate referral
        existing = db.query(Referral).filter(
            Referral.inviter_agent_id == sender_agent_id,
            Referral.invitee_agent_id == invitee_agent_id,
        ).first()
        if existing:
            return self._error(msg_id, -32602, "Referral already exists for this agent pair")

        # Create referral
        referral = Referral(
            id=uuid.uuid4(),
            inviter_agent_id=sender_agent_id,
            invitee_agent_id=invitee_agent_id,
            status=ReferralStatus.PENDING,
            device_fingerprint=params.get("device_fingerprint", ""),
        )
        db.add(referral)
        db.commit()
        db.refresh(referral)

        # Notify invitee
        notification = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "notification",
            "params": {
                "type": "referral_invite",
                "referral_id": str(referral.id),
                "from_agent_id": sender_agent_id,
            },
        }
        await self.send_to_agent(notification, str(invitee_agent_id))

        return self._result(msg_id, {
            "referral_id": str(referral.id),
            "status": "pending",
            "message": "Referral invite sent",
        })

    # ─── approve_payment ─────────────────────────────────────────────────

    async def handle_approve_payment(self, message: dict, sender_agent_id: str, db: Session):
        """Handle approve_payment — owner approves or denies a pending transaction."""
        msg_id = message["id"]
        params = message.get("params")

        if not params:
            return self._error(msg_id, -32602, "Missing parameters")

        if "task_session_id" not in params:
            return self._error(msg_id, -32602, "Missing 'task_session_id' parameter")

        if "approved" not in params:
            return self._error(msg_id, -32602, "Missing 'approved' parameter")

        task_session_id = params["task_session_id"]
        approved = params["approved"]

        # Find the task session
        task = db.query(TaskSession).filter(TaskSession.id == task_session_id).first()
        if not task:
            return self._error(msg_id, -32602, "Task session not found")

        # Only the caller agent's owner can approve
        caller_agent = db.query(Agent).filter(Agent.id == task.caller_agent_id).first()
        sender_agent = db.query(Agent).filter(Agent.id == sender_agent_id).first()

        if not caller_agent or not sender_agent:
            return self._error(msg_id, -32602, "Agent not found")

        # Verify the approver owns the caller agent (same user_id)
        if caller_agent.user_id != sender_agent.user_id:
            return self._error(msg_id, -32602, "Not authorized to approve this payment")

        # Find the pending transaction
        transaction = db.query(Transaction).filter(
            Transaction.task_session_id == task_session_id,
            Transaction.status == TransactionStatus.PENDING,
        ).first()

        if not transaction:
            return self._error(msg_id, -32602, "No pending transaction found for this task")

        caller_wallet = self._get_agent_wallet(db, str(task.caller_agent_id))

        if approved:
            # Complete the transaction — DB trigger handles balance transfer
            transaction.status = TransactionStatus.COMPLETED
            transaction.completed_at = datetime.utcnow()
            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.utcnow()

            # Release reserved credits (trigger will handle the actual transfer)
            if caller_wallet:
                self._release_escrow(db, caller_wallet, task.escrow_amount)
        else:
            # Deny — cancel transaction and release escrow
            transaction.status = TransactionStatus.CANCELLED
            task.status = TaskStatus.FAILED
            task.error_message = "Payment denied by owner"

            if caller_wallet:
                self._release_escrow(db, caller_wallet, task.escrow_amount)

        db.commit()

        # Publish notification
        if self.redis_client:
            await self.redis_client.publish("notifications", json.dumps({
                "type": "payment_approved" if approved else "payment_denied",
                "task_session_id": task_session_id,
                "caller_agent_id": str(task.caller_agent_id),
                "callee_agent_id": str(task.callee_agent_id),
                "amount": task.escrow_amount,
            }))

        return self._result(msg_id, {
            "task_session_id": task_session_id,
            "status": "approved" if approved else "denied",
            "message": f"Payment {'approved and completed' if approved else 'denied and refunded'}",
        })


# Global connection manager singleton
manager = ConnectionManager()
