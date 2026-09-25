"""Dashboard A2A Network pages (Phase 8): rendered from PUBLIC registry
surfaces only, truthful when the gateway is off, and never showing an
agent's private fields (the registry card is already sanitized)."""

from __future__ import annotations

import pytest

AGENT_ID = "0b7d6c1e-0000-4000-8000-000000000001"
ECON = "https://agentnet.io.vn/a2a/extensions/economics/v1"
NETWORK_CARD = {
    "name": "AgentNet",
    "supportedInterfaces": [
        {"url": "https://api.agentnet.io.vn/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        {"url": "https://api.agentnet.io.vn/a2a/http", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
    ],
    "capabilities": {"streaming": True, "extensions": [{"uri": ECON}]},
}
AGENT_CARD = {
    "name": "Translator",
    "description": "Translates text",
    "supportedInterfaces": [
        {"url": "https://api.agentnet.io.vn/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0", "tenant": AGENT_ID},
    ],
    "capabilities": {"streaming": True, "extensions": [{"uri": ECON, "params": {"skillPrices": {"translate": 12.0, "detect": 0.0}}}]},
    "skills": [{"id": "translate", "description": "translate text"}, {"id": "detect", "description": "detect language"}],
}


@pytest.fixture
def dash(monkeypatch):
    from services.dashboard.app import main

    main.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return main, main.app.test_client()


def test_network_page_says_off_when_the_gateway_is_off(dash, monkeypatch):
    main, client = dash
    monkeypatch.setattr(main.api_client, "fetch_a2a_network_card", lambda: None)
    r = client.get("/network")
    assert r.status_code == 200 and b"not enabled" in r.data and b"LIVE" not in r.data


def test_network_page_renders_live_status_endpoints_and_agents(dash, monkeypatch):
    main, client = dash
    monkeypatch.setattr(main.api_client, "fetch_a2a_network_card", lambda: NETWORK_CARD)
    monkeypatch.setattr(main.api_client, "fetch_a2a_conformance", lambda: {"operations": {"SendMessage": "supported", "GetExtendedAgentCard": "unsupported (-32007)"}})
    monkeypatch.setattr(main.api_client, "fetch_federation_summary", lambda: {"total": 2, "remoteAgentsByState": {"verified": 1, "discovered": 1}})
    monkeypatch.setattr(main.api_client, "fetch_agents", lambda **kw: [{"id": AGENT_ID, "name": "Translator"}])
    r = client.get("/network")
    body = r.data.decode()
    assert r.status_code == 200 and "LIVE" in body
    assert "https://api.agentnet.io.vn/a2a/http" in body and "unsupported (-32007)" in body
    assert f"/network/agents/{AGENT_ID}" in body and "verified" in body


def test_agent_a2a_page_shows_tenant_skills_and_prices(dash, monkeypatch):
    main, client = dash
    monkeypatch.setattr(main.api_client, "fetch_agent_a2a_card", lambda agent_id: AGENT_CARD)
    r = client.get(f"/network/agents/{AGENT_ID}")
    body = r.data.decode()
    assert r.status_code == 200
    assert f"tenant {AGENT_ID}" in body and "translate" in body and "12 · escrow" in body and "free" in body
    assert ECON in body


def test_agent_page_rejects_non_uuid_and_unknown_agents(dash, monkeypatch):
    main, client = dash
    assert client.get("/network/agents/../../etc").status_code in (302, 404)
    monkeypatch.setattr(main.api_client, "fetch_agent_a2a_card", lambda agent_id: None)
    assert client.get(f"/network/agents/{AGENT_ID}").status_code == 302
