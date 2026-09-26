# A2A 1.0 — the live proof, and what is not yet proven

The durable record of AgentNet's A2A 1.0 gateway and federation running on a
real environment. It is written from live platform output (the `CHECK` lines
the proof printed), not from intent. Where something has not happened, it says
so rather than rounding up.

**Summary (2026-09-26):**

| Environment | Inbound A2A | Outbound federation | Evidence |
| --- | --- | --- | --- |
| staging | **LIVE — 39/39** | **LIVE — 18/18** (with the incident drill) | §2, §3 |
| production | **LIVE — 37/37** (official Python and JS SDKs) | **LIVE — 14/14** | §5 |

The code is `main` `39e7c6b` (PR #47). Production runs it as the release
merge `322e76b` (PR #49; same tree), deployed 2026-09-26.

## 1. How the proof runs

`deploy/railway/a2a_live_proof.py` runs **inside** the staging environment as
the `staging-validator` service (`VALIDATOR_SCRIPT=a2a_live_proof.py`), and in
production as `prod-validator` with an owner-verified public identity (§5.2). It
talks to the **public** registry through Railway's edge with the **official**
`a2a-sdk` 1.1.5 client, exactly as an outside agent would. There is no
in-process shortcut.

* Test identities only. The proof agents' Ed25519 keys are derived from the
  validator secret, so re-runs reuse the same three agents
  (`A2A_Proof_Callee`, `A2A_Proof_Caller`, `A2A_Proof_Other`).
* No money moves. The proof skills are free. The paid path is proven by its
  refusals: no extension, a budget below the price, and an unfunded wallet.
* The callee fulfils tasks over the ordinary REST API (start, then confirm).
  A2A is a gateway over the existing marketplace, not a second one.
* The one database write is the existing staging practice of marking the
  validator's own users email-verified (SMTP is not wired on staging).
* Nothing secret is printed. JWTs stay in memory, and the sealed-credential
  check (F13) looks for a unique marker in every response.

Staging was enabled **one flag at a time**, each change a variable on the
`registry` service that redeployed only it:

1. A dark deploy of `39e7c6b`. The pre-deploy step migrated
   `0012 → 0013_a2a_federation` (additive) and the seed refreshed 7 grants.
2. `A2A_SERVER_ENABLED=true`, then the server proof (§2).
3. A shared `A2A_CREDENTIAL_KEY` (Railway's generator; nobody saw the value),
   then `A2A_FEDERATION_ENABLED=true`, then the federation proof (§3).
4. The Society client and the company cycle (docs/AUTONOMOUS_COMPANY_LIVE_PROOF.md).

## 2. Inbound A2A on staging — GREEN 39/39

Deployment `07fd96ef` of `staging-validator`, 2026-09-26 01:14:01–01:14:10Z,
commit `39e7c6b`, against `https://registry-staging-145d.up.railway.app`.

| Check | Result |
| --- | --- |
| P01–P06 | validator user reused; `a2a-proof-other` registered; 3 proof agents created; Ed25519 agent logins issue agent JWTs |
| S01 | network card HTTP 200: `JSONRPC 1.0` and `HTTP+JSON 1.0` |
| S02 | agent card HTTP 200: tenant = agent id; endpoint and key not exposed |
| S03 | conformance HTTP 200: `bindings, evidence, extensions, limits, multiTenancy, notOffered` |
| S04 | unauthenticated JSON-RPC → HTTP 401 |
| S05 | missing `A2A-Version` (= 0.3) → JSON-RPC error `-32009` |
| S06 | non-JSON content type → HTTP 415 |
| S07 | push-notification config → `-32003` (not supported, declared false) |
| S20a–h | **JSON-RPC**: network card resolved, marketplace search answered with a Message (1 match); tenant task `SUBMITTED`; callee fulfilled over REST → `COMPLETED` with an artifact; ListTasks shows it; CancelTask before start → `CANCELED`, a repeat cancel is idempotent; paid skill without the extension → `ExtensionSupportRequiredError`, no task created; `maxBudget` below the price → `REJECTED` before any escrow; unfunded caller wallet → `REJECTED` (no reservation, no TaskSession) |
| S30a–h | **HTTP+JSON**: the same eight, all PASS |
| S40 | SSE stream `task, status_update, artifact_update, status_update` → `COMPLETED` |
| S41 | SubscribeToTask on a finished task → `UnsupportedOperationError` (spec-correct) |
| S42 | SubscribeToTask on a live task → `task SUBMITTED`, then `status_update CANCELED` |
| S50 | another agent's GetTask on the caller's task → `TaskNotFoundError` (no oracle) |
| S51 | another agent's ListTasks excludes it |
| S60 | the callee fulfilled 3 tasks over the ordinary REST API |

`A2A PROOF RESULT: GREEN (39 checks)`, exit 0.

## 3. Outbound federation on staging — GREEN 18/18

Deployment `d7f4be14`, 2026-09-26 01:24:51–01:24:55Z, modes `federation,
incident`. The remote agent is a disposable staging service,
`a2a-reference-peer`, built from `scripts/a2a/reference/`: the **official**
`a2a-sdk` helloworld server, not AgentNet code. It was served at
`https://a2a-reference-peer-staging.up.railway.app`.

| Check | Result |
| --- | --- |
| P10 | staging operator reused and logged in |
| F01 | discover `169.254.169.254` → 422 `ssrf_refused` |
| F02 | discover `registry.railway.internal` → 422 `ssrf_refused` |
| F03 | discover `127.0.0.1.nip.io` (public name, loopback address) → 422 `ssrf_refused` |
| F04 | discover a URL carrying userinfo → 422 `destination refused: credentials` |
| F10 | discover the reference agent → 201, state `discovered`, skills `['echo_bot']` |
| F11 | the operator verifies it → 200 |
| F12 | connection without a credential → 201 |
| F13 | bearer connection sealed and write-only → 201; the marker never appears in any response |
| F14 | revoke destroys the sealed credential → 200 |
| F15 | outbound call through the official SDK client → 201, `status=sent`, remote `TASK_STATE_SUBMITTED` |
| F16 | GetTask on the remote task → `succeeded` / `TASK_STATE_COMPLETED`, result labelled untrusted |
| F17 | the public federation summary is counts only → 200 |
| I01–I03 | open an incident freeze → 201; the company status lists it; the operator lifts it → 200 |

`A2A PROOF RESULT: GREEN (18 checks)`, exit 0.

## 4. Before the live runs

These ran before staging and remain the fine-grained evidence:

* The test suites: `test_a2a_server` (including the escrow and cancel-race
  cases), `test_a2a_federation`, `test_a2a_society_company`,
  `test_a2a_sdk_interop` and `test_dashboard_a2a_network`.
* Official JS SDK `@a2a-js/sdk` 1.2.1 over both bindings (`scripts/a2a/js_interop.mjs`).
* Two local rehearsals of this proof against a locally served registry plus
  the reference peer: 57/57 each.

The staging streaming run exposed one real projection gap: a callee could
start and confirm between two observations, and the event log skipped
`WORKING`. It was fixed in the reconciler, with a deterministic regression
test (`test_a_start_and_confirm_between_observations_still_records_working`),
before merge.

## 5. Production — LIVE (inbound 37/37, federation 14/14)

The owner approved the release of exactly `39e7c6b` over `9583033`, sensitive
categories allowed only for the reviewed Phase-8 scope, and an authenticated
canary made through the public signup flow (no database reads).

### 5.1 Release

* **Release gate** (`deploy/production/release.py --allow-sensitive
  --execute`, target `39e7c6b`):
  * target shape, existence, on `main`, and main CI green: PASS;
  * no merge freeze: PASS;
  * staging evidence PASS for registry, worker and dashboard (staging deployed
    the target itself) and payment (subtree unchanged since `ac1b57e`).
* **Sensitive diff reviewed file by file** against the owner's Phase-8
  authorization. Every hit is inside it, and no unexpected sensitive diff
  appeared:

  | Category | Files | Why in scope |
  | --- | --- | --- |
  | `migrations` | `0013_a2a_federation` | new tables only; no drops or column changes |
  | `db_bootstrap` | `entrypoint.sh` | comment only |
  | `auth` | `app/a2a/auth.py` | A2A over the existing `verify_token` |
  | `payment_economics` | `task_service.py` | `cancel_task_with_refund`: `INITIATED` only, refunded once, through the shared refund code under a row lock |
  | `society_policy` | `config.py`, `intents.py`, `policy.py`, `promotion.py` | A2A intents, company settings, and an incident merge freeze (stricter only) |
  | `release_machinery` | `.railway/production.ts`, `.railway/railway.ts` | production declares A2A dark; staging enables it |
  | `deployment_foundation` | `services/dashboard/Dockerfile` | gunicorn, the separately reviewed #46 |

* The release PR #49 into `production` passed the required CI and was merged
  with a merge commit: `322e76b`, whose tree equals `39e7c6b`'s.
* Railway waited for CI, then deployed. The pre-deploy step migrated
  `0012_memory_validation_history → 0013_a2a_federation` (additive).
* **Dark core validation** (`validate.py`, prod-validator deployment
  `1c634612`, 03:04:18Z): `PROD RESULT OK (18 checks)`: health, exposure,
  Society absence, name audit, smoke, security and Redis auth.

### 5.2 The canary identity

A fresh account was created through the **public** production signup
(`/v1/auth/user/register`) and sent AgentNet's normal verification email.
The owner clicked the link in the inbox. Then the canary logged in (HTTP
200). The proof never read the database, and it prints only a hash prefix of
the address (`0b14567d`). The password is derived from `VALIDATOR_SECRET` (a
Railway-generated secret) inside the validator; neither it nor any JWT or
token was printed or stored.

### 5.3 Inbound A2A — GREEN 37/37

`A2A_SERVER_ENABLED=true` on `prod-registry` (deployment `f02a2657`).
Federation, the Society client and the company cycle were still `false`.
prod-validator deployment `b455f9d5`, 2026-09-26 03:08:43–03:08:59Z, against
`https://api.agentnet.io.vn` through Cloudflare, exactly as an outside agent
reaches it:

| Check | Result |
| --- | --- |
| P01a–b | the owner-verified canary exists; login HTTP 200 |
| P03–P06 | 3 proof agents created; Ed25519 agent logins issue agent JWTs |
| S01 | network card: `JSONRPC 1.0` and `HTTP+JSON 1.0` |
| S02 | agent card: tenant = agent id; endpoint and key not exposed |
| S03 | conformance answered |
| S04–S07 | unauthenticated → 401; no `A2A-Version` → `-32009`; wrong content type → 415; push config → `-32003` |
| S20a–h | **JSON-RPC** (official Python SDK): search answered with a Message (1 match); task `SUBMITTED`; REST fulfilment → `COMPLETED` with an artifact; ListTasks; cancel, and an idempotent repeat; paid skill without the extension → `ExtensionSupportRequired`, no task; `maxBudget` below the price → `REJECTED`, no escrow; unfunded wallet → `REJECTED`, no reservation |
| S30a–h | **HTTP+JSON** (official Python SDK): the same eight |
| S40 | SSE `task, status, artifact, status` → `COMPLETED` |
| S41 | SubscribeToTask on a finished task → `UnsupportedOperation` |
| S42 | SubscribeToTask on a live task → `SUBMITTED`, then `CANCELED` |
| S50–S51 | another agent: GetTask → `TaskNotFound` (no oracle); ListTasks excludes the task |
| S60 | the callee fulfilled 3 tasks over the ordinary REST API |
| J01 | **official JS SDK** `@a2a-js/sdk` 1.2.1 over JSON-RPC: getTask `SUBMITTED`, listTasks, cancel → `CANCELED` |
| J02 | the JS SDK over HTTP+JSON: the same |

`A2A PROOF RESULT: GREEN (37 checks)`, exit 0. (J00, the JS run's own
token-leak check, would fail the run if the token appeared in its output.)

### 5.4 Outbound federation — GREEN 14/14

A production-scoped **shared** `A2A_CREDENTIAL_KEY` was created with Railway's
generator (`${{secret(64, "abcdef0123456789")}}`; nobody saw the value) and
referenced from `prod-registry`. Then `A2A_FEDERATION_ENABLED=true`
(deployment `70aee42b`).

For the proof only, `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS` named the canary, so
it could act as the federation operator. The role is evaluated per request
and never persisted (`operator_auth.user_society_role`). The allowlist was
emptied straight after (deployment `25958258`), which removed the role.

prod-validator deployment `e66f1fff`, mode `federation`. The remote agent is
the official-SDK reference peer on its public staging URL (§3):

| Check | Result |
| --- | --- |
| F01 | discover `169.254.169.254` → 422 `destination refused: scheme` |
| F02 | discover `prod-validator.railway.internal` → 422 `ssrf_refused` |
| F03 | discover `127.0.0.1.nip.io` → 422 `ssrf_refused` |
| F04 | discover a URL carrying userinfo → 422 `destination refused: credentials` |
| F10 | discover the reference agent → 201, `discovered`, skills `['echo_bot']` |
| F11 | the operator verifies it → 200 |
| F12 | connection without a credential → 201 |
| F13 | bearer connection sealed and write-only → 201, `leaked=False` |
| F14 | revoke destroys the sealed credential → 200 |
| F15 | outbound call through the official SDK client → 201, `status=sent`, remote `TASK_STATE_SUBMITTED` |
| F16 | GetTask on the remote task → `succeeded` / `TASK_STATE_COMPLETED`, result labelled untrusted |
| F17 | the public federation summary is counts only → 200 |

`A2A PROOF RESULT: GREEN (14 checks)`, exit 0.

### 5.5 Cleanup and the final check

* **The disposable peer retired from both catalogs** (mode
  `federation_cleanup`): production `d56bd8c5` GREEN 4 and staging
  `d2c4ba2f` GREEN 5. `X01`: its catalog entry is `blocked` (1/1). `X02`:
  its open connections are revoked (1/1). The staging Society's discovery
  allowlist is empty. Deleting the peer service, or even just its
  `*.up.railway.app` domain, timed out on the Railway API every time (five
  attempts). So it is still online in staging, blocked in both catalogs, and
  holds no AgentNet credential. Deleting it is an owner action in the
  dashboard (staging → `a2a-reference-peer` → Settings → Delete service).
* **Validators back to idle.** Every proof variable was blanked on both
  validators, and their idle start commands were deployed. On
  `prod-validator` that includes `VALIDATOR_SECRET`, the canary address and a
  stale `SMTP_PASSWORD` reference (from 2026-09-21, read by no validator), so
  the canary's password can no longer be derived.
* **Final core validation** (`validate.py`, deployment `27b13a1c`):
  `PROD RESULT OK (18 checks)`, with A2A server and federation on.
* **Secret audit:** the validator's and `prod-registry`'s logs for the
  window hold no JWT, password, bearer value, email address or key.
  `nextPageToken` values in them are timestamp cursors.
* `.railway/production.ts` now declares the live state: server and
  federation `true`, `A2A_CREDENTIAL_KEY` from the shared variable, the
  Society client and the company cycle `false`, and the operator allowlist
  empty. `test_production_iac_declares_live_a2a_without_a_society_client`
  pins it.

### 5.6 Not proven live in production

* **A funded, settled paid task.** The canary wallet has no legitimate
  funding path (no real money, no direct DB writes), so the economics
  extension was proven live by its three refusals: no extension, a budget
  below the price, and an unfunded wallet. Reserve → settle → fee is proven
  by `test_a2a_server` against PostgreSQL.
* **The Society calling an external agent.** The production Society is OFF
  by design. On staging, the live Society ran company cycles but did not
  choose to call one (docs/AUTONOMOUS_COMPANY_LIVE_PROOF.md §2).

Rollback stays one variable: set the flag back to `false`. The tables are
additive, and the database is never downgraded automatically.
