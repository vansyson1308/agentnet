# Production — the dark bring-up, and what was actually proven

The durable record of AgentNet's first production environment. Written from
live platform reads, not from intent. Where something is an owner action or an
unproven claim, it says so rather than rounding up.

**As of 2026-09-21T01:16Z — all six services SUCCESS.** Railway project `AgentNet`
(`4a40abc4-f650-406d-be04-3d3c27ffc7b1`), environment `production`
(`5e23ccb2-4ba1-417e-a1b4-5bf362d0d2c4`).

## 1. What production runs

Branch `production` at `2adda7094a9c1b7da60027e6f47f4141c7798cfd`, tree
`0bd98264a279cb2de47a534b9b4f2158d0ca3f10` — verified identical to the approved
`main` target and to the release branch the gate produced. See
docs/PRODUCTION_RELEASE.md for the one-time bootstrap and why every gate still
ran.

Production does **not** follow `main`. `main` is autonomously merged by the
Society; production deploys a branch the Society cannot write (ADR-0008 D8).

| Service | id | Source | Healthcheck | First deployment |
| --- | --- | --- | --- | --- |
| `prod-postgres` | `c35d9aa4` | `ghcr.io/railwayapp-templates/postgres-ssl:18` | — | `037a330a` SUCCESS |
| `prod-redis` | `e41f9636` | `redis:8.2` | — | `a1cd9245` SUCCESS |
| `prod-registry` | `055f188d` | `production` @ `/services/registry` | `/readyz` 300s | `5ef7d10a` SUCCESS |
| `prod-payment` | `0208f0ea` | `production` @ `/services/payment` | `/readyz` 300s | `80dd8d66` SUCCESS |
| `prod-worker` | `2dd99b33` | `production` @ `/services/worker` | `/metrics` 120s | `42f2afba` SUCCESS |
| `prod-dashboard` | `05bf5f71` | `production` @ `/services/dashboard` | `/healthz` 120s | `f2339d74` SUCCESS |

Two volumes, both production-only: `prod-postgres-volume` (`a7d05f88`) and
`prod-redis-volume` (`8429d02b`). There is no Society workspace volume,
because there is no Society.

The registry owns the schema through a pre-deploy command that runs in a
separate container and must exit non-zero to block the release (ADR-0008 D4).
Its live evidence: `registry: alembic already stamped — running upgrade`, then
`Application startup complete`, then `Uvicorn running on http://0.0.0.0:8000`.

## 2. Isolation from staging — structural, not asserted

Every production service id is disjoint from its staging counterpart
(`prod-registry` `055f188d` vs staging `registry` `ec110f06`, and so on for all
four). `describe-environment` on staging does not list any `prod-*` service,
and on production does not list any staging service. Each Railway environment
has its own private network, so the two cannot reach each other over internal
DNS (ADR-0008 D2).

Production shares **no secret** with staging. `JWT_SECRET_KEY`,
`FLASK_SECRET_KEY` and `INTERNAL_WORKER_TOKEN` were generated fresh in the
production environment with `${{secret(48)}}`; the datastore passwords with
`${{secret(32)}}`. Nothing was copied. A shared JWT secret would have made a
staging token valid in production (ADR-0008 D9).

**No secret value was ever retrieved, printed or compared.** That is structural
rather than a matter of discipline: the Railway API returns
`valuesRedacted: true` and never serves the values at all.

## 3. Network exposure — dark means dark

| Service | Public domains | TCP proxies |
| --- | --- | --- |
| all six | **0** | **0** |

Nothing in production is reachable from the internet. Services talk to each
other over `*.railway.internal` only. No DNS record was created or changed, and
`agentnet.io.vn` is untouched.

Consequence recorded honestly: `CORS_ALLOWED_ORIGINS` must be non-empty (the
code refuses empty, so that no host can accidentally ship allow-all), and no
legitimate public browser origin exists while production is dark. It is
therefore pinned to the dashboard's **private** origin — an origin no browser
can resolve, which grants nothing. **This must be set to the real origin before
any public exposure.**

## 4. Credential-name audit (names only, never values)

None of the forbidden names appears on any of the six services:

`SOCIETY_MODEL_API_KEY`, `LLM_API_KEY`, `SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM`,
`SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE`, `SOCIETY_GITHUB_TOKEN`,
`DEEPSEEK_API_KEY`.

Production holds no model credential and no GitHub App key. It cannot call a
model and it cannot push to GitHub, because the material to do either is not
present under any name.

## 5. Society absence

There is **no `society-worker` service in production**, and no workspace
volume for one. `prod-payment`, `prod-worker` and `prod-dashboard` carry no
`SOCIETY_*` variable at all. `prod-registry` carries the switches and every one
is off: runtime, autonomous code, auto-merge and staging-deploy all `false`;
promotion, GitHub-credential and deployment providers all `disabled`; model
provider `scripted`; `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS` empty, so no
privileged identity is bootstrapped here.

This agrees with the code rather than substituting for it:
`validate_settings` raises `SocietyConfigError` from `__post_init__` when
`ENVIRONMENT=production` and any of those flags is true, so a production host
configured otherwise fails at startup instead of running.
`settings.production_deploy_enabled` remains a hard `False`.

**Least privilege, checked not assumed:** `prod-worker` has neither
`INTERNAL_WORKER_TOKEN` nor a payment URL. It reaches PostgreSQL, Redis and the
registry, and nothing else. That secret stays on `prod-payment`, which is the
only service that needs it.

## 6. Owner actions still outstanding

Neither is settable from an engineering session; both are recorded rather than
claimed as done.

1. **Apply `deploy/github/production-ruleset.json`** to protect the
   `production` branch. Until then the branch has no ruleset. The command and
   the two deliberate differences from `main` are in `deploy/github/README.md`.
2. **Enable Wait for CI** on the four application services. Verified off:
   `checkSuites: false`. The `on: push` trigger for `production` that makes the
   gate meaningful is already in `.github/workflows/ci.yml` (CI run 167 ran on
   the branch), so this is a single dashboard toggle per service.

## 7. What is NOT proven here

- **No HTTP smoke test was run against production from this session.** The
  engineering environment's network policy denies outbound to
  `*.up.railway.app` (403 on CONNECT), and production has no public domain in
  any case. Health is evidenced by Railway healthchecks gating each release and
  by service logs, not by an external probe. `deploy/production/validate.py`
  exists for an operator or an in-environment runner to execute.
- **Registration through the delivery path was not exercised live on staging.**
  The staging validator's `ensure_user` hit an existing account (HTTP 400),
  which short-circuits before any email logic. That path is covered by unit
  tests, not by a live run.
- Healthchecks gate a release; they are **not** continuous monitoring
  (ADR-0008 D5).
- **No smoke test was run from the ENGINEERING session.** The engineering
  network policy denies outbound to `*.up.railway.app`, and production has no
  public domain in any case. Live validation was therefore run from inside the
  environment (§10), which is also the only place production is reachable from.

## 8. The finding this bring-up surfaced

Production Redis served with **no password** for a window during bring-up —
reachable by anything on the production private network, never from the
internet. Full account, including the two wrong diagnoses I published before
finding the real one, is ADR-0008 D11. It now fails closed: an empty
`REDIS_PASSWORD` refuses to boot rather than silently starting an open Redis,
and the runtime state is checkable from a log marker
(`redis: requirepass will be set (length 32)`, deployment `a1cd9245`) instead
of inferred from configuration.

## 9. PRODUCTION DARK SOAK START

**2026-09-21T01:16Z.** All six services reporting SUCCESS on their first
deployment from the `production` branch at `2adda709`. The environment is
deployed, private, and serving nobody by design.

The soak measures whether a production environment nobody is using stays
healthy: restart loops, memory growth, the worker's 30s poll cycle running
without error against an empty task table, and Postgres/Redis persistence
across the volumes. It does not measure load, because there is none and there
is no public surface to generate any.

Known-good deployment ids, recorded because Railway rollback restores an image
**and** its variables but only within the plan's image-retention window
(Hobby: 72h — ADR-0008 D6), after which rolling back to these specific
deployments is no longer possible:

```
prod-postgres   037a330a-1c46-4f28-ba96-dae295145a5a
prod-redis      a1cd9245-3073-4e44-8756-a3de35cd6460
prod-registry   5ef7d10a-4e29-4ac0-bc1c-43fa9b1a005d
prod-payment    80dd8d66-9c5e-49ac-9878-cf3a511cad97
prod-worker     42f2afba-8e41-4297-a00f-bdea273fe17a
prod-dashboard  f2339d74-0f07-4d28-84ab-d8396da021e4
```

Code rollback is not schema rollback (ADR-0008 D7): reverting the registry to
an earlier image does not undo a migration that already ran.

## 10. Live production validation (§42, §48) — two consecutive clean runs

Run from `prod-validator`, a temporary service **inside** the production
environment reaching `prod-registry` and `prod-dashboard` over private DNS
only. Staging was not involved in either direction; running the validator from
staging would have breached the isolation this phase exists to establish, and
was rejected rather than attempted. The service is `restartPolicyType: NEVER`,
holds no secret value (only variable NAMES, for the credential-absence check,
and Railway references for the datastore), and creates nothing: with delivery
disabled the canary registration is refused.

| Run | Deployment | Validated at | Result |
| --- | --- | --- | --- |
| 1 | `3d83b1d8` | 2026-09-21T01:24:49Z | `PROD RESULT: OK (10 checks)`, exit 0 |
| 2 | `454e21df` | 2026-09-21T01:33:07Z | `PROD RESULT: OK (10 checks)`, exit 0 |

Both against `release.sha = 2adda7094a9c1b7da60027e6f47f4141c7798cfd`, each with
a fresh canary identity (`prod-canary-8df745d7b5@example.com`,
`prod-canary-3ff93ac9dd@example.com`).

```
H01 registry  /healthz  -> 200        H02 registry /readyz -> 200
H03 dashboard /healthz  -> 200        H04 dashboard index  -> 200
N01 no public domain exists for payment/worker/postgres/redis
S01 no model/GitHub credential name present
S02 public society status inert in production
C01 registration with delivery disabled -> 503 (account NOT created)
C02 login for the refused registration  -> 401
C03 public human signup BLOCKED by configuration, fails closed
```

`C01`-`C03` are the substantive result. The registration change this phase
made -- flush the rows, attempt delivery, commit only if it succeeded -- is now
proven in production: it refuses rather than creating an account that could
never be activated, and `C02` confirms no account exists afterwards.

### The first run failed, and the failure was the validator's own

Run 0 (`bcbabecb`, 01:20:54Z) reported
`smoke C01 FAIL registration with delivery disabled -> 422 (expected 503)`.

That was not a production finding. 422 is Pydantic refusing the request body,
so the call never reached the delivery check the test exists to exercise. The
canary used `@agentnet.invalid`, and `email-validator` refuses `.invalid`,
`.test` and `.localhost` as special-use reserved names -- verified directly
against the registry's own `EmailStr` model rather than assumed. Fixed in
`50ce9e7` (canary moved to `example.com`, IANA-reserved and already this
repository's integration-test convention) with a regression test pinning both
directions, so a later tidy-up back to `.invalid` fails loudly instead of
silently reporting a production failure that is the validator's own.

Recorded because it is the more useful half of the lesson: **a validator that
fails for its own reasons and reports it as a production failure is worse than
no validator**, since the failure reads as evidence.

### Two deployments that were not validations

`6fef4fb1` reported SUCCESS with empty logs on both `deployment` and `build`
logType, and `60ff94e8` reported `SKIPPED`. The validator's watch patterns are
`/deploy/production/**` on the `production` branch and nothing under that path
changed, so neither executed the validator. They are **non-runs, not failures**,
and are excluded from the table above rather than counted either way. The
working trigger for this service is `redeploy`, because the validator does its
work at container start -- it re-clones `VALIDATOR_REF` each time -- so
replaying the snapshot genuinely re-runs the validation.

## 11. Security validation (§38) and the Redis auth contract (§39)

Added after the first GREEN verdict, because that verdict was premature: §55
gates GREEN on "security PASS" and §38's probes had never run. Recorded here
rather than quietly folded into §10.

| Run | Deployment | Validated at | Result |
| --- | --- | --- | --- |
| sec-1 | `b319a5b0` | 2026-09-21T02:17:01Z | `PROD RESULT: OK (20 checks)`, exit 0 |
| sec-2 | `621711d5` | 2026-09-21T02:25:47Z | `PROD RESULT: OK (20 checks)`, exit 0 |

Eight anonymous security probes, all against production, none mutating:

```
SEC01 anonymous agent creation        -> 401  refused
SEC02 anonymous wallet read (BOLA)    -> 404  not disclosed
SEC03 malformed registration body     -> 422  4xx, never 5xx
SEC04 oversized body (200k password)  -> 400  refused, not parsed into memory
SEC05 forged X-Forwarded-For          -> 200  served; header not trusted for identity
SEC06 garbage bearer token            -> 401  refused, not ignored
SEC08 25-request burst                -> no 5xx; codes={401}
SEC10 public status/health bodies     -> no secret-shaped name
```

**What SEC08 does not prove.** It proves the service stays a well-behaved 4xx
under a burst. It does **not** demonstrate the rate limiter firing: the
registry's limit is well above 25 requests, so no 429 was produced, and the
detail line says `limiter NOT exercised to threshold` rather than implying
otherwise. Driving it to the threshold would mean deliberately hammering
production, which is not a production-safe probe. Rate limiting is covered by
`tests/test_rate_limiting.py`; it is not claimed as a live production proof.

**Why a probe needing a real account is absent.** Agent registration, wallet
ownership, task lifecycle and escrow all require a logged-in user. Registration
is fail-closed while delivery is disabled (§29), and §37 forbids forcing an
account through a direct database write. So the full money-path smoke is
**not achievable in production** while mail is off. That is a consequence of
the fail-closed contract, not an omission — and it is the reason `C01`-`C03`
assert the refusal rather than the flow. The money path itself is covered by
`tests/test_money_invariants.py` and the staging validator.

### §39 — Redis auth, proven against the running process

```
R01 PASS unauthenticated PING refused (AuthenticationError)
R02 PASS authenticated PING succeeded
```

Both halves matter. `R01` alone would also pass if Redis were simply down or
unreachable; `R02` excludes that. This pairing exists because of what happened
during this environment's bring-up (ADR-0008 D11): Redis served with **no
password** while its configuration said otherwise, and only a real connection
attempt revealed it. A refusal check that could not distinguish "refused" from
"unreachable" would have reported PASS on an open Redis.

The password is handed to the client and never recorded, compared or printed; a
test asserts `check_redis_auth` calls no `report.record`.

## 12. The email and account flow, proven live (2026-09-24)

Phase 7 could only prove the **refusal**: with delivery disabled, registration
answered 503 and created nothing. That was the honest contract at the time, but
it left every authenticated production path unproven, because each of them needs
a logged-in user and no user could exist. This is the other half.

Run: `prod-validator` deployment `d8d3f4e5`, from inside the production private
network, against the registry and payment services on their private addresses.

```
PROD-JSON email_flow.validated_at "2026-09-24T06:16:38Z"
PROD-JSON email_flow.canary_email "delivered@resend.dev"
E01 PASS registration through the normal API        -> 201
E02 PASS login BEFORE verification                  -> 403
E03 PASS one unconsumed token found
PROD-JSON email_flow.token_fingerprint "4307d67ccfd1328b"
E04 PASS verify-email with the delivered token      -> 200
E05 PASS replay of the SAME token                   -> 400
E06 PASS login AFTER verification                   -> 200  (token issued, never printed)
E07 PASS authenticated task list                    -> 200  (empty, as a new account must be)
E08 PASS someone else's task id with a valid token  -> 404
E09 PASS owner reads own wallet -> 200; exactly one user wallet at zero balance
E10 PASS another wallet's balance with a valid token -> 404
E11 PASS anonymous wallet list                      -> 401
PROD-EMAIL RESULT: OK (11 checks)   exit 0
```

`E01 -> 201` is the load-bearing line. Registration attempts delivery **before**
it commits, so a 201 is itself the statement that SMTP accepted the message.

### The message was AgentNet's, not a hand-sent test

Resend's outbound log holds exactly one message, created at `06:16:39.069Z` —
the same second as `E01`:

```
From:       "AgentNet" <noreply@mail.agentnet.io.vn>
To:         delivered@resend.dev
Subject:    Verify your AgentNet email address
Status:     delivered
Message-ID: <010001a0d20f1642-130c6b7f-240a-4cf2-84c2-dd726e4e92c3-000000@email.amazonses.com>
```

Its body carries the activation link the application built from
`PUBLIC_BASE_URL`. The body, the link and the token are **not reproduced here**.

### What is proven, and what is not

The token the endpoint accepted is the token the message carried. Three facts
give that, without anyone printing it: registration passes the same
`token_value` it inserted straight into the delivery call (`auth.py`, one
variable, one transaction); `E03` found **exactly one** unconsumed token for that
address; and the message was created in the same second by that registration.

The designed cross-check — comparing `token_fingerprint` against the SHA-256 of
the link in the delivered message — was **not performed**, and deliberately so.
Computing it here would have required putting the raw token into a command, and
a consumed token is still a credential that would then live in a transcript
forever. The identity argument above costs nothing and leaks nothing.

**Public clickability is a separate claim and is NOT proven.** The delivered
link points at `https://api.agentnet.io.vn`, which does not resolve: no web DNS
cutover has happened. The token was consumed over the private validator path. A
human on the internet still cannot complete this flow — not because the flow is
broken, but because production has no public surface yet.

### Secret-leak audit

The production registry's **complete** deploy log for the running deployment
(`94276750`, container start 2026-09-21T15:22Z through this run) is 20 lines:
startup, and two `registration refused: verification email undeliverable`
warnings from the blocked attempts on the 21st. The successful registration
logged **nothing at all** — the handler logs only refusals.

No SMTP password, no API key, no verification token, no activation link, no
`Authorization` header and no JWT appears anywhere in it. This is a whole log,
not a sample of one.

```
EMAIL SECRET LEAK CHECK: PASS
```

### Configuration of record

| | |
| --- | --- |
| Sending domain | `mail.agentnet.io.vn` — **verified** (DKIM, SPF MX, SPF TXT, return-path CNAME) |
| Credential | `agentnet-production-smtp` — sending access, restricted to that domain |
| Transport | `smtp.resend.com:2465`, implicit TLS (the platform drops 465/587) |
| Sender | `AgentNet <noreply@mail.agentnet.io.vn>` |
| Link origin | `PUBLIC_BASE_URL=https://api.agentnet.io.vn` — configured, not yet routable |
| Set on | `prod-registry` only |

The canary account `delivered@resend.dev` and its zero-balance wallet remain in
production. They are the evidence; removing them would need a direct database
write, which is the one thing this environment does not permit itself.
