#!/usr/bin/env python3
"""
agentnet-paperclip-worker — Bridge between Paperclip (control plane) and AgentNet (execution plane)

Endpoints:
- POST /api/agentnet/execute — Receive heartbeat from Paperclip adapter
- POST /api/agentnet/task-complete — Agent gọi khi hoàn thành
- GET  /health — Health check

Flow:
1. Paperclip heartbeat gọi POST /api/agentnet/execute
2. Worker nhận request, parse issue/task data
3. Worker tạo task trong AgentNet registry, dispatch cho agent phù hợp
4. Worker report kết quả về Paperclip
"""
import json
import os
import sys
import time
import logging
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ── Config ──
PAPERCLIP_URL = os.environ.get("PAPERCLIP_URL", "http://localhost:3100")
AGENTNET_URL = os.environ.get("AGENTNET_URL", "http://localhost:8000")
WORKER_PORT = int(os.environ.get("WORKER_PORT", "8003"))
COMPANY_ID = os.environ.get("PAPERCLIP_COMPANY_ID", "bbb50bef-ce01-4cc8-aac9-e33dae6395c0")

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [PaperclipWorker] %(levelname)s| %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("paperclip-worker")

# ── Paperclip API Helpers ──

def _pc_get(path):
    """GET Paperclip API. Path is absolute e.g. /api/issues/{id}"""
    try:
        req = urllib.request.Request(f"{PAPERCLIP_URL}{path}")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning(f"Paperclip GET {path}: {e}")
        return None

def _pc_patch(path, data):
    """PATCH Paperclip API."""
    body = json.dumps(data).encode()
    url = f"{PAPERCLIP_URL}{path}" if path.startswith("/api") else f"{PAPERCLIP_URL}/api{path}"
    req = urllib.request.Request(url, data=body, method="PATCH", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:200]
        log.warning(f"Paperclip PATCH {path}: HTTP {e.code}: {err}")
        return None
    except Exception as e:
        log.warning(f"Paperclip PATCH {path}: {e}")
        return None

def _pc_post(path, data):
    """POST Paperclip API."""
    body = json.dumps(data).encode()
    url = f"{PAPERCLIP_URL}{path}" if path.startswith("/api") else f"{PAPERCLIP_URL}/api{path}"
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:200]
        log.warning(f"Paperclip POST {path}: HTTP {e.code}: {err}")
        return None
    except Exception as e:
        log.warning(f"Paperclip POST {path}: {e}")
        return None

# ── AgentNet API Helpers ──

def _an_get(path):
    """GET AgentNet API."""
    try:
        req = urllib.request.Request(f"{AGENTNET_URL}{path}")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning(f"AgentNet GET {path}: {e}")
        return None

# ── Core Logic ──

def handle_execute(issue_data: dict) -> dict:
    """
    Nhận request từ Paperclip heartbeat hoặc external trigger.
    issue_data chứa thông tin issue cần xử lý.
    """
    issue_id = issue_data.get("id", "")
    issue_title = issue_data.get("title", "")
    issue_description = issue_data.get("description", "")

    log.info(f"Received execute for issue: {issue_id} — {issue_title[:60]}")

    # Sync trạng thái với Paperclip
    _pc_patch(f"/api/issues/{issue_id}", {"status": "in_progress", "assigneeUserId": "admin-001"})

    # Xác định agent type dựa trên title
    title_lower = issue_title.lower()
    if "test" in title_lower or "qa" in title_lower:
        target_agent_type = "qa"
    elif "deploy" in title_lower or "build" in title_lower:
        target_agent_type = "builder"
    elif "design" in title_lower or "ui" in title_lower or "dashboard" in title_lower:
        target_agent_type = "builder"
    elif "werewolf" in title_lower or "game" in title_lower:
        target_agent_type = "builder"
    else:
        target_agent_type = "builder"

    # Tìm agent trong AgentNet registry
    agents = _an_get("/v1/agents/public/")
    target_agent_id = None
    if agents:
        for a in agents:
            name = a.get("name", "").lower()
            if target_agent_type == "builder" and "builder" in name:
                target_agent_id = a.get("id")
                break
            elif target_agent_type == "qa" and "qa" in name:
                target_agent_id = a.get("id")
                break

    if not target_agent_id:
        # Fallback to Hermes_Builder
        if agents:
            for a in agents:
                if "builder" in a.get("name", "").lower():
                    target_agent_id = a.get("id")
                    break

    log.info(f"Mapped to agent: {target_agent_id or 'unknown'} (type={target_agent_type})")

    return {
        "status": "accepted",
        "issue_id": issue_id,
        "issue_title": issue_title,
        "target_agent_id": target_agent_id,
        "target_agent_type": target_agent_type,
    }


def handle_task_complete(data: dict) -> dict:
    """
    Agent gọi khi task hoàn thành.
    Report kết quả về Paperclip issue.
    """
    task_id = data.get("task_id", "")
    status = data.get("status", "done")
    summary = data.get("summary", "")
    paperclip_issue_id = data.get("paperclip_issue_id", "")

    log.info(f"Task {task_id} completed: {status}")

    if paperclip_issue_id:
        # Update Paperclip issue status
        pc_status = "done" if status == "done" else "blocked"
        _pc_patch(f"/api/issues/{paperclip_issue_id}", {"status": pc_status})

        # Add comment
        _pc_post(f"/api/issues/{paperclip_issue_id}/comments", {
            "body": f"**AgentNet Task {task_id}** — Status: {status}\n\n{summary[:500]}"
        })

    return {"status": "ok"}


# ── HTTP Server ──

class WorkerHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            return json.loads(self.rfile.read(length).decode())
        return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json({
                "status": "ok",
                "service": "agentnet-paperclip-worker",
                "paperclip": PAPERCLIP_URL,
                "agentnet": AGENTNET_URL,
                "paperclip_api": self._test_connections(),
            })
        elif parsed.path == "/api/paperclip/issues":
            # Proxy: list issues from Paperclip
            issues = _pc_get(f"/api/companies/{COMPANY_ID}/issues")
            self._send_json(issues or [])
        elif parsed.path == "/api/agentnet/agents":
            # Proxy: list agents from AgentNet
            agents = _an_get("/v1/agents/public/")
            self._send_json(agents or [])
        else:
            self._send_json({"error": "Not found"}, 404)

    def _test_connections(self):
        """Test both API connections."""
        result = {"paperclip": False, "agentnet": False}
        try:
            r = _pc_get("/api/companies")
            result["paperclip"] = r is not None
        except:
            pass
        try:
            r = _an_get("/v1/agents/public/")
            result["agentnet"] = r is not None
        except:
            pass
        return result

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._read_body()
        except Exception as e:
            self._send_json({"error": f"Invalid JSON: {e}"}, 400)
            return

        if parsed.path == "/api/agentnet/execute":
            result = handle_execute(body)
            self._send_json(result)
        elif parsed.path == "/api/agentnet/task-complete":
            result = handle_task_complete(body)
            self._send_json(result)
        else:
            self._send_json({"error": "Not found"}, 404)

    def log_message(self, fmt, *args):
        log.info(f"{self.client_address[0]} - {fmt % args}")


def main():
    log.info(f"Starting AgentNet Paperclip Worker on port {WORKER_PORT}")
    log.info(f"Paperclip URL: {PAPERCLIP_URL}")
    log.info(f"AgentNet URL:  {AGENTNET_URL}")
    log.info(f"Company ID:    {COMPANY_ID}")

    server = HTTPServer(("0.0.0.0", WORKER_PORT), WorkerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
