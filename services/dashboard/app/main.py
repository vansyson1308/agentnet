"""
Minimal Dashboard for AgentNet Local Dev.

Provides:
- Overview: wallet totals, transaction counts, pending approvals
- Traces: query spans by agent_id, task_session_id
- Approvals: list pending approvals

Run:
    cd services/dashboard
    pip install -r requirements.txt
    python -m app.main

Access at http://localhost:8080
"""

import logging
import os

import httpx
from flask import Flask, jsonify, render_template, request

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)

# Configuration
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:8000")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://localhost:8001")


_cached_token = {"token": None, "expires": 0}


def get_auth_headers():
    """Get auth headers using a service account for dashboard API calls."""
    import time

    now = time.time()
    if _cached_token["token"] and _cached_token["expires"] > now:
        return {"Authorization": f"Bearer {_cached_token['token']}"}

    # Try to login as dashboard service user
    dashboard_email = os.getenv("DASHBOARD_USER_EMAIL", "")
    dashboard_password = os.getenv("DASHBOARD_USER_PASSWORD", "")

    if not dashboard_email or not dashboard_password:
        return {}

    try:
        resp = httpx.post(
            f"{REGISTRY_URL}/v1/auth/user/login",
            data={"username": dashboard_email, "password": dashboard_password},
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            _cached_token["token"] = data.get("access_token")
            _cached_token["expires"] = now + 3500
            return {"Authorization": f"Bearer {_cached_token['token']}"}
    except Exception as e:
        logger.warning(f"Dashboard auto-login failed: {e}")

    return {}


def call_registry(endpoint: str, method: str = "GET", data: dict = None):
    """Call registry service."""
    url = f"{REGISTRY_URL}{endpoint}"
    try:
        if method == "GET":
            resp = httpx.get(url, headers=get_auth_headers(), timeout=10.0)
        else:
            resp = httpx.post(url, json=data, headers=get_auth_headers(), timeout=10.0)
        return resp.json() if resp.status_code == 200 else {"error": resp.text}
    except Exception as e:
        logger.error(f"Error calling registry: {e}")
        return {"error": str(e)}


def call_payment(endpoint: str, method: str = "GET", data: dict = None):
    """Call payment service."""
    url = f"{PAYMENT_URL}{endpoint}"
    try:
        if method == "GET":
            resp = httpx.get(url, headers=get_auth_headers(), timeout=10.0)
        else:
            resp = httpx.post(url, json=data, headers=get_auth_headers(), timeout=10.0)
        return resp.json() if resp.status_code == 200 else {"error": resp.text}
    except Exception as e:
        logger.error(f"Error calling payment: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────


@app.route("/")
def index():
    """Dashboard home."""
    return render_template("index.html")


@app.route("/api/overview")
def api_overview():
    """Get overview metrics."""
    result = {
        "agents": {"count": 0, "active": 0},
        "wallets": {"total_credits": 0, "reserved_credits": 0, "count": 0},
        "transactions": {"total": 0, "completed": 0, "pending": 0},
        "approvals": {"pending": 0, "approved": 0, "denied": 0},
        "tasks": {"total": 0, "completed": 0, "failed": 0, "timeout": 0},
    }

    # Get agents count
    try:
        agents = call_registry("/v1/agents/")
        if isinstance(agents, list):
            result["agents"]["count"] = len(agents)
            result["agents"]["active"] = sum(1 for a in agents if a.get("status") == "active")
    except:
        pass

    # Note: For full metrics, would need direct DB access
    # For now, return mock data if services not available
    # In production, implement /metrics/overview endpoints in services

    return jsonify(result)


@app.route("/api/traces")
def api_traces():
    """Query traces/spans."""
    trace_id = request.args.get("trace_id")
    agent_id = request.args.get("agent_id")
    task_session_id = request.args.get("task_session_id")

    if trace_id:
        return jsonify(call_registry(f"/v1/tasks/traces/{trace_id}"))

    # List recent traces (would need endpoint for this)
    return jsonify({"spans": [], "message": "Provide trace_id to query"})


@app.route("/api/approvals")
def api_approvals():
    """Get approvals."""
    status = request.args.get("status")

    # Call payment service approvals endpoint
    # Note: requires auth in production
    return jsonify({"approvals": [], "message": "Requires auth in production"})


@app.route("/api/agents")
def api_agents():
    """Get all agents with details."""
    agents = call_registry("/v1/agents/")
    if isinstance(agents, list):
        return jsonify({"agents": agents})
    return jsonify({"agents": [], "error": agents.get("error", "Failed to fetch agents")})


@app.route("/api/agents/<agent_id>/card")
def api_agent_card(agent_id):
    """Get A2A Agent Card for a specific agent."""
    return jsonify(call_registry(f"/v1/agents/{agent_id}/a2a-card"))


@app.route("/api/registry-card")
def api_registry_card():
    """Get the registry's A2A Agent Card."""
    try:
        resp = httpx.get(f"{REGISTRY_URL}/.well-known/agent-card.json", timeout=10.0)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/approvals/<approval_id>/approve", methods=["POST"])
def api_approve(approval_id):
    """Approve a pending approval request."""
    return jsonify(call_payment(f"/v1/approval_requests/{approval_id}/approve", method="POST"))


@app.route("/api/approvals/<approval_id>/deny", methods=["POST"])
def api_deny(approval_id):
    """Deny a pending approval request."""
    return jsonify(call_payment(f"/v1/approval_requests/{approval_id}/deny", method="POST"))


@app.route("/health")
def health():
    """Health check."""
    services = {}
    try:
        httpx.get(f"{REGISTRY_URL}/health", timeout=2.0)
        services["registry"] = "ok"
    except:
        services["registry"] = "unavailable"

    try:
        httpx.get(f"{PAYMENT_URL}/health", timeout=2.0)
        services["payment"] = "ok"
    except:
        services["payment"] = "unavailable"

    return jsonify({"status": "ok", "services": services})


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
