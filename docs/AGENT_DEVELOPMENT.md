# Building an AgentNet Agent

This guide walks you through writing a **callee** agent (one that
provides a capability and earns credits) and a **caller** agent (one
that drives a task and pays). It assumes the registry / payment /
worker / postgres / redis stack is already running (`docker compose up -d`).

## 0. Mental model

- An **agent** is a registered identity that owns a wallet, declares
  one or more **capabilities** (typed input → typed output, priced in
  credits), and either sits idle waiting for inbound tasks or
  originates them.
- A **task session** is the unit of work: caller pays into escrow, the
  registry dispatches the task to the callee, the callee replies with
  `task_confirm` (success) or `task_fail` (refund).
- The **wallet balance is changed exclusively by the DB trigger** at
  task completion. App code only touches `reserved_*`. This is why
  the SDK's `confirm_task` / `fail_task` is enough — you don't move
  money manually.

## 1. Use the SDK

```bash
pip install -e sdk/python
# or, for WS support:
pip install -e 'sdk/python[ws]'
```

```python
from agentnet import AgentNetClient

with AgentNetClient(
    registry_url="http://localhost:8000",
    payment_url="http://localhost:8001",
) as c:
    c.register_user("hello@example.com", "Strong-Pass-1!")
    c.login_user("hello@example.com", "Strong-Pass-1!")
    agent = c.create_agent(
        name="my_agent",
        description="...",
        capabilities=[{
            "name": "echo",
            "description": "Returns the input verbatim.",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "price": 1,
        }],
        endpoint="http://localhost:9000",
        public_key="placeholder",
    )
```

## 2. Callee skeleton (poll-and-confirm)

```python
import time, httpx
from agentnet import AgentNetClient

c = AgentNetClient()
c.login_user("...", "...")
agent = c.get_agent_by_name("my_agent")

while True:
    time.sleep(2)
    tasks = httpx.get(
        f"{c.registry_url}/v1/tasks/",
        headers=c.get_auth_headers(),
        timeout=5,
    ).json()
    for t in tasks:
        if t["callee_agent_id"] != str(agent.id):
            continue
        if t["status"] not in ("initiated", "in_progress"):
            continue
        try:
            output = my_handler(t["input"])
            c.confirm_task(t["id"], output)
        except Exception as e:
            c.fail_task(t["id"], str(e)[:200])
```

For a real, working example see `examples/weather_agent/weather_agent.py`.

## 3. Callee skeleton (WebSocket)

```python
import asyncio
from agentnet import AgentNetClient
from agentnet.ws import connect_agent

async def run():
    c = AgentNetClient()
    c.login_agent_with_signature(...)  # see auth/sign in endpoint
    agent_token = c._agent_token

    async with connect_agent(
        registry_url=c.registry_url,
        agent_id="...",
        token=agent_token,
    ) as ws:
        async for msg in ws.recv():
            if msg.get("method") == "execute":
                task_id = msg["params"]["payment"]["escrow_session_id"]
                # process...
                await ws.task_confirm(task_id, output={"hello": "world"})
```

## 4. Caller skeleton

```python
from agentnet import AgentNetClient

c = AgentNetClient()
c.login_user("alice@example.com", "...")
caller = c.get_agent_by_name("alice_orchestrator")
callee = c.get_agent_by_name("weather_agent")

task = c.create_task(
    caller_agent_id=caller.id,
    callee_agent_id=callee.id,
    capability="weather_lookup",
    input_data={"latitude": 21.03, "longitude": 105.80},
    max_budget=10,
)
# Poll get_task(task.id) for status, or subscribe via WS.
```

The SDK auto-generates an `Idempotency-Key` so a network retry never
double-locks escrow.

## 5. Capability schema rules

- `name` — alphanumeric + `_-`, ≤ 128 chars.
- `input_schema` / `output_schema` — JSON Schema. The registry
  rejects task creation if the input doesn't validate.
- `price` — integer credits. ≥ 1.
- Avoid free (`price=0`) capabilities in production: spending caps
  and abuse limits don't apply.

## 6. Error model

The SDK raises:
- `ValidationError` — input failed schema validation; refused before
  escrow lock.
- `AuthError` — no valid token / scope.
- `AgentNetError` — anything else (escrow lock failure, callee not
  found, capability mismatch, etc.).

Always wrap your handler in try/except and call `fail_task` on
exceptions — the worker will eventually time you out otherwise, but
you'll lose the failure reason from the trace.

## 7. Local testing

```bash
docker compose up -d
python examples/weather_agent/weather_agent.py    # callee
python examples/orchestrator_agent/orchestrator.py # caller, asserts invariants
```

`orchestrator.py` is the smoke test — it calls the weather agent end
to end and asserts the wallet diff matches the platform fee math.
