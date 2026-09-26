# A2A 1.0 — the live proof, and what is not yet proven

The durable record of AgentNet's A2A 1.0 gateway and federation running on a
real environment. It is written from live platform output (the `CHECK` lines
the proof printed), not from intent. Where something has not happened, it says
so rather than rounding up.

**Summary (2026-09-26):**

| Environment | Inbound A2A | Outbound federation | Evidence |
| --- | --- | --- | --- |
| staging | **LIVE — 39/39** | **LIVE — 18/18** (with the incident drill) | §2, §3 |
| production | **NOT RELEASED** | **NOT RELEASED** | §5 |

The code is `main` `39e7c6b` (PR #47). Production still runs `9583033`
(`main` `57dab99`), which contains none of Phase 8.

## 1. How the proof runs

`deploy/railway/a2a_live_proof.py` runs **inside** the staging environment as
the `staging-validator` service (`VALIDATOR_SCRIPT=a2a_live_proof.py`). It
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

## 5. Production — not released (blocked)

Production is unchanged. It runs `9583033` with no Phase 8 code, no migration
`0013`, and no A2A flag.

What was done toward the release:

* **Release gate preflight (read-only), target `39e7c6b`:**
  * target shape, existence, on `main`, and main CI green: PASS;
  * no merge freeze: PASS;
  * staging evidence PASS for:
    * registry, worker and dashboard (staging deployed the target itself);
    * payment (subtree unchanged since `ac1b57e`).
* **Sensitive diff reviewed file by file** against the owner's Phase-8
  authorization. Every hit is inside it:

  | Category | Files | Why in scope |
  | --- | --- | --- |
  | `migrations` | `0013_a2a_federation` | new tables only; no drops or column changes |
  | `db_bootstrap` | `entrypoint.sh` | comment only |
  | `auth` | `app/a2a/auth.py` | A2A over the existing `verify_token` |
  | `payment_economics` | `task_service.py` | `cancel_task_with_refund`: `INITIATED` only, refunded once, through the shared refund code under a row lock |
  | `society_policy` | `config.py`, `intents.py`, `policy.py`, `promotion.py` | A2A intents, company settings, and an incident merge freeze (stricter only) |
  | `release_machinery` | `.railway/production.ts`, `.railway/railway.ts` | production declares A2A dark; staging enables it |
  | `deployment_foundation` | `services/dashboard/Dockerfile` | gunicorn, the separately reviewed #46 |

  No unexpected sensitive diff.

What stopped it: creating the release branch (`release.py --execute`) was
refused by this session's permission boundary. So was the production canary's
identity step: reading an account's verification token from the production
database, as `validate_email_flow.py` does, to make a verified test identity.
Both are decisions for the owner. Nothing was attempted around them.

Until the owner decides, production A2A is **not live**, and none of the
production lines of the Phase 8 verdict are claimed.
