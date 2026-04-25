#!/usr/bin/env python3
"""
AgentNet Echo Agent — Always-on WebSocket agent.
Registers itself on startup, listens for incoming tasks, echoes back results.
"""
import asyncio
import json
import logging
import os
import sys
import uuid

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Echo] %(message)s")
log = logging.getLogger("echo_agent")

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://127.0.0.1:8000")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://127.0.0.1:8001")
AGENT_NAME = "AgentNet_Echo"
AGENT_PASSWORD = "EchoAgent2026!"
AGENT_EMAIL = "echo@agentnet.io.vn"


class EchoAgent:
    def __init__(self):
        self.token = None
        self.agent_id = None
        self.wallet_id = None
        self.client = httpx.AsyncClient(timeout=30.0)

    async def ensure_registered(self):
        """Register user + agent. Idempotent — skips if exists."""
        # Register user (might fail if exists, that's OK)
        r = await self.client.post(f"{REGISTRY_URL}/v1/auth/user/register", json={
            "email": AGENT_EMAIL, "password": AGENT_PASSWORD
        })
        if r.status_code == 201:
            log.info(f"User registered: {r.json().get('id')}")
        elif r.status_code == 400:
            log.info("User already exists, proceeding")

        # Login to get token
        r = await self.client.post(f"{REGISTRY_URL}/v1/auth/user/login", data={
            "username": AGENT_EMAIL, "password": AGENT_PASSWORD
        })
        self.token = r.json()["access_token"]
        log.info("Logged in successfully")

        # Create agent (might fail if exists)
        capability = [{
            "name": "echo",
            "version": "1.0",
            "description": "Echoes back any input with metadata",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "price": 2
        }]
        r = await self.client.post(
            f"{REGISTRY_URL}/v1/agents/",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "name": AGENT_NAME,
                "description": "24/7 Echo Agent — luôn sẵn sàng phản hồi",
                "capabilities": capability,
                "endpoint": "ws://agentnet.io.vn/internal/echo",
                "public_key": "ZWNob0FnZW50UHVibGljS2V5"
            }
        )
        if r.status_code == 201:
            data = r.json()
            self.agent_id = data["id"]
            log.info(f"Agent created: {self.agent_id}")
        elif r.status_code == 400:
            # Already exists — find it
            r2 = await self.client.get(
                f"{REGISTRY_URL}/v1/agents/?name={AGENT_NAME}",
                headers={"Authorization": f"Bearer {self.token}"}
            )
            agents = r2.json()
            if agents:
                self.agent_id = agents[0]["id"]
                log.info(f"Agent exists: {self.agent_id}")
            else:
                log.error("Cannot find/create agent")
                return False

        # Get wallet and fund it
        if self.agent_id:
            r = await self.client.get(
                f"{PAYMENT_URL}/v1/wallets/?owner_type=agent&owner_id={self.agent_id}",
                headers={"Authorization": f"Bearer {self.token}"}
            )
            wallets = r.json()
            if wallets:
                self.wallet_id = wallets[0]["id"]
                bal = wallets[0].get("balance_credits", 0)
                if bal < 100:
                    await self.client.post(
                        f"{PAYMENT_URL}/v1/wallets/{self.wallet_id}/fund",
                        headers={"Authorization": f"Bearer {self.token}"},
                        json={"amount": 10000, "currency": "credits"}
                    )
                    log.info("Wallet funded with 10000 credits")
            log.info(f"Wallet ID: {self.wallet_id}")
        return True

    async def check_incoming_tasks(self):
        """Poll for tasks assigned to this agent and process them."""
        while True:
            try:
                r = await self.client.get(
                    f"{REGISTRY_URL}/v1/tasks/?callee_agent_id={self.agent_id}&status=initiated",
                    headers={"Authorization": f"Bearer {self.token}"}
                )
                tasks = r.json() if r.status_code == 200 else []
                # Also check in_progress and history
                for task in (tasks if isinstance(tasks, list) else []):
                    task_id = task.get("id")
                    if task_id:
                        await self.process_task(task_id)
            except Exception as e:
                log.error(f"Poll error: {e}")
            await asyncio.sleep(5)

    async def process_task(self, task_id):
        """Start, execute, and confirm a task."""
        log.info(f"Processing task: {task_id}")
        try:
            # Start task
            r = await self.client.put(
                f"{REGISTRY_URL}/v1/tasks/{task_id}/start",
                headers={"Authorization": f"Bearer {self.token}"}
            )
            if r.status_code not in (200, 400):
                log.warning(f"Start task failed: {r.status_code} {r.text[:100]}")
                return

            # Echo — just mirror the input
            await asyncio.sleep(0.5)  # Simulate processing

            # Confirm with output
            r = await self.client.put(
                f"{REGISTRY_URL}/v1/tasks/{task_id}/confirm",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"result": f"Echoed by AgentNet Echo Agent at {uuid.uuid4()}"}
            )
            if r.status_code == 200:
                log.info(f"✅ Task {task_id} completed successfully")
            else:
                log.warning(f"Confirm failed: {r.status_code} {r.text[:100]}")
        except Exception as e:
            log.error(f"Error processing task {task_id}: {e}")

    async def run(self):
        if not await self.ensure_registered():
            log.error("Failed to initialize Echo Agent")
            return

        log.info(f"🤖 Echo Agent running! ID: {self.agent_id}")
        log.info(f"   Wallet: {self.wallet_id}")
        log.info("   Polling for incoming tasks every 5s...")

        await self.check_incoming_tasks()


if __name__ == "__main__":
    agent = EchoAgent()
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        log.info("Echo Agent stopped")
