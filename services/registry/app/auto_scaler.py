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

    while not _stop_event.is_set():
        try:
            builder_agent_id = await get_original_builder_agent_id()
            if builder_agent_id is None:
                logger.warning("No builder agent found, waiting...")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Get builder status and open task count
            builder_status = await get_agent_status(builder_agent_id)
            open_tasks = await get_open_task_count(builder_agent_id)
            logger.debug(
                f"Builder agent {builder_agent_id} status={builder_status} open_tasks={open_tasks}"
            )

            db = get_db()
            try:
                already_scaled = has_spawned_scaling_agent(db)
            finally:
                db.close()

            if not already_scaled and open_tasks > 50 and builder_status == "busy":
                # Scale up
                container_name = f"builder-scaling-{os.urandom(4).hex()}"
                container_name = start_scaled_container(container_name)
                agent_id = await register_scaled_agent(container_name)
                if agent_id:
                    _spawned_container_name = container_name
                    _spawned_agent_id = agent_id
                    logger.info(
                        f"Scaled up: container={container_name} agent={agent_id}"
                    )
                else:
                    # Clean up container if registration failed
                    stop_scaled_container(container_name)

            elif _spawned_container_name is not None and open_tasks < 20:
                # Scale down
                logger.info(
                    f"Backlog below 20 (open_tasks={open_tasks}), scaling down"
                )
                stop_scaled_container(_spawned_container_name)
                if _spawned_agent_id:
                    await delete_scaled_agent(_spawned_agent_id)
                _spawned_container_name = None
                _spawned_agent_id = None

        except Exception as e:
            logger.error(f"Auto-scaler loop error: {e}", exc_info=True)

        # Wait for the next polling interval, or stop early if event is set
        try:
            await asyncio.wait_for(
                _stop_event.wait(), timeout=POLL_INTERVAL
            )
            # If event was set, break immediately
            break
        except asyncio.TimeoutError:
            # Timeout means we should re-evaluate
            continue

    logger.info("Auto-scaler loop ended")


def cleanup_containers():
    """Stop and remove any scaled container that might still be running."""
    global _spawned_container_name
    if _spawned_container_name:
        stop_scaled_container(_spawned_container_name)
        _spawned_container_name = None
    # Also try to clean up any dangling containers with prefix
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    try:
        for container in _docker_client.containers.list(all=True):
            if container.name.startswith("builder-scaling-"):
                try:
                    container.stop(timeout=10)
                    container.remove()
                    logger.info(f"Cleaned up leftover container {container.name}")
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Failed to clean up containers: {e}")


async def start_auto_scaler() -> asyncio.Task:
    """Start the auto-scaler background task."""
    global _scaler_task
    _scaler_task = asyncio.create_task(auto_scaler_loop())
    return _scaler_task


async def stop_auto_scaler():
    """Signal the auto-scaler loop to stop and clean up resources."""
    global _stop_event, _scaler_task
    _stop_event.set()
    # Clean up any running scaled container
    cleanup_containers()
    if _scaler_task is not None:
        _scaler_task.cancel()
        try:
            await _scaler_task
        except asyncio.CancelledError:
            pass
        _scaler_task = None
    logger.info("Auto-scaler stopped")