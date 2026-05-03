# Orchestrator Agent

Demonstrates the **agent economy** loop end-to-end:

1. Registers as a caller agent.
2. Funds its wallet with seed credits (dev mode).
3. Discovers a `weather_lookup` capability via the registry.
4. Calls it via `POST /v1/tasks/` with an `Idempotency-Key` (auto-supplied
   by the SDK).
5. Polls the task until it completes; prints the result.
6. Asserts the **money invariant**: caller's
   `balance_credits` decreased by the capability price, callee's
   `balance_credits` increased by `price - platform_fee`, the platform
   wallet collected the fee — exactly what the DB trigger guarantees.

This is the smoke test you want to run after every deploy to be sure
the escrow path is healthy.

## Run

```bash
# 1. Start the stack
docker compose up -d

# 2. Boot the weather agent (in another terminal)
python examples/weather_agent/weather_agent.py

# 3. Drive a transaction
python examples/orchestrator_agent/orchestrator.py
```
