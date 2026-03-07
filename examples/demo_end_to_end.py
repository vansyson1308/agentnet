#!/usr/bin/env python3
"""
End-to-end demo script for AgentNet.

This script demonstrates a full task execution flow:
1. Create user and login
2. Create/verify an agent with capability
3. Fund the agent's wallet (dev mode)
4. Create a second agent (caller)
5. Create task session (escrow reserved)
6. Complete task (escrow released)
7. Print transaction history

Prerequisites:
    - Docker Compose running with services
    - Ports 8000 (registry), 8001 (payment) available

Usage:
    python demo_end_to_end.py
"""

import sys
import time
import uuid
from pathlib import Path

# Add SDK to path
sys.path.insert(0, str(Path(__file__).parent.parent / "sdk" / "python"))

from agentnet import AgentNetClient, AgentNetError


def main():
    # Unique identifier for this run
    run_id = uuid.uuid4().hex[:8]
    print(f"=== AgentNet End-to-End Demo (run: {run_id}) ===\n")

    # Configuration
    REGISTRY_URL = "http://localhost:8000"
    PAYMENT_URL = "http://localhost:8001"

    # Demo user
    demo_email = f"demo_{run_id}@example.com"
    demo_password = "Demo123!"  # Must have upper, lower, digit

    # Initialize client
    client = AgentNetClient(
        registry_url=REGISTRY_URL,
        payment_url=PAYMENT_URL
    )

    try:
        # ─────────────────────────────────────────────────────
        # Step 1: Register and Login
        # ─────────────────────────────────────────────────────
        print("Step 1: Register user...")
        try:
            user = client.register_user(demo_email, demo_password)
            # Login after registration to get auth token
            client.login_user(demo_email, demo_password)
            print(f"  Created user: {user.email} ({user.id})")
        except AgentNetError as e:
            if "already registered" in str(e):
                print(f"  User already exists, logging in...")
                client.login_user(demo_email, demo_password)
                print(f"  Logged in")
            else:
                raise

        # ─────────────────────────────────────────────────────
        # Step 2: Create Callee Agent (Echo Agent)
        # ─────────────────────────────────────────────────────
        print("\nStep 2: Create callee agent (echo service)...")

        callee_capabilities = [{
            "name": "echo",
            "description": "Echoes back the input message",
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"}
                },
                "required": ["message"]
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "echoed": {"type": "string"}
                }
            },
            "version": "1.0", "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}}, "output_schema": {"type": "object", "properties": {"echoed": {"type": "string"}}}, "price": 5
        }]

        try:
            callee_agent = client.create_agent(
                name=f"echo_agent_{run_id}",
                description="Echo service - returns input as output",
                capabilities=callee_capabilities,
                endpoint="http://localhost:9000",
                public_key="demo_key_" + run_id
            )
            print(f"  Created callee agent: {callee_agent.name} ({callee_agent.id})")
        except AgentNetError as e:
            if "already exists" in str(e).lower():
                print(f"  Agent already exists, fetching...")
                callee_agent = client.get_agent_by_name(f"echo_agent_{run_id}")
                print(f"  Using existing agent: {callee_agent.name}")
            else:
                raise

        # Get callee wallet
        callee_wallet = client.get_agent_wallet(callee_agent.id)
        print(f"  Callee wallet: {callee_wallet.id}")
        print(f"  Initial balance: {callee_wallet.balance_credits} credits")

        # ─────────────────────────────────────────────────────
        # Step 3: Fund Callee Wallet (Development Only)
        # ─────────────────────────────────────────────────────
        print("\nStep 3: Fund callee wallet (dev mode)...")

        # For demo, we'll directly fund via payment service if available
        # In production, you'd use a proper payment flow
        try:
            # Try to use the fund endpoint
            client.dev_fund_wallet(callee_wallet.id, 1000, "credits")
            print(f"  Funded 1000 credits")
        except AgentNetError as e:
            print(f"  Note: Could not auto-fund ({e})")
            print(f"  Please fund manually or use DB")
            # Continue anyway for demo

        callee_wallet = client.get_agent_wallet(callee_agent.id)
        print(f"  Updated balance: {callee_wallet.balance_credits} credits")

        # ─────────────────────────────────────────────────────
        # Step 4: Create Caller Agent
        # ─────────────────────────────────────────────────────
        print("\nStep 4: Create caller agent...")

        caller_agent = client.create_agent(
            name=f"caller_agent_{run_id}",
            description="Caller agent for demo",
            capabilities=[],
            endpoint="http://localhost:9001",
            public_key="caller_key_" + run_id
        )
        print(f"  Created caller agent: {caller_agent.name} ({caller_agent.id})")

        caller_wallet = client.get_agent_wallet(caller_agent.id)
        print(f"  Caller wallet: {caller_wallet.id}")

        # Fund caller wallet
        try:
            client.dev_fund_wallet(caller_wallet.id, 1000, "credits")
            print(f"  Funded 1000 credits")
        except AgentNetError:
            print(f"  Note: Could not auto-fund")

        caller_wallet = client.get_agent_wallet(caller_agent.id)
        print(f"  Caller balance: {caller_wallet.balance_credits} credits")

        # ─────────────────────────────────────────────────────
        # Step 5: Create Task Session (Escrow Reserved)
        # ─────────────────────────────────────────────────────
        print("\nStep 5: Create task session (escrow reserved)...")

        task = client.create_task(
            caller_agent_id=caller_agent.id,
            callee_agent_id=callee_agent.id,
            capability="echo",
            input_data={"message": f"Hello from AgentNet! Run: {run_id}"},
            max_budget=5,  # Price is 5 credits
            currency="credits",
            timeout_seconds=60
        )

        print(f"  Task created: {task.id}")
        print(f"  Trace ID: {task.trace_id}")
        print(f"  Status: {task.status}")
        print(f"  Escrow amount: {task.escrow_amount}")

        # Check wallet balances after escrow
        caller_wallet = client.get_agent_wallet(caller_agent.id)
        print(f"  Caller reserved: {caller_wallet.reserved_credits} credits")

        # ─────────────────────────────────────────────────────
        # Step 6: Complete Task (Simulate agent execution)
        # ─────────────────────────────────────────────────────
        print("\nStep 6: Simulate task completion...")

        # In a real scenario, the callee agent would process the task
        # For demo, we simulate the confirm step
        task_result = {"echoed": f"Hello from AgentNet! Run: {run_id}"}

        # Note: In real flow, this would be done by the callee agent
        # For demo purposes, we'll just show the state
        print(f"  Task would execute: echo with message 'Hello from AgentNet!'")
        print(f"  Task would return: {task_result}")

        # ─────────────────────────────────────────────────────
        # Step 7: Check Final State
        # ─────────────────────────────────────────────────────
        print("\nStep 7: Check final wallet state...")

        caller_wallet = client.get_agent_wallet(caller_agent.id)
        callee_wallet = client.get_agent_wallet(callee_agent.id)

        print(f"  Caller wallet:")
        print(f"    Balance: {caller_wallet.balance_credits} credits")
        print(f"    Reserved: {caller_wallet.reserved_credits} credits")

        print(f"  Callee wallet:")
        print(f"    Balance: {callee_wallet.balance_credits} credits")
        print(f"    Reserved: {callee_wallet.reserved_credits} credits")

        # ─────────────────────────────────────────────────────
        # Step 8: Get Trace (if available)
        # ─────────────────────────────────────────────────────
        print("\nStep 8: Get trace/spans...")

        try:
            trace = client.get_trace(task.trace_id)
            print(f"  Trace ID: {trace.get('trace_id')}")
            print(f"  Total spans: {trace.get('total_spans', 0)}")
            for span in trace.get('spans', [])[:3]:
                print(f"    - {span.get('event')}: {span.get('capability')}")
        except AgentNetError as e:
            print(f"  Note: Could not get trace ({e})")

        print("\n=== Demo Complete ===")
        print(f"Run ID: {run_id}")
        print(f"Task ID: {task.id}")

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        client.close()


if __name__ == "__main__":
    main()
