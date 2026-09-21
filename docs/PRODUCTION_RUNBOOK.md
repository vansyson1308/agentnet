# Production runbook (dark)

Production is a **dark** environment: real, isolated, running, and reachable
only on Railway-generated domains. DNS has not moved. `agentnet.io.vn` and its
subdomains are untouched.

## Topology

```
production environment (Railway)
├── Postgres     private, NEW, no staging data
├── Redis        private, NEW
├── registry     PUBLIC (Railway-generated domain) — owns migrations
├── payment      private
├── worker       private
└── dashboard    PUBLIC (Railway-generated domain)
```

No `society-worker`. No `staging-validator`. No simulation. No Jaeger. No
volumes — so production redeploys are zero-downtime (a volume would force brief
downtime even with a healthcheck; ADR-0008 D5).

Services reach each other over the environment's own private network as
`SERVICE.railway.internal`. Each environment has an isolated network, so
production cannot address staging and vice versa.

## Bring-up

Declared in `.railway/production.ts`, which refuses any environment but
`production`:

```bash
railway link --project AgentNet --environment production
railway config plan  --config .railway/production.ts   # preview first
railway config apply --config .railway/production.ts
```

### Secrets (create once, never read back)

Production-scoped **shared** variables. Generated fresh — never copied from
staging, because a shared JWT secret would make a staging token valid in
production:

```bash
openssl rand -hex 32 | railway variable set JWT_SECRET_KEY       --stdin
openssl rand -hex 32 | railway variable set FLASK_SECRET_KEY     --stdin
openssl rand -hex 32 | railway variable set INTERNAL_WORKER_TOKEN --stdin
```

**Recommended owner hardening:** seal these three in the dashboard (variable →
3-dot menu → *Seal*). A sealed value is supplied to builds and deployments but
can never be read back through the UI or the API. Sealing is one-way and is a
UI action, so it is not applied by automation here (ADR-0008 D9).

### Settings not expressible in IaC

Set once after apply:

* generated public domains for **registry** and **dashboard** only;
* **Wait for CI = ON** for all four application services;
* Postgres/Redis remain **no public networking** (the default — do not add a TCP proxy).

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

```bash
python deploy/production/validate.py \
  --registry https://<generated> --dashboard https://<generated> \
  --var-names "$(railway variables --kv | cut -d= -f1 | paste -sd,)" \
  --email-delivery disabled
```

Reports health, network exposure, Society absence, the secret-name audit, the
core smoke path and the email-delivery contract. It reports and never repairs.

## Email delivery / public signup

`EMAIL_DELIVERY_PROVIDER=smtp` in production, through Resend on the sending
domain `mail.agentnet.io.vn`. The credential is a **send-only** Resend key
restricted to that domain; it is set directly in Railway and has never been
read, printed or committed.

**Status as of 2026-09-21:** transport is proven (login, MAIL FROM and RCPT TO
all accepted on port 2465) but the domain is `partially_verified` -- DKIM and
the return-path CNAME verify, the SPF pair does not yet -- so Resend refuses
the send and registration still answers 503. Public human signup is therefore
still blocked, and still blocked *honestly*: nothing half-created.

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

* **DNS**: unchanged. Railway-generated domains only, and they are validation
  surfaces — not advertised, not linked publicly, no custom domain.
* **A2A**: untouched.
* **Production Society**: OFF, and refused by `config.py` in production.
