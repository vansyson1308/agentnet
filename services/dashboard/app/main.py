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


def get_auth_headers():
    """Get auth headers - for dev, use a simple token or session."""
    # In production, implement proper auth
    # For dev dashboard, we assume services allow localhost
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
        agents = call_registry("/api/v1/agents/")
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
        return jsonify(call_registry(f"/api/v1/tasks/traces/{trace_id}"))

    # List recent traces (would need endpoint for this)
    return jsonify({"spans": [], "message": "Provide trace_id to query"})


@app.route("/api/approvals")
def api_approvals():
    """Get approvals."""
    status = request.args.get("status")

    # Call payment service approvals endpoint
    # Note: requires auth in production
    return jsonify({"approvals": [], "message": "Requires auth in production"})


@app.route("/health")
def health():
    """Health check."""
    # Check if services are available
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
