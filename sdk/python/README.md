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
