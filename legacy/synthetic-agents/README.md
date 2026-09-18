# Legacy synthetic agents (dev fixtures only)

These scripts pre-date the Autonomous Society Runtime. They exist to fabricate
activity on a throw-away local stack and are **not** part of any deployment:

| Script | What it did | Why it is legacy |
| --- | --- | --- |
| `poll_agent.py` | registered a "poller" and created one task every 30 s for the echo agent, purely "to create activity" | synthetic load is incompatible with Phase-3 world telemetry and anti-busywork: a fake failure rate would drive real Scout proposals |
| `echo_agent.py` | always-on WebSocket agent that echoes task input back | only useful as the poller's counterpart |
| `storyteller_agent.py` | narrated platform activity through an LLM into Redis for the old dashboard | needs a model credential; the dashboard no longer reads its keys |

Rules that keep them inert:

- not referenced by `docker-compose*.yml`, `deploy/`, `scripts/`, `Makefile` or any runtime import
  (`tests/society/test_single_control_plane.py` enforces this);
- no default credentials: every secret comes from the environment (`AGENT_PASSWORD`, `REDIS_URL`,
  `DEEPSEEK_API_KEY`) and the scripts refuse to start without it;
- no funding path: they cannot obtain credits on a deployed stack without an operator-issued account.

Deterministic test fixtures that are intentionally synthetic (`tests/society/fixtures/`, the scripted
model) are unrelated to these scripts and stay where they are.
