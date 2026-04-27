import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional

import redis.asyncio as redis
from fastapi import WebSocket
from sqlalchemy import func as sql_func
from sqlalchemy.orm import Session

from .auth import TokenData, hash_input, verify_token
from .database import get_db
from .models import (
    Agent,
    AgentStatus,
    CurrencyType,
    Offer,
    OfferStatus,
    Referral,
    ReferralStatus,
    TaskSession,
    TaskStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletOwnerType,
)
from .schemas import WebSocketMessage, WebSocketResponse

logger = logging.getLogger(__name__)

# Rate limit constants
EXECUTE_RATE_LIMIT = 100  # max transactions per hour per agent
OFFER_RATE_LIMIT = 10  # max offers per hour per agent
MAX_REFERRALS_PER_AGENT = 5  # max referrals per inviter
OFFER_ELIGIBILITY_MIN_TASKS = 10
OFFER_ELIGIBILITY_MIN_SUCCESS = 0.90
OFFER_ELIGIBILITY_MIN_QUALITY = 3.5
OFFER_TASK_RATIO_THRESHOLD = 0.80
APPROVAL_TTL_SECONDS = 300  # 5-minute approval timeout

# Plaza constants
PLAZA_BROADCAST_INTERVAL = 1.0  # seconds between position broadcasts to all plaza clients
PLAZA_CHAT_BUBBLE_DURATION = 5.0  # seconds chat bubbles stay visible


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.agent_connections: Dict[str, str] = {}  # agent_id -> connection_id
        self.redis_client = None
        self.pubsub = None
        # Plaza state
        self.plaza_connections: Dict[str, "PlazaParticipant"] = {}  # connection_id -> PlazaParticipant

    async def init_redis(self):
        """Initialize Redis connection and pubsub."""
        redis_url = (
            f"redis://:{os.getenv('REDIS_PASSWORD', 'your_redis_password')}"
            f"@{os.getenv('REDIS_HOST', 'redis')}:{os.getenv('REDIS_PORT', '6379')}/0"
        )
        self.redis_client = await redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
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

            # Mark agent as online on WebSocket connect
            agent.is_online = True
            agent.last_seen_at = datetime.utcnow()
            db.commit()

            logger.info(f"Agent {agent.id} connected with connection ID {connection_id}")

            # Broadcast online status to dashboard
            asyncio.create_task(
                self.broadcast({
                    "type": "agent_status",
                    "agent_id": str(agent.id),
                    "name": agent.name,
                    "status": "online",
                    "capability": agent.current_capability,
                })
            )

            # If agent is in the plaza, add to plaza roster
            # Plaza participation is determined by the first message sent (see handle_message)
            return connection_id

        except Exception as e:
            logger.error(f"Error connecting agent: {e}")
            await websocket.close(code=1008, reason="Authentication error")
            return None

    def disconnect(self, connection_id: str, db: Optional[Session] = None):
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

                # Mark agent as offline if db session provided
                if db is not None:
                    try:
                        agent = db.query(Agent).filter(Agent.id == agent_id).first()
                        if agent:
                            agent.is_online = False
                            agent.last_seen_at = datetime.utcnow()
                            db.commit()
                            # Broadcast offline status
                            asyncio.create_task(
                                self.broadcast({
                                    "type": "agent_status",
                                    "agent_id": str(agent.id),
                                    "status": "offline",
                                })
                            )
                    except Exception as e:
                        logger.error(f"Error marking agent offline: {e}")

            # Remove from plaza if present
            if connection_id in self.plaza_connections:
                participant = self.plaza_connections.pop(connection_id)
                # Broadcast departure
                asyncio.create_task(
                    self.broadcast_to_plaza({
                        "type": "plaza_participant_left",
                        "agent_id": participant.agent_id,
                    })
                )

    async def handle_message(self, message: dict, db: Session, connection_id: str):
        """Route incoming WebSocket messages to appropriate handlers."""
        msg_type = message.get("type", "")
        agent_id = None
        for aid, cid in self.agent_connections.items():
            if cid == connection_id:
                agent_id = aid
                break

        if not agent_id:
            logger.warning(f"Message from unknown connection {connection_id}")
            return

        if msg_type == "plaza_join":
            await self.handle_plaza_join(connection_id, agent_id, db)
        elif msg_type == "plaza_position":
            await self.handle_plaza_position(message, agent_id, connection_id)
        elif msg_type == "plaza_chat":
            await self.handle_plaza_chat(message, agent_id, db)
        elif msg_type == "plaza_status_request":
            await self.handle_plaza_status_request(connection_id, db)
        # ... existing task/offer messages would be handled here (preserve)

    # ---- Plaza handlers ----

    async def handle_plaza_join(self, connection_id: str, agent_id: str, db: Session):
        """Handle agent entering the plaza."""
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            return

        participant = PlazaParticipant(
            agent_id=agent_id,
            name=agent.name,
            status=agent.status,
            capability=agent.current_capability,
            x=0.0, y=0.0, z=0.0,
        )
        self.plaza_connections[connection_id] = participant

        # Send current plaza state to the new participant
        participants_list = []
        for con_id, part in self.plaza_connections.items():
            participants_list.append(part.to_dict())
        await self.send_message(connection_id, {
            "type": "plaza_state",
            "participants": participants_list,
        })

        # Broadcast newcomer to all other plaza participants
        await self.broadcast_to_plaza({
            "type": "plaza_participant_joined",
            "agent_id": agent_id,
            "name": agent.name,
            "status": agent.status,
            "capability": agent.current_capability,
            "x": 0.0, "y": 0.0, "z": 0.0,
        }, exclude=[connection_id])

    async def handle_plaza_position(self, message: dict, agent_id: str, connection_id: str):
        """Handle position update from an agent in the plaza."""
        participant = self.plaza_connections.get(connection_id)
        if not participant:
            return
        participant.x = message.get("x", 0.0)
        participant.y = message.get("y", 0.0)
        participant.z = message.get("z", 0.0)
        participant.last_update = datetime.utcnow()

        # Broadcast to other plaza participants
        await self.broadcast_to_plaza({
            "type": "plaza_position_update",
            "agent_id": agent_id,
            "x": participant.x,
            "y": participant.y,
            "z": participant.z,
        }, exclude=[connection_id])

    async def handle_plaza_chat(self, message: dict, agent_id: str, db: Session):
        """Handle chat message from an agent in the plaza."""
        text = message.get("text", "").strip()
        if not text:
            return
        # Get agent name
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        name = agent.name if agent else "Unknown"
        await self.broadcast_to_plaza({
            "type": "plaza_chat_bubble",
            "agent_id": agent_id,
            "name": name,
            "text": text,
            "timestamp": datetime.utcnow().isoformat(),
        })

    async def handle_plaza_status_request(self, connection_id: str, db: Session):
        """Handle request for current agent status update (for card display)."""
        participant = self.plaza_connections.get(connection_id)
        if not participant:
            return
        agent = db.query(Agent).filter(Agent.id == participant.agent_id).first()
        if not agent:
            return
        # Send status update back to requesting client
        await self.send_message(connection_id, {
            "type": "plaza_agent_status",
            "agent_id": str(agent.id),
            "status": agent.status,
            "capability": agent.current_capability,
            "task_count": agent.total_tasks_completed,
            "reputation_tier": agent.reputation_tier,
        })

    # ---- Broadcast helpers ----

    async def broadcast_to_plaza(self, message: dict, exclude: list = None):
        """Send a message to all plaza participants."""
        if exclude is None:
            exclude = []
        for con_id in self.plaza_connections:
            if con_id not in exclude:
                await self.send_message(con_id, message)

    async def send_message(self, connection_id: str, message: dict):
        """Send a JSON message to a specific connection."""
        websocket = self.active_connections.get(connection_id)
        if websocket:
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Error sending message to {connection_id}: {e}")

    async def broadcast(self, message: dict):
        """Broadcast a message to all connections (used by dashboard)."""
        for connection_id in list(self.active_connections.keys()):
            await self.send_message(connection_id, message)

    # ... existing methods (handle_execute, handle_offer, etc.) would be preserved below ...


class PlazaParticipant:
    """Represents an agent currently in the plaza."""
    def __init__(self, agent_id: str, name: str, status: str = "idle",
                 capability: str = "", x: float = 0.0, y: float = 0.0, z: float = 0.0):
        self.agent_id = agent_id
        self.name = name
        self.status = status
        self.capability = capability
        self.x = x
        self.y = y
        self.z = z
        self.last_update = datetime.utcnow()

    def to_dict(self):
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "status": self.status,
            "capability": self.capability,
            "x": self.x,
            "y": self.y,
            "z": self.z,
        }


# Singleton instance
manager = ConnectionManager()