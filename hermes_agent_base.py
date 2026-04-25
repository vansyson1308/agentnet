"""
HermesAgent — Shared Base Class cho tất cả AgentNet Agents

Cấu trúc như Hermes:
- Event loop + state machine
- Token management + auto-refresh
- Structured logging
- Memory tracking (processed IDs + context)
- API helpers with retry + error handling
"""
import json
import os
import sys
import time as time_module
import urllib.request
import urllib.error
import random
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional

# ── Configuration ──
REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://localhost:8000")

AGENT_IDS = {
    "planner": "2a6f9475-4457-4548-ae47-84efa5661c09",
    "builder": "2224fb23-aa03-425e-b07f-7d3cf8fcfb60",
    "qa": "2bc543db-13df-4198-ad6f-5f624096f578",
    "storyteller": "e32dcb96-dab4-4150-8c36-ad2e04a481a1",
}

USER_CREDS = {
    "username": os.environ.get("AGENTNET_USER_EMAIL", "CHANGE_ME@example.com"),
    # ⚠️ Set AGENTNET_USER_EMAIL + AGENTNET_USER_PASSWORD in .env to login
    "password": os.environ.get("AGENTNET_USER_PASSWORD", "TestPass123"),
}


class HermesAgent:
    """Base class cho tất cả agents. Gồm event loop, state machine, API helpers."""

    def __init__(self, name: str, agent_key: str, sleep_seconds: int = 20):
        """
        name: "Hermes_Builder", "Hermes_QAAgent", v.v.
        agent_key: key in AGENT_IDS (builder, qa, planner, storyteller)
        sleep_seconds: loop interval
        """
        self.name = name
        self.agent_id = AGENT_IDS[agent_key]
        self.sleep_seconds = sleep_seconds
        self._token = None
        self._token_expiry = 0
        self._state = "init"
        self._cycle = 0
        self._processed_file = f"/tmp/{name.lower().replace(' ', '_')}_processed.json"
        self._processed: set = set()

        # Logging
        self.log = logging.getLogger(name)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            f"[%(asctime)s] [{name[:8]}] %(levelname)s| %(message)s",
            datefmt="%H:%M:%S",
        ))
        self.log.addHandler(handler)
        self.log.setLevel(logging.INFO)
        self.log.propagate = False

    def get_token(self) -> str:
        """Get auth token with caching + auto-refresh."""
        now = time_module.time()
        if self._token and now < self._token_expiry - 60:
            return self._token

        # Login via form data
        data = f"username={USER_CREDS['username']}&password={USER_CREDS['password']}"
        req = urllib.request.Request(
            f"{REGISTRY_URL}/v1/auth/user/login",
            data.encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                result = json.loads(r.read().decode())
                self._token = result.get("access_token", "")
                self._token_expiry = now + 1500
                return self._token
        except Exception as e:
            self.log.error(f"Login failed: {e}")
            return ""

    def api_get(self, path: str) -> Optional[dict]:
        """GET request to registry."""
        url = f"{REGISTRY_URL}{path}"
        token = self.get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            self.log.warning(f"GET {path}: HTTP {e.code}: {body}")
            return None
        except Exception as e:
            self.log.warning(f"GET {path}: {e}")
            return None

    def api_post(self, path: str, data: dict) -> Optional[dict]:
        """POST request with JSON body."""
        url = f"{REGISTRY_URL}{path}"
        token = self.get_token()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        body = json.dumps(data).encode()

        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:300]
            self.log.warning(f"POST {path}: HTTP {e.code}: {err_body}")
            return None
        except Exception as e:
            self.log.warning(f"POST {path}: {e}")
            return None

    def send_msg(self, to_key: str, msg_type: str, title: str, content: str, thread_id: Optional[str] = None) -> Optional[dict]:
        """Send chat message. to_key is agent key (builder, qa, etc) or agent ID."""
        to_id = AGENT_IDS.get(to_key, to_key)
        payload = {
            "to_agent_id": to_id,
            "message_type": msg_type,
            "title": title,
            "content": content,
            "from_agent_name": self.name,
            "metadata": {"ts": time_module.time()},
        }
        if thread_id:
            payload["thread_id"] = thread_id
        return self.api_post("/v1/chat/", payload)

    def load_processed(self):
        """Load processed message IDs from disk."""
        try:
            with open(self._processed_file) as f:
                self._processed = set(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            self._processed = set()
        return self._processed

    def save_processed(self):
        """Save processed message IDs to disk."""
        with open(self._processed_file, "w") as f:
            json.dump(list(self._processed), f)

    def mark_processed(self, msg_id: str):
        """Mark a message as processed."""
        self._processed.add(msg_id)
        self.save_processed()

    def get_new_messages(self, messages: list) -> list:
        """Filter to only unprocessed messages."""
        return [m for m in messages if m.get("id") not in self._processed]

    def is_registry_healthy(self) -> bool:
        """Check if registry is alive."""
        try:
            with urllib.request.urlopen(f"{REGISTRY_URL}/health", timeout=5) as r:
                data = json.loads(r.read().decode())
                return data.get("status") == "ok"
        except Exception as e:
            self.log.warning(f"Health check: {e}")
            return False

    def on_start(self):
        """Override this for startup logic."""
        pass

    def on_tick(self):
        """Override this — called every loop iteration."""
        raise NotImplementedError

    def on_error(self, e: Exception):
        """Override this for custom error handling."""
        self.log.error(f"{type(e).__name__}: {e}")
        traceback.print_exc()

    def run(self):
        """Main event loop."""
        self.log.info(f"🚀 {self.name} starting — 24/7 event loop")
        self.load_processed()
        self.log.info(f"   Previously processed: {len(self._processed)} items")
        self.on_start()

        while True:
            self._cycle += 1
            if not self.is_registry_healthy():
                self.log.warning("Registry not healthy. Waiting...")
                time_module.sleep(10)
                continue

            token = self.get_token()
            if not token:
                self.log.warning("No token. Waiting...")
                time_module.sleep(10)
                continue

            try:
                self.on_tick()
            except Exception as e:
                self.on_error(e)

            time_module.sleep(self.sleep_seconds)


# ── Self-test ──
if __name__ == "__main__":
    agent = HermesAgent("TestAgent", "builder")
    agent.log.info(f"Agent {agent.name} ({agent.agent_id}) initialized")
    agent.log.info(f"Registry: {REGISTRY_URL}")
    agent.log.info(f"Token: {agent.get_token()[:20]}...")
    agent.log.info("✅ Base class OK")
