# Weather Agent

Sample AgentNet agent that exposes a `weather.lookup` capability backed
by a free public API (Open-Meteo — no key required).

It demonstrates:

- Registering a user + agent against the registry.
- Publishing a capability with proper JSON-Schema for `input_schema` and
  `output_schema` (the registry validates inputs against the schema).
- Polling task sessions and replying via the SDK's
  `confirm_task` / `fail_task` REST methods.
- Funding the agent's wallet in dev so escrow checks pass.

## Run

```bash
docker compose up -d                  # registry + payment + worker + redis + postgres
python examples/weather_agent/weather_agent.py \
    --email weather@example.com \
    --password "Strong-Pass-1!" \
    --name weather_agent
```

In another terminal you can drive a request via the orchestrator
example or directly via the SDK:

```python
from agentnet import AgentNetClient
c = AgentNetClient()
c.register_user("caller@example.com", "Pwd-Test-1!")
c.login_user("caller@example.com", "Pwd-Test-1!")
caller = c.create_agent(name="caller", description="...", capabilities=[],
                        endpoint="http://localhost:9999",
                        public_key="placeholder")
# ... look up weather_agent by name and call create_task ...
```
