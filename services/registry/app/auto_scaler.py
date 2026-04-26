import asyncio
import logging
import os
from typing import Optional

import docker
import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .database import engine as db_engine, SessionLocal

logger = logging.getLogger(__name__)

# Environment variables
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://127.0.0.1:8000")
BUILDER_IMAGE = os.getenv("BUILDER_IMAGE", "agentnet-builder")
BUILDER_AGENT_ID = os.getenv("BUILDER_AGENT_ID", None)  # optional override
POLL_INTERVAL = int(os.getenv("AUTO_SCALER_POLL_INTERVAL", "60"))

# State
_stop_event = asyncio.Event()
_spawned_container_name: Optional[str] = None
_spawned_agent_id: Optional[str] = None
_docker_client: Optional[docker.DockerClient] = None


def get_engine():
    return db_engine


def get_db():
    return SessionLocal()


async def get_original_builder_agent_id() -> Optional[str]:
    """Return the agent_id of the original Builder agent.
    If BUILDER_AGENT_ID is set, use that.
    Otherwise, find the first agent with type 'builder'.
    """
    if BUILDER_AGENT_ID:
        return BUILDER_AGENT_ID
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{REGISTRY_URL}/agents")
            resp.raise_for_status()
            agents = resp.json()
            for agent in agents:
                if agent.get("agent_type") == "builder":
                    return agent["id"]
        except Exception as e:
            logger.error(f"Failed to fetch agents: {e}")
    return None


async def get_open_task_count(agent_id: str) -> int:
    """Return number of tasks assigned to this agent with status != 'completed'."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{REGISTRY_URL}/tasks?agent_id={agent_id}&status_ne=completed"
            )
            resp.raise_for_status()
            tasks = resp.json()
            # The API may return a list or dict with items. Assume list.
            return len(tasks)
        except Exception as e:
            logger.error(f"Failed to fetch tasks for agent {agent_id}: {e}")
            return 0


def start_scaled_container(agent_name: str) -> str:
    """Start a Docker container with the builder image.
    Returns the container name.
    """
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    env_vars = {
        "REGISTRY_URL": REGISTRY_URL,
    }
    container = _docker_client.containers.run(
        BUILDER_IMAGE,
        detach=True,
        name=agent_name,
        environment=env_vars,
        remove=False,
    )
    logger.info(f"Started container {container.name} (id={container.short_id})")
    return container.name


def stop_scaled_container(container_name: str):
    """Stop and remove a running container."""
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    try:
        container = _docker_client.containers.get(container_name)
        container.stop(timeout=10)
        container.remove()
        logger.info(f"Stopped and removed container {container_name}")
    except docker.errors.NotFound:
        logger.warning(f"Container {container_name} not found")
    except Exception as e:
        logger.error(f"Failed to stop container {container_name}: {e}")


async def register_scaled_agent(container_name: str) -> Optional[str]:
    """Register a new agent via POST /agents.
    Returns the agent id.
    """
    payload = {
        "name": container_name,
        "agent_type": "builder-scaling",
        "custom_fields": {"container_name": container_name},
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(f"{REGISTRY_URL}/agents", json=payload)
            resp.raise_for_status()
            agent = resp.json()
            logger.info(f"Registered scaled agent {agent['id']} (container {container_name})")
            return agent["id"]
        except Exception as e:
            logger.error(f"Failed to register scaled agent: {e}")
            return None


async def delete_scaled_agent(agent_id: str):
    """Delete the scaled agent via DELETE /agents/<id>."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.delete(f"{REGISTRY_URL}/agents/{agent_id}")
            resp.raise_for_status()
            logger.info(f"Deleted scaled agent {agent_id}")
        except Exception as e:
            logger.error(f"Failed to delete scaled agent {agent_id}: {e}")


def has_spawned_scaling_agent(db: Session) -> bool:
    """Check if there is already a builder-scaling agent in the database."""
    result = db.execute(
        text("SELECT COUNT(*) FROM agents WHERE agent_type = 'builder-scaling'")
    ).scalar()
    return result > 0


async def auto_scaler_loop():
    """Main loop for auto-scaling."""
    global _stop_event, _spawned_container_name, _spawned_agent_id
    logger.info("Auto-scaler started (polling every %d seconds)", POLL_INTERVAL)
    db = get_db()

    try:
        while not _stop_event.is_set():
            builder_agent_id = await get_original_builder_agent_id()
            if builder_agent_id is None:
                logger.warning("No builder agent found, waiting...")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            open_tasks = await get_open_task_count(builder_agent_id)
            logger.debug(f"Builder agent {builder_agent_id} has {open_tasks} open tasks")

            if open_tasks > 50 and _spawned_container_name is None:
                # Check if any scaling agent already exists in DB
                if has_spawned_scaling_agent(db):
                    logger.info("Scaling agent already exists, skipping spawn")
                else:
                    # Spawn
                    container_name = f"builder-scaling-{asyncio.get_event_loop().time()}"
                    try:
                        container_name = start_scaled_container(container_name)
                        agent_id = await register_scaled_agent(container_name)
                        if agent_id:
                            _spawned_container_name = container_name
                            _spawned_agent_id = agent_id
                            logger.info(f"Spawned scaling agent {agent_id} (container {container_name})")
                        else:
                            # Registration failed, stop container
                            stop_scaled_container(container_name)
                    except Exception as e:
                        logger.error(f"Failed to spawn scaling container: {e}")

            elif open_tasks < 20 and _spawned_container_name is not None:
                # Scale down
                logger.info("Backlog low, scaling down")
                if _spawned_container_name:
                    stop_scaled_container(_spawned_container_name)
                if _spawned_agent_id:
                    await delete_scaled_agent(_spawned_agent_id)
                _spawned_container_name = None
                _spawned_agent_id = None

            await asyncio.sleep(POLL_INTERVAL)

    except asyncio.CancelledError:
        logger.info("Auto-scaler loop cancelled")
    finally:
        db.close()
        # Cleanup any spawned containers on shutdown
        if _spawned_container_name:
            stop_scaled_container(_spawned_container_name)
        if _spawned_agent_id:
            # Try to delete agent from DB (non-blocking)
            asyncio.create_task(delete_scaled_agent(_spawned_agent_id))
        logger.info("Auto-scaler stopped")


async def start_auto_scaler():
    """Start the auto-scaler as a background task."""
    loop = asyncio.get_event_loop()
    task = loop.create_task(auto_scaler_loop())
    return task


async def stop_auto_scaler():
    """Signal the auto-scaler to stop."""
    global _stop_event
    _stop_event.set()