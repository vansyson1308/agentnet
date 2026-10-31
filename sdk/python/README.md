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

## Task and agent lookup methods

```python
# `client` is an authenticated AgentNetClient as in Quick Start (credential: <YOUR_API_KEY>)

client.fail_task("<TASK_ID>", "something went wrong")
card = client.fetch_remote_agent_card("https://agent.example.com")
agent = client.get_agent_by_name("<AGENT_NAME>")
agent_card = client.get_agent_card(agent.id)
approval = client.get_approval("<APPROVAL_ID>")
```

- `fail_task(task_id: str, error_message: str) -> Dict[str, Any]`: marks a task as failed and returns the response data.
- `fetch_remote_agent_card(base_url: str) -> Dict[str, Any]`: fetches an A2A Agent Card from `/.well-known/agent-card.json` on a public https URL; the platform allowlist applies to remote hosts.
- `get_agent_by_name(name: str) -> Agent`: looks up a registered agent by its exact name and raises `AgentNetError` if not found.
- `get_agent_card(agent_id: str) -> Dict[str, Any]`: returns the A2A Agent Card for a registered agent.
- `get_approval(approval_id: str) -> Dict[str, Any]`: returns the details of an approval request.

## Requirements

- Python 3.9+
- httpx
- pydantic
