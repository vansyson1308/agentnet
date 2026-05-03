"""
Orchestrator example — drives a real transaction through the agent
economy and asserts the money invariants hold.

Run after the registry / payment / worker stack is up AND a callee
agent (e.g. examples/weather_agent/weather_agent.py) is running and
serving the ``weather_lookup`` capability.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "sdk" / "python"))

from agentnet import AgentNetClient  # noqa: E402

log = logging.getLogger("orchestrator")


def _wait_for_terminal(c: AgentNetClient, task_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = c.get_task(task_id)
        status = task.get("status")
        if status in ("completed", "failed", "timeout", "refunded"):
            return task
        time.sleep(1.0)
    raise TimeoutError(f"task {task_id} did not reach a terminal state in {timeout}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry-url", default=os.getenv("REGISTRY_URL", "http://localhost:8000"))
    parser.add_argument("--payment-url", default=os.getenv("PAYMENT_URL", "http://localhost:8001"))
    parser.add_argument("--caller-email", default="orchestrator@example.com")
    parser.add_argument("--caller-password", default="Strong-Pass-Orch-1!")
    parser.add_argument("--callee-name", default="weather_agent")
    parser.add_argument("--latitude", type=float, default=21.028511)
    parser.add_argument("--longitude", type=float, default=105.804817)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with AgentNetClient(registry_url=args.registry_url, payment_url=args.payment_url) as c:
        try:
            c.register_user(args.caller_email, args.caller_password)
        except Exception:
            pass
        c.login_user(args.caller_email, args.caller_password)

        try:
            caller = c.create_agent(
                name="orchestrator_agent",
                description="Caller demo agent.",
                capabilities=[],
                endpoint="http://localhost:9999",
                public_key="placeholder",
            )
        except Exception:
            caller = c.get_agent_by_name("orchestrator_agent")

        # Seed funds in dev so escrow lock succeeds.
        wallet = c.get_agent_wallet(caller.id)
        if wallet.balance_credits < 50:
            c.dev_fund_wallet(wallet.id, 1000, "credits")
            wallet = c.get_agent_wallet(caller.id)
        log.info("caller wallet before task: balance=%d", wallet.balance_credits)

        callee = c.get_agent_by_name(args.callee_name)

        task = c.create_task(
            caller_agent_id=caller.id,
            callee_agent_id=callee.id,
            capability="weather_lookup",
            input_data={"latitude": args.latitude, "longitude": args.longitude},
            max_budget=10,
            currency="credits",
        )
        log.info("created task %s (trace %s)", task.id, task.trace_id)

        terminal = _wait_for_terminal(c, task.id)
        log.info("task terminal status: %s", terminal.get("status"))
        if terminal.get("status") != "completed":
            raise SystemExit(
                f"task did not complete: {terminal.get('status')} - {terminal.get('error_message')}"
            )

        # Verify money invariant.
        wallet_after = c.get_agent_wallet(caller.id)
        spent = wallet.balance_credits - wallet_after.balance_credits
        log.info("caller wallet after task: balance=%d (spent %d)", wallet_after.balance_credits, spent)
        assert spent == 5, f"expected 5 credits spent, got {spent}"

        callee_wallet = c.get_agent_wallet(callee.id)
        log.info("callee wallet after task: balance=%d", callee_wallet.balance_credits)

        # Trace contains create + complete spans.
        trace = c.get_trace(str(task.trace_id))
        events = [s.get("event") for s in trace.get("spans", [])]
        log.info("trace events: %s", events)
        assert "task_created" in events
        assert "task_completed" in events
        log.info("OK — money invariant holds, trace is queryable.")


if __name__ == "__main__":
    main()
