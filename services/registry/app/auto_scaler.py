import asyncio
import logging
import os
from typing import Optional

import docker
import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from .database import SessionLocal

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

# Task reference for the loop
_scaler_task: Optional[asyncio.Task] = None


def get_db() -> Session:
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


async def get_agent_status(agent_id: str) -> Optional[str]:
    """Fetch agent status (e.g., 'busy') from the registry."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{REGISTRY_URL}/agents/{agent_id}")
            resp.raise_for_status()
            agent = resp.json()
            return agent.get("status")
        except Exception as e:
            logger.error(f"Failed to fetch agent {agent_id} status: {e}")
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


def has_spawned_scaling_agent() -> bool:
    """Check if there is already a builder-scaling agent in the database.
    This uses synchronous DB access; call via asyncio.to_thread.
    """
    db = get_db()
    try:
        result = db.execute(
            text("SELECT COUNT(*) FROM agents WHERE agent_type = 'builder-scaling'")
        ).scalar()
        return result > 0
    except Exception as e:
        logger.error(f"Failed to check for scaling agent: {e}")
        return False
    finally:
        db.close()


async def scale_loop():
    """Background loop that evaluates load and scales up/down as needed."""
    global _spawned_container_name, _spawned_agent_id
    logger.info("Auto-scaler loop started")
    while not _stop_event.is_set():
        try:
            # Get the original builder agent ID
            builder_id = await get_original_builder_agent_id()
            if not builder_id:
                logger.warning("No builder agent found, skipping scale check")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Check builder status and task load
            status = await get_agent_status(builder_id)
            backlog = await get_open_task_count(builder_id)
            logger.debug(f"Builder agent {builder_id}: status={status}, backlog={backlog}")

            # Determine if we need to scale up
            scale_up = (
                backlog > 50
                and status == "busy"
                and _spawned_agent_id is None
            )

            # Determine if we need to scale down
            scale_down = (
                backlog < 20
                and _spawned_agent_id is not None
            )

            if scale_up:
                # Generate a unique container name
                container_name = f"builder-scaling-{int(asyncio.get_event_loop().time())}"
                try:
                    container_name = start_scaled_container(container_name)
                    agent_id = await register_scaled_agent(container_name)
                    if agent_id:
                        _spawned_container_name = container_name
                        _spawned_agent_id = agent_id
                        logger.info(f"Scaled up: container={container_name}, agent={agent_id}")
                    else:
                        # Registration failed, stop the container
                        stop_scaled_container(container_name)
                except Exception as e:
                    logger.error(f"Failed to scale up: {e}")

            elif scale_down:
                try:
                    if _spawned_container_name:
                        stop_scaled_container(_spawned_container_name)
                    if _spawned_agent_id:
                        await delete_scaled_agent(_spawned_agent_id)
                    _spawned_container_name = None
                    _spawned_agent_id = None
                    logger.info("Scaled down and cleaned up")
                except Exception as e:
                    logger.error(f"Failed to scale down: {e}")

        except Exception as e:
            logger.error(f"Error in scale loop: {e}", exc_info=True)

        # Wait for next poll interval or until stop event is set
        try:
            await asyncio.wait_for(
                _stop_event.wait(), timeout=POLL_INTERVAL
            )
        except asyncio.TimeoutError:
            pass  # Expected, continue loop

    logger.info("Auto-scaler loop stopped")


async def start_auto_scaler() -> asyncio.Task:
    """Start the background auto-scaler task. Returns the task object.
    If already running, returns existing task.
    """
    global _scaler_task
    if _scaler_task is not None and not _scaler_task.done():
        logger.warning("Auto-scaler already running")
        return _scaler_task
    _stop_event.clear()
    _scaler_task = asyncio.create_task(scale_loop())
    logger.info("Auto-scaler started")
    return _scaler_task


async def stop_auto_scaler():
    """Signal the auto-scaler to stop and await its completion."""
    global _scaler_task
    if _scaler_task is None:
        return
    _stop_event.set()
    try:
        await _scaler_task
    except asyncio.CancelledError:
        pass
    _scaler_task = None
    logger.info("Auto-scaler stopped")
