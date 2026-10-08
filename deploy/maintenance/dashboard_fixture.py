"""Serve the REAL dashboard app with stable fixture data (visual-test mode).

Used by the experience-contract gate (services/dashboard/tests/
test_experience_contract.py) and ``browser_probe.py --local-dashboard``:
the actual Flask app and templates, with the registry client replaced by
fixed, realistic data (capabilities are structured objects, as the API
returns them). No network, no credentials, no randomness.
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Iterator

FIXTURE_AGENTS = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "name": "EchoAgent",
        "description": "Echoes its input",
        "capabilities": [{"name": "echo", "description": "Echo the input", "price": 1}, {"name": "summarize", "description": "Summarize text", "price": 3}],
        "success_rate": 0.97,
        "total_tasks_completed": 40,
        "total_tasks_failed": 1,
        "total_tasks_timeout": 0,
        "reputation_tier": "gold",
        "status": "active",
        "rating": 4.8,
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "name": "TranslateAgent",
        "description": "Translates text",
        "capabilities": [{"name": "translate", "description": "Translate text", "price": 2}],
        "success_rate": 0.88,
        "total_tasks_completed": 12,
        "total_tasks_failed": 2,
        "total_tasks_timeout": 1,
        "reputation_tier": "silver",
        "status": "active",
        "rating": 4.1,
    },
]


def _patch_client(client) -> None:
    client.health_registry = lambda timeout=2.0: True
    client.fetch_agents = lambda *a, **k: [dict(x) for x in FIXTURE_AGENTS]
    client.fetch_agent = lambda agent_id: next((dict(x) for x in FIXTURE_AGENTS if x["id"] == agent_id), dict(FIXTURE_AGENTS[0]))
    client.fetch_a2a_network_card = lambda: {
        "name": "AgentNet", "version": "1.0", "description": "AgentNet A2A gateway",
        "supportedInterfaces": [
            {"url": "https://api.agentnet.test/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
            {"url": "https://api.agentnet.test/a2a/http", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
        ],
        "capabilities": {"streaming": True, "pushNotifications": False, "extensions": []},
        "skills": [], "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
    }
    client.fetch_a2a_conformance = lambda: {
        "protocolVersion": "1.0", "specRelease": "1.0.1", "sdk": {"python": "a2a-sdk==1.1.5"},
        "bindings": [{"protocolBinding": "JSONRPC", "url": "https://api.agentnet.test/a2a"}, {"protocolBinding": "HTTP+JSON", "url": "https://api.agentnet.test/a2a/http"}],
        "notOffered": ["gRPC", "push notifications"], "operations": {"SendMessage": "supported", "GetTask": "supported", "CancelTask": "supported before the agent starts"},
        "multiTenancy": "tenant = marketplace agent id", "security": {"scheme": "HTTP Bearer", "credentials": ["agent JWT"]}, "extensions": [], "limits": {}, "evidence": {"tests": []},
    }
    client.fetch_federation_summary = lambda: {"remoteAgentsByState": {"active": 1}, "total": 1}
    client.fetch_agent_a2a_card = lambda agent_id: None


@contextlib.contextmanager
def serve_dashboard() -> Iterator[str]:
    """Yield the base URL of the real dashboard on an ephemeral local port."""
    os.environ.setdefault("ENVIRONMENT", "development")
    from werkzeug.serving import make_server

    from services.dashboard.app import main as dash

    _patch_client(dash.api_client)
    srv = make_server("127.0.0.1", 0, dash.app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        t.join(timeout=5)


#: The critical public journeys (names match public_surface_contract.json items).
CRITICAL_PAGES = [
    {"name": "ui_root", "path": "/"},
    {"name": "landing", "path": "/landing", "expected_final_path": "/landing"},
    {"name": "metaverse", "path": "/metaverse", "expected_final_path": "/metaverse"},
    {"name": "marketplace", "path": "/marketplace", "expected_final_path": "/marketplace"},
    {"name": "network", "path": "/network", "expected_final_path": "/network"},
    {"name": "login", "path": "/login", "expected_final_path": "/login"},
    {"name": "register", "path": "/register", "expected_final_path": "/register"},
]
