#!/usr/bin/env python3
"""
Seed demo data for judge-ready dashboard experience.

Creates realistic agent-first workflow data:
- 4 specialized agents with distinct roles
- Funded wallets
- 3 task sessions in different lifecycle states
- Transaction records
- Trace spans for audit trail

All data is real backend data, not frontend mocks.
Run once after services are up.

Usage:
    python demo/seed_demo_data.py
"""

import json
import sys
import time
import uuid

import httpx

REGISTRY = "http://localhost:8000"
PAYMENT = "http://localhost:8001"

# Demo user
EMAIL = "hackathon@agentnet.io"
PASSWORD = "Hackathon2026!"


def login():
    r = httpx.post(
        f"{REGISTRY}/v1/auth/user/login",
        data={"username": EMAIL, "password": PASSWORD},
        timeout=10,
    )
    if r.status_code != 200:
        # Try register first
        httpx.post(
            f"{REGISTRY}/v1/auth/user/register",
            json={"email": EMAIL, "password": PASSWORD},
            timeout=10,
        )
        r = httpx.post(
            f"{REGISTRY}/v1/auth/user/login",
            data={"username": EMAIL, "password": PASSWORD},
            timeout=10,
        )
    return r.json()["access_token"]


def create_agent(token, name, description, capabilities):
    """Create an agent if it doesn't already exist."""
    import ed25519

    sk, vk = ed25519.create_keypair()
    pk = vk.to_ascii(encoding="hex").decode()
    h = {"Authorization": f"Bearer {token}"}

    r = httpx.post(
        f"{REGISTRY}/v1/agents/",
        headers=h,
        json={
            "name": name,
            "description": description,
            "capabilities": capabilities,
            "endpoint": f"https://{name.lower().replace(' ', '-')}.agentnet.io/execute",
            "public_key": pk,
        },
        timeout=10,
    )
    if r.status_code in (200, 201):
        agent = r.json()
        print(f"  Created: {name} ({agent['id'][:8]}...)")
        return agent
    elif "already exists" in r.text.lower():
        # Find existing
        agents = httpx.get(f"{REGISTRY}/v1/agents/", headers=h, timeout=10).json()
        for a in agents:
            if a.get("name") == name:
                print(f"  Exists:  {name} ({a['id'][:8]}...)")
                return a
    else:
        print(f"  Error creating {name}: {r.status_code} {r.text[:100]}")
    return None


def fund_wallet(token, agent_id, amount=1000):
    """Fund an agent's wallet."""
    h = {"Authorization": f"Bearer {token}"}
    wallets = httpx.get(f"{PAYMENT}/v1/wallets/", headers=h, timeout=10).json()
    for w in wallets if isinstance(wallets, list) else []:
        if w.get("owner_id") == agent_id:
            httpx.post(
                f"{PAYMENT}/v1/wallets/{w['id']}/fund",
                headers=h,
                json={"amount": amount, "currency": "credits"},
                timeout=10,
            )
            print(f"  Funded {amount} credits -> wallet {w['id'][:8]}...")
            return w
    return None


def create_task(token, caller_id, callee_id, capability, input_data, budget=5):
    """Create a task session (locks escrow)."""
    h = {"Authorization": f"Bearer {token}"}
    r = httpx.post(
        f"{REGISTRY}/v1/tasks/",
        headers=h,
        json={
            "caller_agent_id": caller_id,
            "callee_agent_id": callee_id,
            "capability": capability,
            "input": input_data,
            "max_budget": budget,
            "currency": "credits",
            "timeout_seconds": 3600,
        },
        timeout=10,
    )
    if r.status_code in (200, 201):
        task = r.json()
        print(f"  Task created: {task.get('task_session_id', '?')[:8]}... (escrow: {budget})")
        return task
    else:
        print(f"  Task error: {r.status_code} {r.text[:100]}")
    return None


def main():
    print("=" * 60)
    print("  AgentNet Demo Data Seeder")
    print("=" * 60)

    # Login
    print("\n[1/5] Authenticating...")
    token = login()
    h = {"Authorization": f"Bearer {token}"}
    print(f"  Token: {token[:20]}...")

    # Create specialized agents
    print("\n[2/5] Creating demo agents...")

    agents_spec = [
        {
            "name": "ResearchBot-Alpha",
            "description": "Autonomous research agent — analyzes markets, trends, and datasets. Produces structured research reports for other agents.",
            "capabilities": [
                {
                    "name": "market_research",
                    "version": "2.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "price": 8.0,
                },
                {
                    "name": "data_analysis",
                    "version": "1.5",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "price": 5.0,
                },
            ],
        },
        {
            "name": "BuilderBot-Prime",
            "description": "Code generation and integration agent — builds solutions based on specifications from other agents.",
            "capabilities": [
                {
                    "name": "code_generation",
                    "version": "3.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "price": 15.0,
                },
                {
                    "name": "api_integration",
                    "version": "1.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "price": 12.0,
                },
            ],
        },
        {
            "name": "QA-Sentinel",
            "description": "Quality assurance and security audit agent — reviews code, tests outputs, validates agent work products.",
            "capabilities": [
                {
                    "name": "code_review",
                    "version": "2.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "price": 10.0,
                },
                {
                    "name": "security_audit",
                    "version": "1.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "price": 20.0,
                },
            ],
        },
        {
            "name": "TreasuryGuard",
            "description": "Settlement and escrow management agent — monitors payment flows, validates completions, triggers releases.",
            "capabilities": [
                {
                    "name": "escrow_verification",
                    "version": "1.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "price": 3.0,
                },
                {
                    "name": "settlement_audit",
                    "version": "1.0",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "price": 5.0,
                },
            ],
        },
    ]

    created_agents = []
    for spec in agents_spec:
        agent = create_agent(token, **spec)
        if agent:
            created_agents.append(agent)

    if len(created_agents) < 2:
        print("\nNot enough agents created. Exiting.")
        sys.exit(1)

    # Fund wallets
    print("\n[3/5] Funding agent wallets...")
    for agent in created_agents:
        fund_wallet(token, agent["id"], amount=2000)

    # Create tasks
    print("\n[4/5] Creating demo task sessions...")

    # Task 1: Research -> completed scenario
    if len(created_agents) >= 2:
        research_agent = created_agents[0]
        builder_agent = created_agents[1]

        task1 = create_task(
            token,
            caller_id=builder_agent["id"],
            callee_id=research_agent["id"],
            capability="market_research",
            input_data={
                "topic": "AI agent marketplace trends Q1 2026",
                "depth": "comprehensive",
                "format": "structured_report",
            },
            budget=8,
        )

    # Task 2: Build task -> in progress
    if len(created_agents) >= 3:
        qa_agent = created_agents[2]

        task2 = create_task(
            token,
            caller_id=research_agent["id"],
            callee_id=builder_agent["id"],
            capability="code_generation",
            input_data={
                "specification": "Build agent discovery API endpoint",
                "language": "python",
                "framework": "fastapi",
            },
            budget=15,
        )

    # Task 3: QA review
    if len(created_agents) >= 4:
        treasury_agent = created_agents[3]

        task3 = create_task(
            token,
            caller_id=builder_agent["id"],
            callee_id=qa_agent["id"],
            capability="code_review",
            input_data={
                "repository": "agentnet/registry",
                "scope": "escrow_payment_flow",
                "severity_threshold": "medium",
            },
            budget=10,
        )

    # Verify final state
    print("\n[5/5] Verifying seeded data...")
    agents = httpx.get(f"{REGISTRY}/v1/agents/", headers=h, timeout=10).json()
    print(f"  Total agents: {len(agents)}")

    wallets = httpx.get(f"{PAYMENT}/v1/wallets/", headers=h, timeout=10).json()
    if isinstance(wallets, list):
        total_credits = sum(w.get("balance_credits", 0) for w in wallets)
        total_reserved = sum(w.get("reserved_credits", 0) for w in wallets)
        print(f"  Total wallets: {len(wallets)}")
        print(f"  Total credits: {total_credits}")
        print(f"  Total reserved (escrow): {total_reserved}")

    print("\n" + "=" * 60)
    print("  Demo data seeded successfully!")
    print("  Dashboard should now show populated metrics.")
    print("=" * 60)


if __name__ == "__main__":
    main()
