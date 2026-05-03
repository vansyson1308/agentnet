"""
Weather Agent — sample AgentNet capability provider.

Polls task sessions assigned to it, fetches a real weather forecast
from Open-Meteo (no API key needed), confirms the task with the SDK
which charges the caller's wallet via escrow.

Usage::

    python examples/weather_agent/weather_agent.py \
        --email weather@example.com \
        --password "Strong-Pass-1!"
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

import httpx

# Make the SDK importable without `pip install -e`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "sdk" / "python"))

from agentnet import AgentNetClient  # noqa: E402

CAPABILITY: Dict[str, Any] = {
    "name": "weather_lookup",
    "description": "Forecast for a city via Open-Meteo (free, keyless).",
    "input_schema": {
        "type": "object",
        "properties": {
            "latitude": {"type": "number", "minimum": -90, "maximum": 90},
            "longitude": {"type": "number", "minimum": -180, "maximum": 180},
        },
        "required": ["latitude", "longitude"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "temperature_c": {"type": "number"},
            "wind_kph": {"type": "number"},
            "weathercode": {"type": "integer"},
            "fetched_at": {"type": "string"},
        },
    },
    "price": 5,
}


def fetch_weather(lat: float, lon: float) -> Dict[str, Any]:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
    }
    resp = httpx.get(url, params=params, timeout=10.0)
    resp.raise_for_status()
    cw = resp.json().get("current_weather") or {}
    return {
        "temperature_c": cw.get("temperature"),
        "wind_kph": cw.get("windspeed"),
        "weathercode": cw.get("weathercode"),
        "fetched_at": cw.get("time"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default=os.getenv("WEATHER_EMAIL", "weather@example.com"))
    parser.add_argument("--password", default=os.getenv("WEATHER_PASSWORD", "Strong-Pass-1!"))
    parser.add_argument("--name", default="weather_agent")
    parser.add_argument(
        "--registry-url",
        default=os.getenv("REGISTRY_URL", "http://localhost:8000"),
    )
    parser.add_argument(
        "--payment-url",
        default=os.getenv("PAYMENT_URL", "http://localhost:8001"),
    )
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("weather-agent")

    with AgentNetClient(registry_url=args.registry_url, payment_url=args.payment_url) as c:
        try:
            c.register_user(args.email, args.password)
        except Exception:  # already exists is fine
            pass
        c.login_user(args.email, args.password)

        try:
            agent = c.create_agent(
                name=args.name,
                description="Free weather lookup powered by Open-Meteo.",
                capabilities=[CAPABILITY],
                endpoint=os.getenv("WEATHER_ENDPOINT", "http://localhost:9101"),
                public_key="placeholder",
            )
        except Exception:
            agent = c.get_agent_by_name(args.name)
        log.info("agent ready: id=%s", agent.id)

        # Make sure the wallet has some seed credits so this agent can
        # itself originate tasks (in case the orchestrator example needs it).
        try:
            wallet = c.get_agent_wallet(agent.id)
            if wallet.balance_credits < 100:
                c.dev_fund_wallet(wallet.id, 1000, "credits")
        except Exception as e:
            log.warning("could not fund wallet (likely prod mode): %s", e)

        log.info("polling for tasks every %.1fs ...", args.poll_interval)
        while True:
            time.sleep(args.poll_interval)
            try:
                # Fetch tasks where this agent is the callee and status is initiated/in_progress.
                resp = httpx._client.Client(timeout=10.0).get(
                    f"{args.registry_url}/v1/tasks/",
                    headers=c.get_auth_headers(),
                )
                if resp.status_code != 200:
                    continue
                tasks = resp.json()
            except Exception as e:
                log.warning("poll failed: %s", e)
                continue
            for task in tasks:
                if task.get("callee_agent_id") != str(agent.id):
                    continue
                if task.get("status") not in ("initiated", "in_progress"):
                    continue
                task_id = task["id"]
                try:
                    inp = task.get("input") or {}
                    weather = fetch_weather(inp["latitude"], inp["longitude"])
                    c.confirm_task(task_id, weather)
                    log.info("confirmed task %s", task_id)
                except Exception as e:
                    log.error("task %s failed: %s", task_id, e)
                    try:
                        c.fail_task(task_id, str(e)[:200])
                    except Exception:
                        pass


if __name__ == "__main__":
    main()
