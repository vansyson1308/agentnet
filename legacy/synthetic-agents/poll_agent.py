#!/usr/bin/env python3
"""
AgentNet Poll Agent — Bạn đồng hành của Echo Agent.
Cứ 30 giây tạo 1 task gửi đến Echo Agent để tạo activity.
"""
import asyncio
import json
import logging
import os
import uuid

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Poll] %(message)s")
log = logging.getLogger("poll_agent")

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://127.0.0.1:8000")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://127.0.0.1:8001")
AGENT_EMAIL = "caller@agentnet.io.vn"
AGENT_PASSWORD = os.getenv("AGENT_PASSWORD", "")
if not AGENT_PASSWORD:
    raise SystemExit("AGENT_PASSWORD env var is required (never hard-code credentials)")
AGENT_NAME = "AgentNet_Poller"
ECHO_AGENT_NAME = "AgentNet_Echo"


class PollAgent:
    def __init__(self):
        self.token = None
        self.agent_id = None
        self.echo_agent_id = None
        self.client = httpx.AsyncClient(timeout=30.0)

    async def ensure(self):
        """Register or find poller agent + find echo agent."""
        # Register user
        await self.client.post(f"{REGISTRY_URL}/v1/auth/user/register", json={
            "email": AGENT_EMAIL, "password": AGENT_PASSWORD
        })

        # Login
        r = await self.client.post(f"{REGISTRY_URL}/v1/auth/user/login", data={
            "username": AGENT_EMAIL, "password": AGENT_PASSWORD
        })
        self.token = r.json()["access_token"]

        # Find echo agent
        r = await self.client.get(f"{REGISTRY_URL}/v1/agents/?name={ECHO_AGENT_NAME}",
                                  headers={"Authorization": f"Bearer {self.token}"})
        agents = r.json()
        if agents:
            self.echo_agent_id = agents[0]["id"]
            log.info(f"Found Echo Agent: {self.echo_agent_id}")
        else:
            log.error("Echo agent not found!")
            return False

        # Create or find poller agent
        r = await self.client.post(f"{REGISTRY_URL}/v1/agents/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "name": AGENT_NAME,
                "description": "Polling agent — tạo task đều đặn",
                "capabilities": [{"name": "poll", "version": "1.0",
                    "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 0}],
                "endpoint": "http://127.0.0.1:9999",
                "public_key": "cG9sbEFnZW50S2V5"
            })
        if r.status_code == 201:
            self.agent_id = r.json()["id"]
        else:
            r2 = await self.client.get(f"{REGISTRY_URL}/v1/agents/?name={AGENT_NAME}",
                                       headers={"Authorization": f"Bearer {self.token}"})
            agents = r2.json()
            if agents:
                self.agent_id = agents[0]["id"]

        log.info(f"Poller Agent ID: {self.agent_id}")

        # Fund wallet
        r = await self.client.get(f"{PAYMENT_URL}/v1/wallets/?owner_type=agent&owner_id={self.agent_id}",
                                  headers={"Authorization": f"Bearer {self.token}"})
        wallets = r.json()
        if wallets:
            wid = wallets[0]["id"]
            await self.client.post(f"{PAYMENT_URL}/v1/wallets/{wid}/fund",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"amount": 50000, "currency": "credits"})
        return True

    async def create_task(self):
        """Create a task for Echo Agent."""
        try:
            payload = {
                "caller_agent_id": self.agent_id,
                "callee_agent_id": self.echo_agent_id,
                "capability": "echo",
                "input": {
                    "message": f"Hello Echo! Poll at {uuid.uuid4().hex[:8]}",
                    "source": "AgentNet_Poller",
                    "timestamp": str(uuid.uuid4())
                },
                "max_budget": 2,
                "currency": "credits",
                "timeout_seconds": 60
            }
            r = await self.client.post(f"{REGISTRY_URL}/v1/tasks/",
                headers={"Authorization": f"Bearer {self.token}"}, json=payload)
            if r.status_code == 201:
                data = r.json()
                log.info(f"Task created: {data['task_session_id'][:8]}... escrow locked")
            else:
                log.warning(f"Task create: {r.status_code} {r.text[:80]}")
        except Exception as e:
            log.error(f"Task error: {e}")

    async def run(self):
        if not await self.ensure():
            return
        log.info(f"Poller Agent running! Creating tasks every 30s...")
        while True:
            await self.create_task()
            await asyncio.sleep(30)


if __name__ == "__main__":
    agent = PollAgent()
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        log.info("Poller Agent stopped")
