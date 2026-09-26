# Production runbook (dark)

Production is a **dark** environment: real, isolated, running, and reachable
from nowhere outside Railway's private network. **No service has a public
domain** -- not even a Railway-generated one. DNS has not moved.
`agentnet.io.vn` and its web subdomains are untouched; only the mail subdomain
`mail.agentnet.io.vn` (Resend sending domain) is in use.

## Topology

```
production environment (Railway)            source: branch `production`, Wait for CI ON
├── prod-postgres   private  image postgres-ssl:18, volume prod-postgres-volume (/var/lib/postgresql/data)
├── prod-redis      private  image redis:8.2, volume prod-redis-volume (/data), requirepass fail-closed
├── prod-registry   private until DNS  — owns migrations (pre-deploy), the future public API
├── prod-payment    private  forever
├── prod-worker     private  forever
├── prod-dashboard  private until DNS  — the future public UI
└── prod-validator  private  operator instrument, not part of the application (see "Validation")
```

No `society-worker`. No `staging-validator`. No simulation. No Jaeger. The four
application services have no volume, so their redeploys are zero-downtime (a
volume would force brief downtime even with a healthcheck; ADR-0008 D5).

Services reach each other over the environment's own private network as
`prod-<service>.railway.internal`. Each environment has an isolated network, so
production cannot address staging and vice versa. Railway services are
project-wide: the unprefixed `registry`, `payment`, `worker`, `dashboard`,
`Postgres`, `Redis` are **staging's** services and must never be named in the
production IaC.

## Infrastructure as Code

`.railway/production.ts` declares exactly what is live (ADR-0008 D13) and
refuses any environment but `production`. **Read the plan before any apply** --
in a one-file Railway project, omitting a resource or a variable DELETES it:

```bash
railway link --project AgentNet --environment production
railway config plan  --file .railway/production.ts    # must show 0 add / 0 change
railway config apply --file .railway/production.ts    # never with --confirm-destructive unless the plan was read
```

The expected plan against today's environment is **0 to add, 0 to change, 1 to
destroy**: the one destroy is `prod-validator`, which is deliberately not part
of the declared application. Anything else in the plan -- above all any line
touching `prod-postgres`, `prod-redis`, a volume, a `*_PASSWORD` or
`SMTP_*` variable -- means the file and the environment have drifted; stop and
reconcile instead of applying. (No Railway CLI token is available to the
automation that maintains this repository, so the recorded plan is an offline
evaluation of the file with the real `railway/iac` SDK against a live snapshot;
ADR-0008 D13.)

Declared since the SDK can express them: Wait for CI (`checkSuites: true`),
watch paths, restart policy, region, the Redis start command and both volumes.
Not declared, on purpose: public domains (there are none while dark) and the
values of secrets.

### Secrets (create once, never read back)

Production-scoped **shared** variables. Generated fresh — never copied from
staging, because a shared JWT secret would make a staging token valid in
production:

```bash
openssl rand -hex 32 | railway variable set JWT_SECRET_KEY       --stdin
openssl rand -hex 32 | railway variable set FLASK_SECRET_KEY     --stdin
openssl rand -hex 32 | railway variable set INTERNAL_WORKER_TOKEN --stdin
```

`POSTGRES_PASSWORD` (prod-postgres) and `REDIS_PASSWORD` (prod-redis) were
generated once inside Railway; every consumer references them. The IaC declares
them as `preserve()`, so no plan can re-generate or overwrite them.

`SMTP_PASSWORD` (prod-registry) is **owner-managed**: the Resend sending key,
restricted to `mail.agentnet.io.vn`, pasted by the owner directly into Railway.
It is never in git, never in chat, never read back; the IaC declares it as
`preserve()`. If it is ever absent or wrong, registration fails **closed** --
503, nothing committed -- because registration is atomic with delivery. To
rotate it: create a new domain-restricted sending key in Resend, paste it into
`prod-registry → Variables → SMTP_PASSWORD`, let the registry redeploy, run the
email-flow validator, then revoke the old key.

**Recommended owner hardening:** seal the shared secrets and `SMTP_PASSWORD` in
the dashboard (variable → 3-dot menu → *Seal*). A sealed value is supplied to
builds and deployments but can never be read back through the UI or the API.
Sealing is one-way and is a UI action, so it is not applied by automation here
(ADR-0008 D9).

### CORS: the dark value, the dashboard cutover, and the apex

While dark, `CORS_ALLOWED_ORIGINS` on prod-registry and prod-payment was
`http://prod-dashboard.railway.internal:8080`. No browser can present that
origin, so it admits nothing, but it satisfies both services' refusal to
start without an explicit list. The dashboard itself calls the registry
server-side over private DNS and needs no CORS at all.

Stage A of `docs/PRODUCTION_CUTOVER.md` set prod-registry to the dashboard's
public origin until 2026-09-25 (deployment `785df9b2`):

```
prod-registry CORS_ALLOWED_ORIGINS  =  https://dashboard.agentnet.io.vn   (until 2026-09-25)
```

The **live value** since 2026-09-25T13:06Z (deployment `34bd9c7b`, same
`60559d7f` source; carried into the `95830331` release, registry `8c903a22`) is the canonical UI at the apex, declared in
`.railway/production.ts` as `PUBLIC_UI_ORIGIN`. It was applied after the
Cloudflare delegation, once the apex served the dashboard over HTTPS
(`docs/CLOUDFLARE_MIGRATION.md` §10.2):

```
prod-registry CORS_ALLOWED_ORIGINS  =  https://agentnet.io.vn
```

`get_cors_origins()` accepts a comma-separated list of exact origins, but no
transition list is needed. The compatibility host `dashboard.agentnet.io.vn`
is answered by a Cloudflare 301 and serves no page, so it never needs to be an
allowed origin.

prod-payment keeps the private-only value (`PRIVATE_ONLY_CORS_ORIGIN`), because
payment never becomes public. Never use `*`, and never a Railway-generated domain.

## What must never be here

| Name | Why |
| --- | --- |
| `SOCIETY_MODEL_API_KEY`, `LLM_API_KEY`, `DEEPSEEK_API_KEY` | production runs no cognition |
| `SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM` / `_FILE`, `SOCIETY_GITHUB_TOKEN` | production has no promotion authority |
| a `society-worker` service | the Society is a staging faculty |

Audit by **variable name only**. Values are never inspected, printed or compared.

## Migration ownership

**The registry alone** migrates, through a Railway *pre-deploy command* that
runs in a separate container before the new deployment starts and must exit
non-zero on failure:

```
sh -c 'SKIP_DB_BOOTSTRAP=false /app/entrypoint.sh true'
```

The runtime container then starts with `SKIP_DB_BOOTSTRAP=true`. `payment`,
`worker` and `dashboard` never migrate — that is what stops a multi-service
deploy racing `alembic` against itself. Unlike staging, production runs **no**
Society fleet seed: there is no Society here to need those rows.

## Health

| Service | Probe | Note |
| --- | --- | --- |
| registry | `/healthz`, `/readyz` | `/readyz` is the deploy healthcheck |
| payment | `/healthz`, `/readyz` | private |
| dashboard | `/healthz` + index renders | |
| worker | `/metrics` on 9100 | private; doubles as the healthcheck |

Railway probes from `healthcheck.railway.app` and marks a deployment Active only
on a 2xx. It does **not** monitor afterwards — a healthcheck is a release gate,
not uptime monitoring. Continuous monitoring is not yet solved and is not
claimed to be.

## Validation

Validation runs **inside** production's private network, from `prod-validator`: a non-public service built from the registry
image, restart policy NEVER, which clones `$VALIDATOR_REF` at start and runs
one validator. Its only credentials are references to the Postgres/Redis
passwords (the Redis auth check and the verification-token read need them); the
rest of its variables are non-secret -- private URLs, the production variable
NAMES, and `EMAIL_DELIVERY_PROVIDER` as a reference to the registry's. It has no
model credential, no GitHub credential and no SMTP password.

```bash
# core: health, exposure, Society absence, name audit, smoke, security, Redis auth
python deploy/production/validate.py \
  --registry "$PROD_REGISTRY_URL" --dashboard "$PROD_DASHBOARD_URL" \
  --var-names "$PROD_VAR_NAMES"          # EMAIL_DELIVERY_PROVIDER = ${{prod-registry.EMAIL_DELIVERY_PROVIDER}}
# the account path: register -> deliver -> verify -> login -> authenticated reads
python deploy/production/validate_email_flow.py --registry "$PROD_REGISTRY_URL" --payment "$PROD_PAYMENT_URL"
```

Both report and never repair. Every canary is a fresh
`delivered+<label>@resend.dev` address -- Resend's simulated-delivery sink --
never `example.com`, whose null MX would turn each run into a bounce against
the sending domain's reputation. The address does not depend on
`--email-delivery`, so a wrong or missing value cannot make it a real one.

To run one: point the start command at the validator, then change any variable
(e.g. `VALIDATION_RUN`) so Railway creates a NEW deployment -- a *redeploy*
replays the previous deployment, start command included (ADR-0008 D12).

## Email delivery / public signup

`EMAIL_DELIVERY_PROVIDER=smtp` in production, through Resend on the sending
domain `mail.agentnet.io.vn`. The credential is a **send-only** Resend key
restricted to that domain; it is set directly in Railway and has never been
read, printed or committed.

**Status as of 2026-09-24: PROVEN LIVE.** `mail.agentnet.io.vn` is fully
verified and the whole flow ran against production -- register -> AgentNet's own
message delivered -> verify -> replay rejected -> login -> authenticated reads,
11/11 checks, exit 0. `docs/PRODUCTION_DARK_PROOF.md` §12 has the record.

The account path is therefore READY. What is still not true is that a human on
the internet can complete it: the link the message carries points at
`https://api.agentnet.io.vn`, which does not resolve, because the web DNS
cutover has not happened. The proof consumed the token over the private
validator path instead. **Mail transport readiness and public clickability are
different claims** -- do not let a green flow report imply the second.

Registration is **atomic with delivery**: the user, wallet and verification
token are written, delivery is attempted, and only then is the transaction
committed. If delivery is impossible the whole thing is rolled back and the API
answers **503** — no account is created. This replaces the older behaviour where
an account was committed, the link was never sent, and the API still reported
success, leaving an address that could neither log in nor be re-registered.

With `disabled`, **public human signup is blocked, honestly**. The live
configuration that opens it:

```
EMAIL_DELIVERY_PROVIDER=smtp
SMTP_HOST=smtp.resend.com   SMTP_PORT=2465      # NOT 465 -- see below
SMTP_USERNAME=resend        SMTP_PASSWORD=…     # set in Railway; never in a command line
SMTP_FROM=AgentNet <noreply@mail.agentnet.io.vn>
SMTP_TLS=true               SMTP_STARTTLS=false # implicit TLS
PUBLIC_BASE_URL=https://api.agentnet.io.vn      # the link the message carries
```

**Port 2465, not 465.** Railway's egress drops the standard submission ports.
Measured from inside the production network:

```
smtp.resend.com:465  tls=True   FAIL 20.0s TimeoutError
smtp.resend.com:587  tls=False  FAIL 20.0s TimeoutError
smtp.resend.com:2465 tls=True   OK 0.1s  banner='220 Resend SMTP Relay ESMTP'
```

Resend publishes 2465 (implicit TLS) and 2587 (STARTTLS) for exactly this. A
blocked port looks like a 20s hang and then a 503 from registration, which is
indistinguishable from a dead vendor unless you measure the port.

**A domain-scoped key will not send from an unverified domain.** Resend answers
`550 The associated domain with your API key is not verified` at DATA -- after
a successful login and an accepted MAIL FROM/RCPT TO, so auth and sender look
healthy right up to the point of failure. Check the domain's records are all
`verified`, not `partially_verified`, before concluding anything about the
credential.

No vendor is chosen in code. `smtp` speaks to whatever host the owner
configures; Resend is a deployment choice, recorded here and in Railway.

## Recovery

1. **Restart** — the process is wedged; the image is fine.
2. **Rollback** (dashboard) — restores the previous image *and its variables*,
   no rebuild. Only within the retention window (Hobby: 72h).
3. **Redeploy** — rebuilds from a selected deployment's source. Works after the
   image has expired.
4. **Release a revert** — a Git revert through `deploy/production/release.py`,
   the same gate as any other release.

Record for every release: the approved `main` SHA, the production branch SHA and
tree, the Railway deployment ids, the previous known-good deployment ids, and
the migration head. Retention is finite; the written record is what survives it.

**A code rollback is not a schema rollback.** If the release contained a
migration, rolling the image back leaves the new schema in place. That is why
the release gate refuses migrations without explicit owner acknowledgement.

## Boundaries

* **DNS**: `api` and `dashboard` are live through ZoneDNS since the Stage A
  cutover (`docs/PRODUCTION_CUTOVER.md`). Authoritative DNS moves to Cloudflare
  when the owner changes the nameservers at Nhân Hòa
  (`docs/CLOUDFLARE_MIGRATION.md`). Railway holds exactly three custom domains:
  `api.agentnet.io.vn` → prod-registry:8000, and `agentnet.io.vn` (canonical
  UI) plus `dashboard.agentnet.io.vn` (compatibility host, redirected to the
  apex at the edge) → prod-dashboard:8080. There is no Railway-generated domain
  and no TCP proxy, and payment, worker, Postgres and Redis have no domain at all.
* **A2A** (Phase 8, ADR-0009): the gateway ships in the registry image and stays
  404 until enabled; see "A2A enablement" below. `prod-registry` is the only
  service involved. No new service, domain or port is created.
* **Production Society**: OFF, and refused by `config.py` in production.

## A2A enablement (Phase 8)

**Status (2026-09-26): steps 1 and 2 are DONE and proven live** (docs/A2A_LIVE_PROOF.md §5: inbound 37/37, federation 14/14). Step 3 stays off by design.

The A2A code ships dark. Every flag defaults to `false`. Enable **one flag at a time**, validate, then continue. Each step is a variable change on `prod-registry` only, which redeploys only that service.

`.railway/production.ts` declared these variables dark until each was live. It now declares the live state (`a2a`): server and federation `true`, `A2A_CREDENTIAL_KEY: ctx.shared.A2A_CREDENTIAL_KEY`, the Society client and the company cycle `false`. So the file never claims a state production lacks, and an apply never deletes a live variable.

1. **Server:** `A2A_PUBLIC_BASE_URL=https://api.agentnet.io.vn` is already declared; set `A2A_SERVER_ENABLED=true`. Validate:
   - `GET /.well-known/agent-card.json`: a v1 card with two interfaces, `streaming: true`, `pushNotifications: false`;
   - an unauthenticated `POST /a2a` gets `401`;
   - a missing `A2A-Version` gets `-32009`;
   - a canary network search succeeds;
   - a canary free task runs through escrow, then gets `CancelTask`, and the wallet is unchanged.

   Evidence goes in `docs/A2A_LIVE_PROOF.md`.
2. **Federation:** create the production-scoped **shared** variable `A2A_CREDENTIAL_KEY` with Railway's generator, `${{secret(64, "abcdef0123456789")}}` (the same way `JWT_SECRET_KEY` was made: the value renders inside Railway's store, nobody sees or pastes it, and a Fernet key is derived from it). Reference it from `prod-registry` as `${{shared.A2A_CREDENTIAL_KEY}}`, then set `A2A_FEDERATION_ENABLED=true`. Validate:
   - an operator discovery of the reference agent;
   - SSRF refusals (metadata IP, private redirect);
   - one operator outbound call.
3. **Society client:** stays `false` in production. The production Society is OFF by configuration, so this flag has nothing to enable there.

**Rollback:** set the flag back to `false`. The tables are additive and harmless when unused, and the DB is never downgraded automatically.

### Re-running the A2A proof in production

`deploy/railway/a2a_live_proof.py` runs from `prod-validator` with an **owner-verified public identity**. It never reads the database:

* `A2A_PROOF_IDENTITY=public` and `A2A_PROOF_CANARY_EMAIL` (an inbox the owner reads). The password is derived in the validator from its `VALIDATOR_SECRET`, a Railway-generated secret nobody has seen.
* First run: `A2A_PROOF_SIGNUP_ONLY=true`. It signs up through the public API, AgentNet sends the normal verification email, and the proof stops at `A2A PROOF AWAITING OWNER VERIFICATION`. The owner clicks the link. Later runs reuse the account.
* `A2A_PROOF_MODES`: `server` (read-mostly, free skills only), `federation`, or `federation_cleanup`. The federation modes need operator authority. Name the canary in `SOCIETY_OPERATOR_BOOTSTRAP_EMAILS` on `prod-registry` for the window only, then empty it again. The role is evaluated per request and never persisted.
* The start command installs the pinned SDK, clones `$VALIDATOR_REF` and runs the script:
  `sh -c 'pip install -q "a2a-sdk[http-server]==1.1.5" && rm -rf /tmp/repo && git clone -q --depth 1 --branch "$VALIDATOR_REF" https://github.com/vansyson1308/agentnet.git /tmp/repo && python /tmp/repo/deploy/railway/a2a_live_proof.py; echo "validator finished with exit $?"; exec tail -f /dev/null'`.
  The JS interop step downloads Node 22 from nodejs.org and checks it against `SHASUMS256.txt`. Afterwards, restore the idle start command.

The canary account and its three proof agents (`A2A_Proof_Callee`, `A2A_Proof_Caller`, `A2A_Proof_Other`) remain in production. Their skills are free and they hold no funds.

Metrics are bounded and `/metrics` stays unserved in production. The audit trail is `a2a_audit_log`, which is append-only.

## Owner hardening (post-launch; not a launch blocker)

These are owner-only actions. The engineering agent has no authority for them and does not perform them.

### O1. Production ruleset: remove the `rebase` merge method

The `production` branch ruleset (id `23748786`) allows `merge`, `squash` and `rebase`. The release gate records and verifies the exact SHA it releases, so a rebase merge adds nothing and rewrites the reviewed commits.

Steps:

1. GitHub → repository **Settings → Rules → Rulesets → production**.
2. Under "Require a pull request before merging", **Allowed merge methods**: uncheck **Rebase**.
3. Keep Merge and Squash, and change nothing else.
4. Save.

To verify: `GET /repos/vansyson1308/agentnet/rulesets/23748786` shows `allowed_merge_methods: ["merge","squash"]`. Never weaken another rule while doing this.

### O2. DNSSEC for `agentnet.io.vn` (Cloudflare → Nhân Hòa)

Authoritative DNS is on Cloudflare; the registrar is Nhân Hòa (.vn). Enable DNSSEC only when there is time to watch it. A wrong DS record takes the whole zone offline for validating resolvers.

1. **Cloudflare** → the `agentnet.io.vn` zone → **DNS → Settings → DNSSEC → Enable DNSSEC**. Cloudflare signs the zone and shows the **DS record**: key tag, algorithm 13 (ECDSA P-256 SHA-256), digest type 2 (SHA-256) and the digest. Copy them exactly.
2. **Nhân Hòa** → the domain's management panel → DNSSEC / DS record: add one DS record with the same key tag, algorithm, digest type and digest. (.vn delegation is operated by VNNIC; Nhân Hòa submits the DS on the owner's behalf. If the panel has no DNSSEC form, open a support ticket with the four values.)
3. Wait for the parent TTL, then verify:
   - `dig +dnssec DS agentnet.io.vn @8.8.8.8` returns the DS;
   - `dig +dnssec agentnet.io.vn` shows the `ad` flag through a validating resolver;
   - https://dnsviz.net/d/agentnet.io.vn/dnssec/ is all green;
   - Cloudflare shows "DNSSEC is active".
4. **Rollback, in order:** remove the DS at Nhân Hòa first. Wait until the DS has expired from caches (the parent TTL, often up to 24–48 h). Only then disable DNSSEC in Cloudflare. Disabling Cloudflare signing while the DS is still published breaks resolution.

Mail (Resend DKIM/SPF/DMARC) and the Railway custom domains are unaffected, because DNSSEC signs the same records.
