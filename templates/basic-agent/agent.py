"""
AgentNet Basic Agent Template

A minimal FastAPI-based agent that:
1. Serves an A2A Agent Card at /.well-known/agent-card.json
2. Handles task execution via /execute endpoint
3. Responds to verification challenges via /verify endpoint
4. Self-registers with AgentNet Registry on startup

Usage:
    # Install dependencies
    pip install fastapi uvicorn httpx

    # Run the agent
    python agent.py

    # Or with custom settings
    python agent.py --name "my-agent" --port 9000 --registry http://localhost:8000
"""

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---

AGENT_NAME = os.getenv("AGENT_NAME", "my-basic-agent")
AGENT_DESCRIPTION = os.getenv("AGENT_DESCRIPTION", "A basic AgentNet agent")
AGENT_PORT = int(os.getenv("AGENT_PORT", "9000"))
AGENT_HOST = os.getenv("AGENT_HOST", "0.0.0.0")
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:8000")


# --- Define your capabilities here ---

CAPABILITIES = [
    {
        "name": "hello",
        "version": "1.0",
        "description": "Says hello to the given name",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name to greet"},
            },
            "required": ["name"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "greeting": {"type": "string"},
            },
        },
        "price": 1,  # 1 credit per call
    },
]


# --- A2A Agent Card ---

def build_agent_card(base_url: str) -> dict:
    """Build an A2A Agent Card for this agent."""
    return {
        "name": AGENT_NAME,
        "description": AGENT_DESCRIPTION,
        "version": "1.0.0",
        "provider": {
            "name": "AgentNet Community",
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "supportedInterfaces": [
            {"protocol": "REST", "url": base_url},
        ],
        "skills": [
            {
                "id": cap["name"],
                "name": cap["name"],
                "description": cap.get("description", cap["name"]),
                "tags": [cap["name"]],
                "examples": [],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            }
            for cap in CAPABILITIES
        ],
        "securitySchemes": {
            "none": {"type": "none", "description": "No authentication required"},
        },
    }


# --- FastAPI App ---

app = FastAPI(title=AGENT_NAME, description=AGENT_DESCRIPTION)


class ExecuteRequest(BaseModel):
    capability: str
    input: Dict[str, Any]
    task_session_id: str = ""


class VerifyRequest(BaseModel):
    capability: str
    test_input: Dict[str, Any]


@app.get("/.well-known/agent-card.json")
async def agent_card():
    """A2A Agent Card — discovery endpoint."""
    base_url = f"http://{AGENT_HOST}:{AGENT_PORT}"
    return build_agent_card(base_url)


@app.post("/execute")
async def execute(request: ExecuteRequest):
    """
    Execute a task.

    This is where you implement your agent's core logic.
    Modify the handler for each capability below.
    """
    if request.capability == "hello":
        name = request.input.get("name", "World")
        return {"greeting": f"Hello, {name}! I'm {AGENT_NAME}."}

    raise HTTPException(status_code=400, detail=f"Unknown capability: {request.capability}")


@app.post("/verify")
async def verify(request: VerifyRequest):
    """
    Handle verification challenges from AgentNet Registry.

    The registry sends test inputs to verify your agent can
    actually perform the claimed capabilities.
    """
    if request.capability == "hello":
        name = request.test_input.get("name", "Test")
        return {"greeting": f"Hello, {name}! I'm {AGENT_NAME}."}

    raise HTTPException(status_code=400, detail=f"Unknown capability: {request.capability}")


@app.get("/health")
async def health():
    return {"status": "ok", "agent": AGENT_NAME}


# --- Self-registration with AgentNet Registry ---

async def register_with_agentnet(user_email: str, password: str):
    """
    Register this agent with AgentNet Registry.

    Call this after the agent is running and reachable.
    """
    async with httpx.AsyncClient() as client:
        # Login (OAuth2 form data format)
        login_resp = await client.post(
            f"{REGISTRY_URL}/v1/auth/user/login",
            data={"username": user_email, "password": password},
        )
        if login_resp.status_code != 200:
            logger.error(f"Login failed: {login_resp.text}")
            return

        token = login_resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Register agent
        agent_data = {
            "name": AGENT_NAME,
            "description": AGENT_DESCRIPTION,
            "capabilities": CAPABILITIES,
            "endpoint": f"http://{AGENT_HOST}:{AGENT_PORT}",
            "public_key": str(uuid.uuid4()),
        }

        reg_resp = await client.post(
            f"{REGISTRY_URL}/v1/agents/",
            json=agent_data,
            headers=headers,
        )

        if reg_resp.status_code == 201:
            agent_id = reg_resp.json()["id"]
            logger.info(f"Agent registered with AgentNet: {agent_id}")
        else:
            logger.warning(f"Registration response: {reg_resp.status_code} {reg_resp.text}")


# --- Main ---

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"AgentNet Agent: {AGENT_NAME}")
    parser.add_argument("--name", default=AGENT_NAME, help="Agent name")
    parser.add_argument("--port", type=int, default=AGENT_PORT, help="Port to listen on")
    parser.add_argument("--registry", default=REGISTRY_URL, help="AgentNet Registry URL")
    args = parser.parse_args()

    AGENT_NAME = args.name
    AGENT_PORT = args.port
    REGISTRY_URL = args.registry

    logger.info(f"Starting {AGENT_NAME} on port {AGENT_PORT}")
    logger.info(f"A2A Card: http://localhost:{AGENT_PORT}/.well-known/agent-card.json")
    logger.info(f"Docs: http://localhost:{AGENT_PORT}/docs")

    uvicorn.run(app, host=AGENT_HOST, port=AGENT_PORT)
