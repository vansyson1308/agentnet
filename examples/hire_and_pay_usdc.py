"""Hire an agent and pay it in USDC (staging by default).

Usage:
    AGENTNET_API_KEY=... python examples/hire_and_pay_usdc.py --capability summarize --max-spend 1.50
"""

import argparse
import inspect
import os
import sys
import time
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://staging.agentnet.dev"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TERMINAL_STATES = {"completed", "confirmed", "failed", "cancelled", "expired", "refunded", "disputed"}


def _scrub(text, secret):
    text = str(text)
    return text.replace(secret, "[redacted]") if secret else text


def check_base_url(url, allow_production):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("invalid base URL")
    local = host in LOCAL_HOSTS
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ValueError("refusing non-https base URL (only localhost may use http)")
    if not local and "staging" not in host and not allow_production:
        raise ValueError("refusing production host without --allow-production")
    return url


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def agent_price(agent, capability):
    fallback = None
    for cap in _field(agent, "capabilities", None) or []:
        if not isinstance(cap, dict):
            continue
        price = None
        for key in ("price", "price_usdc", "price_per_call"):
            if cap.get(key) is not None:
                price = float(cap[key])
                break
        if price is None:
            continue
        if capability in (cap.get("name"), cap.get("id")):
            return price
        if fallback is None:
            fallback = price
    return fallback


def _default_client_factory(base_url, api_key):
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "sdk", "python"))
    from agentnet.client import AgentNetClient

    client = AgentNetClient(registry_url=base_url, payment_url=base_url)
    client._agent_token = api_key
    return client


def _create_task_once(client, agent, capability, price):
    agent_id = _field(agent, "id")
    candidates = {
        "capability": capability,
        "agent_id": agent_id,
        "provider_agent_id": agent_id,
        "to_agent_id": agent_id,
        "escrow_amount": price,
        "amount": price,
        "currency": "USDC",
    }
    try:
        params = inspect.signature(client.create_task).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs = {k: v for k, v in candidates.items() if k in params}
    return client.create_task(**kwargs)


def main(argv=None, client_factory=None, sleep=time.sleep):
    parser = argparse.ArgumentParser(description="Hire an agent and pay in USDC")
    parser.add_argument("--capability", required=True)
    parser.add_argument("--max-spend", type=float, required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--allow-production", action="store_true")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-attempts", type=int, default=30)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args(argv)

    api_key = os.environ.get("AGENTNET_API_KEY", "")
    if not api_key:
        print("error: AGENTNET_API_KEY is not set", file=sys.stderr)
        return 2

    try:
        base_url = check_base_url(args.base_url, args.allow_production)
    except ValueError as exc:
        print("error: " + _scrub(exc, api_key), file=sys.stderr)
        return 2

    factory = client_factory or _default_client_factory
    try:
        client = factory(base_url, api_key)
        agents = client.search_agents(capability=args.capability, max_price=args.max_spend)
        if not agents:
            print("error: no agent found for capability", file=sys.stderr)
            return 1
        agent = agents[0]
        price = agent_price(agent, args.capability)
        if price is None:
            print("error: agent price unknown; aborting", file=sys.stderr)
            return 1
        print("Agent: %s price: %s USDC" % (_field(agent, "name", ""), price))
        if price > args.max_spend:
            print("error: price %s exceeds --max-spend %s; aborting" % (price, args.max_spend), file=sys.stderr)
            return 1

        task = _create_task_once(client, agent, args.capability, price)
        task_id = _field(task, "id")
        print("Created escrow task: %s" % task_id)

        deadline = time.monotonic() + args.timeout
        final = None
        for attempt in range(max(1, args.max_attempts)):
            current = client.get_task(task_id)
            status = str(_field(current, "status", "")).lower()
            if status in TERMINAL_STATES:
                final = current
                break
            if time.monotonic() >= deadline:
                break
            sleep(args.poll_interval)
        if final is None:
            print("error: task %s did not reach a terminal state in time" % task_id, file=sys.stderr)
            return 1
        print("Task %s finished with status: %s" % (task_id, _field(final, "status")))
        print("Result: %s" % (_field(final, "result", final),))
        return 0
    except Exception as exc:
        print("error: " + _scrub("%s: %s" % (type(exc).__name__, exc), api_key), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
