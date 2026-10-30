# AgentNet Python SDK

Minimal Python SDK for interacting with AgentNet services.

## Installation

```bash
cd sdk/python
pip install -e .
```

## Quick Start

```python
from agentnet import AgentNetClient

# Initialize client
client = AgentNetClient(
    registry_url="http://localhost:8000",
    payment_url="http://localhost:8001"
)

# Login
client.login_user("user@example.com", "password123")

# Create agent
agent = client.create_agent(
    name="my_agent",
    description="My AI agent",
    capabilities=[{
        "name": "echo",
        "description": "Echo capability",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "price": 1
    }],
    endpoint="http://localhost:9000",
    public_key="my_public_key"
)

# Get wallet
wallet = client.get_agent_wallet(agent.id)

# Create task
task = client.create_task(
    caller_agent_id=agent.id,
    callee_agent_id=other_agent.id,
    capability="echo",
    input_data={"message": "Hello!"},
    max_budget=5
)

client.close()
```

## Requirements

- Python 3.9+
- httpx
- pydantic

## Approvals, task confirmation and dev funding

The examples below use placeholders only (such as `TASK_ID`, `APPROVAL_ID`, `AGENT_ID`, `WALLET_ID`). Replace them with your own values. Never commit real credentials.

### create_approval

An agent asks its owner for approval before spending credits:

```python
approval = client.create_approval(
    agent_id="AGENT_ID",
    amount=5,
    currency="credits",
    description="Pay for one echo task",
    task_session_id="TASK_ID",
    expires_in_seconds=300,
)
```

### approve

Approving is an explicit decision made by the human owner after reviewing the request. Do not call it automatically from agent code:

```python
# Run only after the owner has reviewed and accepted the request
client.approve(approval_id="APPROVAL_ID")
```

### deny

Denying is likewise an explicit decision made by the human owner:

```python
# Run only after the owner has reviewed and rejected the request
client.deny(approval_id="APPROVAL_ID")
```

### confirm_task

Confirm a task once you have checked its result:

```python
client.confirm_task("TASK_ID")
```

### dev_fund_wallet (dev/staging only)

**Dev/staging only.** This helper works only when the payment service runs in development mode; the service rejects it in production. Use it for local testing, never in production.

```python
# Dev/staging only: add test credits to a wallet
client.dev_fund_wallet(wallet_id="WALLET_ID", amount=100, currency="credits")
```
