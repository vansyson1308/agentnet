#!/usr/bin/env python3
"""
AgentNet Agent SDK — chạy AI agent local, kết nối tới AgentNet Registry.

Usage:
    python agent_sdk.py --name MyAgent --capability echo --registry https://agentnet.io.vn --api-key YOUR_KEY
    python agent_sdk.py --name Worker --capability reverse --registry https://agentnet.io.vn --api-key YOUR_KEY

Capabilities built-in:
    - echo: trả lại input y hệt
    - reverse: đảo ngược chuỗi input.text
    - count_words: đếm số từ trong input.text
    - uppercase: viết hoa input.text

Yêu cầu: pip install websockets httpx
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Optional

try:
    import httpx
except ImportError:
    print("Missing httpx. Run: pip install httpx")
    sys.exit(1)

try:
    import websockets
except ImportError:
    print("Missing websockets. Run: pip install websockets")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agentnet-sdk")

# ─── Built-in Capabilities ─────────────────────────────────────────────

CAPABILITY_HANDLERS: dict[str, callable] = {}


def capability(name: str):
    """Decorator đăng ký handler cho một capability."""
    def wrapper(fn):
        CAPABILITY_HANDLERS[name] = fn
        return fn
    return wrapper


@capability("echo")
async def handle_echo(input_data: dict) -> dict:
    """Trả lại input y hệt."""
    return {"echo": input_data}


@capability("reverse")
async def handle_reverse(input_data: dict) -> dict:
    """Đảo ngược chuỗi input.text."""
    text = input_data.get("text", "")
    return {"reversed": text[::-1], "original_length": len(text)}


@capability("count_words")
async def handle_count_words(input_data: dict) -> dict:
    """Đếm số từ trong input.text."""
    text = input_data.get("text", "")
    words = text.split()
    return {"word_count": len(words), "char_count": len(text)}


@capability("uppercase")
async def handle_uppercase(input_data: dict) -> dict:
    """Viết hoa input.text."""
    text = input_data.get("text", "")
    return {"uppercase": text.upper(), "original_length": len(text)}


# ─── Agent SDK Core ────────────────────────────────────────────────────


class AgentSDK:
    """Agent SDK kết nối tới AgentNet Registry, nhận offer, chạy task."""

    def __init__(
        self,
        name: str,
        capability_name: str,
        registry_url: str,
        api_key: str,
        endpoint: Optional[str] = None,
    ):
        self.name = name
        self.capability_name = capability_name
        self.registry_url = registry_url.rstrip("/")
        self.api_key = api_key
        self.endpoint = endpoint or f"https://agent-sdk.local/{name}"
        self.agent_id: Optional[str] = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.running = True
        self._task_id_counter = 0

        # HTTP client
        self.http = httpx.AsyncClient(
            base_url=self.registry_url,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )

    async def _api_call(self, method: str, path: str, **kwargs) -> Any:
        """Gọi REST API với auth token."""
        headers = kwargs.pop("headers", {})
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = await self.http.request(method, path, headers=headers, **kwargs)
        if resp.status_code >= 400:
            text = resp.text[:200]
            raise Exception(f"API {resp.status_code}: {text}")
        return resp.json()

    async def register(self) -> str:
        """Đăng ký agent với registry nếu chưa có agent_id."""
        logger.info(f"Registering agent '{self.name}' with capability '{self.capability_name}'...")

        data = {
            "name": self.name,
            "description": f"{self.name} — autonomous agent via AgentNet SDK",
            "endpoint": self.endpoint,
            "public_key": f"sdk-v1-{uuid.uuid4().hex[:16]}",
            "capabilities": [
                {
                    "name": self.capability_name,
                    "version": "1.0",
                    "description": f"Agent capability: {self.capability_name}",
                    "price": 0,
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                }
            ],
        }

        result = await self._api_call("POST", "/v1/agents/", json=data)
        self.agent_id = result.get("id")
        if not self.agent_id:
            raise Exception(f"Registration failed: {result}")
        logger.info(f"✅ Registered as agent {self.agent_id}")
        return self.agent_id

    async def heartbeat_loop(self):
        """Gửi heartbeat mỗi 30s."""
        while self.running and self.agent_id:
            try:
                await self._api_call(
                    "POST",
                    f"/v1/agents/{self.agent_id}/heartbeat?capability={self.capability_name}",
                )
                logger.debug(f"❤️ Heartbeat: {self.agent_id}")
            except Exception as e:
                logger.warning(f"Heartbeat failed: {e}")
            await asyncio.sleep(30)

    async def websocket_loop(self):
        """Kết nối WebSocket và lắng nghe offers real-time."""
        if not self.agent_id:
            logger.error("Cannot connect WS: not registered")
            return

        ws_url = self.registry_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url += f"/v1/ws/agent/{self.agent_id}?token={self.api_key}"

        while self.running:
            try:
                async with websockets.connect(ws_url) as ws:
                    self.ws = ws
                    logger.info(f"🔗 WebSocket connected: {ws_url[:60]}...")

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            await self._handle_ws_message(msg)
                        except json.JSONDecodeError:
                            pass
                        except Exception as e:
                            logger.error(f"WS handler error: {e}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket disconnected, reconnecting in 5s...")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                await asyncio.sleep(5)

    async def _handle_ws_message(self, msg: dict):
        """Xử lý message từ WebSocket (offer, execute, notification)."""
        method = msg.get("method")
        params = msg.get("params", {})

        if method == "notification":
            ntype = params.get("type")
            if ntype == "offer_received":
                logger.info(f"📩 Offer received: {params.get('title')} from {params.get('from_agent_id')}")
                # Auto-accept
                offer_id = params.get("offer_id")
                if offer_id:
                    await self._api_call("POST", f"/v1/offers/{offer_id}/accept")
                    logger.info(f"✅ Auto-accepted offer {offer_id}")

        elif method == "execute":
            logger.info(f"⚡ Task execute: {params.get('capability')}")
            cap = params.get("capability", "")
            inp = params.get("input", {})
            task_id = msg.get("id")
            from_agent = msg.get("from")

            handler = CAPABILITY_HANDLERS.get(cap) or CAPABILITY_HANDLERS.get(self.capability_name)
            if handler:
                try:
                    result = await handler(inp)
                    response = {
                        "jsonrpc": "2.0",
                        "id": task_id,
                        "result": {"output": result, "agent_id": self.agent_id},
                    }
                    if self.ws:
                        await self.ws.send(json.dumps(response))
                    logger.info(f"✅ Task completed: {cap}")
                except Exception as e:
                    logger.error(f"Task execution failed: {e}")
                    if self.ws:
                        await self.ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": task_id,
                            "error": {"code": -32000, "message": str(e)},
                        }))
            else:
                logger.warning(f"Unknown capability: {cap}")

    async def run(self):
        """Khởi chạy agent."""
        logger.info(f"🚀 AgentNet SDK — {self.name} ({self.capability_name})")
        logger.info(f"   Registry: {self.registry_url}")

        # Register
        try:
            agent_id = await self.register()
        except Exception as e:
            logger.error(f"Failed to register: {e}")
            return

        # Run heartbeat + WebSocket in parallel
        await asyncio.gather(
            self.heartbeat_loop(),
            self.websocket_loop(),
        )


# ─── CLI ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="AgentNet Agent SDK")
    parser.add_argument("--name", required=True, help="Agent name")
    parser.add_argument("--capability", default="echo", help="Capability name (echo/reverse/count_words/uppercase)")
    parser.add_argument("--registry", default="https://agentnet.io.vn", help="Registry URL")
    parser.add_argument("--api-key", help="API key / JWT token")
    parser.add_argument("--endpoint", help="Public endpoint URL (optional)")

    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("AGENTNET_API_KEY")
    if not api_key:
        print("Error: Need --api-key or AGENTNET_API_KEY env var")
        print("Tip: Register a user at the registry first, then login to get a token")
        sys.exit(1)

    sdk = AgentSDK(
        name=args.name,
        capability_name=args.capability,
        registry_url=args.registry,
        api_key=api_key,
        endpoint=args.endpoint,
    )

    try:
        asyncio.run(sdk.run())
    except KeyboardInterrupt:
        logger.info("👋 Shutting down...")
        sdk.running = False


if __name__ == "__main__":
    main()
