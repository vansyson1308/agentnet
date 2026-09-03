# Society Runtime — staging and live-model runbook (Phase 2)

This runbook takes the Autonomous Society Runtime from the deterministic proof
(`examples/demo_autonomous_society.py`) to a **staging** system driven by a real model.
Production stays OFF throughout: no production worker, no production credential, no
production autonomous code, no production deploy intent (hard `False` in code).

Companion documents: `docs/SOCIETY_RUNTIME.md` (architecture), ADR-0002 (decisions),
`docs/SOCIETY_LIVE_PROOF.md` (what was actually proven, and what was blocked).

---

## 0. Hard rules

| Rule | How it is enforced |
| --- | --- |
| Never use a credential that appeared in git history | `python -m app.society.canary preflight` refuses fingerprints in `COMPROMISED_CREDENTIAL_FINGERPRINTS` and anything key-shaped in the checkout's `git log -p` |
| Never print / commit / trace a credential | scripts read `SOCIETY_MODEL_API_KEY` (or `LLM_API_KEY`) from the environment only; reports carry an 8-hex fingerprint prefix; `/v1/society/config` redacts it; provider errors keep a 200-char body excerpt and no headers |
| NO FAKE AUTONOMY | `canary run/observe` refuse `scripted`/`fake`; a report FAILs if any completed run carries another provider; nobody writes Builder output or QA/Security verdicts by hand |
| Operator authority is server-side | `users.society_role` + one dependency (`operator_auth`); user JWTs only; scoped `spt_` and agent tokens are 403 |
| Runtime OFF by default | `SOCIETY_RUNTIME_ENABLED` / `SOCIETY_AUTONOMOUS_CODE_ENABLED` default `false` in every compose file; the worker idles and touches nothing |
| Production untouched | `docker-compose.prod.yml` has no society service and no `SOCIETY_*` variable (`tests/test_society_staging_compose.py`) |

---

## 1. Staging deployment (on the staging host)

```bash
cd /opt/agentnet && git fetch && git checkout main && git pull
# .env additions (values from the secret store — never from git):
#   SOCIETY_OPERATOR_BOOTSTRAP_EMAILS=<first operator email>
#   SOCIETY_RUNTIME_ENABLED=false            # flip to true only for the canary window
#   SOCIETY_MODEL_PROVIDER=scripted          # openai_compatible only after preflight is READY
#   SOCIETY_MODEL_BASE_URL= / SOCIETY_MODEL_NAME= / SOCIETY_MODEL_API_KEY=   (rotated, never leaked)
bash deploy/runbook-staging.sh
```

`deploy/runbook-staging.sh` now also:

1. asserts `alembic current` inside `agentnet-staging-registry` is `0008_society_phase2`;
2. runs `deploy/society-migration-check.sh --mode docker` — fresh bundle path, upgrade path
   (0004→0008), idempotent second upgrade, downgrade/upgrade round-trip, on scratch databases;
3. waits for `agentnet-staging-society-worker` to be healthy (internal `:9101/metrics`);
4. runs `deploy/society-staging-smoke.py` (`--inject` when `SOCIETY_SMOKE_TOKEN` is set).

The society worker gets the repository bind-mounted at `/workspace/repo` and a named volume for
worktrees; it never publishes a port and never sees a docker socket.

### 1.1 First operator

```bash
# bootstrap (email must be in SOCIETY_OPERATOR_BOOTSTRAP_EMAILS on registry-staging), then make it durable:
docker exec agentnet-staging-registry python -m app.society.operator <email> operator
# afterwards the allowlist can be emptied; assign more roles via POST /v1/society/operators
```

Get a user JWT for that operator through the normal login (`POST /v1/auth/user/login`) and keep it in
the environment as `SOCIETY_SMOKE_TOKEN` / `SOCIETY_REDTEAM_TOKEN` / `SOCIETY_CANARY_TOKEN`. Tokens are
never printed by any script.

### 1.2 Seed and verify

```bash
docker exec agentnet-staging-registry python -m app.society.seed          # idempotent; keeps operator gates
SOCIETY_SMOKE_TOKEN=... python3 deploy/society-staging-smoke.py --api http://localhost:8100 \
    --expect-runtime off --inject --metrics-probe 127.0.0.1:9101
```

With the runtime off the injected `staging.canary.signal` must persist as `pending` and appear on
the public story without its payload (checks C11a–C11c).

---

## 2. Live-model preflight

```bash
docker exec agentnet-staging-society-worker python -m app.society.canary preflight
```

Verdicts:

| Verdict | Meaning | Next step |
| --- | --- | --- |
| `LIVE MODEL READY` | provider is `openai_compatible`, credential safe, one bounded JSON probe succeeded | proceed to §3 |
| `LIVE MODEL BLOCKED — NO SAFE CREDENTIAL` | no credential, or fingerprint compromised (git history / denylist) | obtain a rotated credential; do **not** test the leaked one |
| `LIVE MODEL BLOCKED — PROVIDER UNREACHABLE` | base URL missing, transport failure, non-JSON reply, provider error (status kept, never the key) | fix `SOCIETY_MODEL_BASE_URL`/network; check `probe.error` |
| `LIVE MODEL BLOCKED — PROVIDER IS NOT LIVE (NO FAKE AUTONOMY)` | provider is `scripted`/`fake` | this is the correct state for a deployment that has not been given a live model |

The report also lists the limits the canary will run under (daily USD budget, request retries,
timeout, runs/hour, runs/correlation, causation depth). Tighten them for the first window:
`SOCIETY_DAILY_MODEL_BUDGET=1.0 SOCIETY_MAX_RUNS_PER_HOUR=30 SOCIETY_MAX_RUNS_PER_CORRELATION=12`.

---

## 3. Canaries (real model, staging worker owns the credential)

Enable the runtime for the window (`SOCIETY_RUNTIME_ENABLED=true`, `SOCIETY_MODEL_PROVIDER=openai_compatible`,
restart `society-worker-staging`), then drive each canary **over HTTP** from the host — the canary
process never holds the model credential:

```bash
export SOCIETY_CANARY_TOKEN=...   # operator user JWT
python3 -c 'import sys; sys.path.insert(0,"services/registry")' 2>/dev/null
docker exec -e SOCIETY_CANARY_TOKEN agentnet-staging-registry \
    python -m app.society.canary observe --api http://localhost:8000 --scenario single  --report /tmp/canary-1.json
docker exec -e SOCIETY_CANARY_TOKEN agentnet-staging-registry \
    python -m app.society.canary observe --api http://localhost:8000 --scenario multi   --report /tmp/canary-2.json
# approval interruption: gate one LOW intent for the Scout, then decide from the canary
docker exec agentnet-staging-registry python -m app.society.canary gate --role scout --intent CREATE_IMPROVEMENT
docker exec -e SOCIETY_CANARY_TOKEN agentnet-staging-registry \
    python -m app.society.canary observe --api http://localhost:8000 --scenario approval --decide approve --report /tmp/canary-3a.json
docker exec -e SOCIETY_CANARY_TOKEN agentnet-staging-registry \
    python -m app.society.canary observe --api http://localhost:8000 --scenario approval --decide reject  --report /tmp/canary-3b.json
docker exec agentnet-staging-registry python -m app.society.canary gate --role scout --clear
```

| Canary | Injected event | PASS criteria (evaluated from durable state, never self-reported) |
| --- | --- | --- |
| 1 single agent | `staging.canary.signal` (Scout) | ≥1 completed run, `model_provider=openai_compatible`, model name as configured, no DEAD run |
| 2 multi-agent | `platform.metric.anomaly` | ≥2 roles completed runs, ≥1 causation-linked follow-up event |
| 3 approval | `platform.metric.anomaly` + gate | intent parked `awaiting_approval`; after `approve` → `executed` (resumed, model not re-called); after `reject` → `rejected`, never executed; `intent_approvals` row present |

Reports contain run ids, roles, statuses, provider/model, tokens, cost, request/retry/timeout
counts, intent types with policy/execution decisions, event types with depth, approvals and candidate
statuses — **no payloads, prompts, context or decision text**.

`canary run` (in-process worker, DB access, credential in this process) exists for local proofs and
is refused for anything but `openai_compatible` too.

---

## 4. Soak and GO / NO-GO

Soak window: leave the runtime on with the tightened limits and inject world events only through the
ingress (no manual intermediate events, no hand-written QA/Security marks, no grant inflation, loop
breakers untouched). Read `GET /v1/society/budget`, `/metrics`, `/approvals` and the worker log.

GO requires all of: ≥3 correlations, ≥15 real-model runs, ≥3 roles, ≥1 approval, ≥1 rejection,
≥1 docs-only candidate that reached QA **and** Security with both reports persisted (never merged),
zero DEAD runs caused by the runtime itself, no credential in any log/trace/context, no public-surface
leak (`society-staging-redteam.py` ALL DEFENDED), production flags untouched.

NO-GO (stop the runtime, keep the evidence): any forbidden HIGH intent allowed, any wallet/escrow
inconsistency, any loop breaker tripping repeatedly, a credential appearing anywhere, or the daily
budget exhausted by fewer than the expected runs.

Failure policy: `SOCIETY_RUNTIME_ENABLED=false` (events wait, nothing is lost) → collect
`/v1/society/story/<corr>/detail` for the affected correlation → fix on a branch → re-run the
canaries. Never edit rows by hand to make a canary pass.

---

## 5. Continuous red-team

```bash
SOCIETY_REDTEAM_TOKEN=... SOCIETY_REDTEAM_USER_TOKEN=... SOCIETY_REDTEAM_AGENT_TOKEN=... \
python3 deploy/society-staging-redteam.py --api http://localhost:8100 --burst 40 --report redteam.json
```

Attacks A01–A12 (reserved event families, allowlist, `target_agent_id`, oversize and malformed
payloads, prompt injection through an allow-listed event, operator surfaces with anonymous/user/agent
tokens, approvals on unknown intents, role escalation, idempotent replay, per-actor rate limit,
event_type pattern). Exit code 0 means all defended; `--burst` consumes the actor's hourly quota.

---

## 6. Script environment variables (inputs to scripts, never service settings)

| Variable | Used by | Value |
| --- | --- | --- |
| `SOCIETY_SMOKE_TOKEN` | `deploy/society-staging-smoke.py` | operator user JWT (optional; enables operator checks and `--inject`) |
| `SOCIETY_REDTEAM_TOKEN` | `deploy/society-staging-redteam.py` | operator or event_producer user JWT (required) |
| `SOCIETY_REDTEAM_USER_TOKEN` / `SOCIETY_REDTEAM_AGENT_TOKEN` | red-team | plain user JWT / agent or `spt_` token (optional, adds 403 checks) |
| `SOCIETY_CANARY_TOKEN` | `python -m app.society.canary observe` | operator user JWT (required) |
| `SOCIETY_CANARY_API_URL` | canary `observe` | default `--api` |

Tokens are read, never echoed. Reports are JSON files you choose the path for.

---

## 7. Local dry run of this runbook (no live model)

The whole topology was exercised locally against Postgres with `SOCIETY_MODEL_PROVIDER=scripted`
(registry via uvicorn on :8100, `python -m app.society.worker`, fleet seeded, operator/producer/user/
agent tokens): migration check PASS (fresh + upgrade + round-trip), smoke PASS (C01–C12), red-team
ALL DEFENDED (A01–A12, per-actor limit tripped after 29 events), `canary observe` correctly
**REFUSED** with `PROVIDER IS NOT LIVE (NO FAKE AUTONOMY)`, `canary preflight` reported
`NO SAFE CREDENTIAL` when no credential was present. See `docs/SOCIETY_LIVE_PROOF.md`.
