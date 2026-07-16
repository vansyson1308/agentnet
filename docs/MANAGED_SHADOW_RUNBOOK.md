# Managed shadow first-batch runbook

This batch implements the Gate 2A/2B foundation and a Gate 3 Codex runtime.
It is disabled by default and cannot create a financial transaction.

## Safety boundary

- `MANAGED_EXECUTION_ENABLED=false` is the default.
- The public create contract accepts only `economy_mode=managed_shadow`.
- The database requires managed-shadow `task_sessions.escrow_amount = 0`.
- The managed service never imports or creates the `Transaction` model.
- Legacy Task start/confirm/fail endpoints reject managed tasks.
- The legacy auto-scaler and YAML backlog export are disabled by default.

Paperclip's deployed version, callback schema, and authentication mechanism
must still be verified at Gate 0. The adapter endpoint in this batch uses a
dedicated scoped bearer token and must not be exposed as a value-settlement
approval channel.

## Start the shadow profile

Set unique secrets for:

- `MANAGED_EXECUTION_SERVICE_TOKEN`
- `RUNTIME_REGISTRATION_TOKEN`
- `PAPERCLIP_ADAPTER_TOKEN`

Then combine the production compose definition with the additive override:

```sh
docker compose \
  -f docker-compose.prod.yml \
  -f docker-compose.managed-shadow.yml \
  --profile managed-shadow up -d registry paperclip-adapter
```

## Register a runtime

Call `POST /v1/runtimes/register` with the runtime-registration bearer token.
Use a narrow repository allowlist and declare the exact capability,
permissions, model, and provider. Store the returned `rt_...` token once; only
its SHA-256 hash is retained by AgentNet.

Configure `CODEX_RUNTIME_ID`, `CODEX_RUNTIME_TOKEN`, and
`CODEX_REPOSITORY_ALLOWLIST`. Pin `CODEX_CLI_VERSION` to an explicitly tested
`@openai/codex` release and provide `CODEX_API_KEY` for non-interactive
`codex exec`; the runtime removes that key from its long-lived environment and
supplies it only to the Codex child process. Then start `codex-runtime`. It claims
work through:

```text
POST /v1/runtimes/{runtime_id}/assignments/claim?wait_seconds=30
```

The claim transaction changes Lease `offered -> acknowledged` and returns a
one-time opaque lease token. Heartbeats renew it up to the run deadline.

## Delivery and recovery

Delivery is at-least-once. Exactly-once effect is provided by logical-work,
idempotency, event-sequence, active-Lease, and active-slot constraints.
The registry reconciler expires stale Leases every 15 seconds and blocks the
abandoned execution, preserving its evidence for later repair/reconciliation.

Artifacts are finalized under `sha256/<prefix>/<hash>`. PostgreSQL stores only
metadata, manifests, checksums, URI, changed files, provenance, and usage.

## Not enabled by this batch

- Real escrow or financial transactions
- Independent QA and bounded repair
- Signed candidate-bound approval receipts
- Release/refund settlement
- Production Paperclip contract certification

Those remain Gate 4 through Gate 6 prerequisites for `managed_value`.
