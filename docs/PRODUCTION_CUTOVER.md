# Production DNS cutover — api / dashboard

**Status: PREPARED, NOT EXECUTED.** Every step below that is not a DNS edit has
been done, or prepared and checked, before the soak gate. The only thing left is
the owner's ZoneDNS change, which is irreducible: nothing in this repository or
its connectors holds authority over the `agentnet.io.vn` zone.

| | |
| --- | --- |
| Cutover not before | **2026-09-24T15:22:00Z** (22:22 +07): the end of the 72-hour production soak that started 2026-09-21T15:22Z |
| Production candidate | branch `production` @ `60559d7f1654436c1062de9cc8a3e6ddde29ce07`, tree of main `adbe0a53b0afe37369d1f9af58562a674fa1c352` (FROZEN: no repin for the cutover) |
| Who edits DNS | the owner only, in ZoneDNS (nameservers `ns1`–`ns4.zonedns.vn`) |
| Prepared | 2026-09-24T09:20Z |

Only two names change: `api` is a pure ADD, and `dashboard` moves from the VPS
to Railway. The apex, `www`, `payment`, `staging`, every `mail.*` record (the
Resend sending domain), MX, NS and the VPS `139.180.143.222` stay untouched
until Stage A has passed. Stage B changes the apex, `payment` and `staging`,
after that, as separate decisions.

## 1. What already exists on Railway

Created 2026-09-24 with the Railway connector (`generate-domain` with a custom
hostname and a target port). Creating a custom domain changes routing
configuration only; it does not rebuild or restart the service.

| Hostname | Service | Target port | Domain id | Railway requires | Status at 09:17Z |
| --- | --- | --- | --- | --- | --- |
| `api.agentnet.io.vn` | prod-registry | 8000 | `9b1bd905-a1c3-46a6-84e5-c31d16c599d9` | CNAME `api` → `0m6buta9.up.railway.app` **and** the ownership TXT (§3) | DNS `REQUIRES_UPDATE`, verified `false`, certificate `VALIDATING_OWNERSHIP` |
| `dashboard.agentnet.io.vn` | prod-dashboard | 8080 | `a9d8d553-57b5-4f9d-849f-3e34a83ce636` | CNAME `dashboard` → `b0vdfe25.up.railway.app` **and** the ownership TXT (§3) | DNS `REQUIRES_UPDATE`, verified `false`, certificate `VALIDATING_OWNERSHIP` |

These are pending states, as they should be before DNS exists. **Do not delete
or recreate these domains.** A recreated domain gets a new CNAME target and a
new TXT value, and the table in §3 would then be wrong.

Verified at the same time:

* No redeploy, restart or variable change came with the domain creation.
  prod-registry's latest deployment is still `c1232e72` (08:13Z release) and
  prod-dashboard's is still `f2339d74`. Staged changes: none.
* No Railway service domain and no TCP proxy exists on any production service.
  payment, worker, Postgres, Redis and the validator have no domain of any kind.
* Ports: prod-registry `PORT=8000` and prod-dashboard `PORT=8080`, matching
  the domains' target ports.
* `.railway/production.ts` now declares exactly these two domains. The
  offline plan against live (the real `railway/iac` SDK evaluated against a
  names-only snapshot) reports:

  | File | vs live now | vs live after Stage A step 1 |
  | --- | --- | --- |
  | main (`adbe0a53`) | `!! DESTROY` both custom domains, plus the validator | same |
  | this change | 0 add / **1 change** (prod-registry `CORS_ALLOWED_ORIGINS`) / validator | 0 add / 0 change / validator |

  **Until this change merges, nobody may `railway config apply` main's
  production file.** On apply, a custom domain the file does not declare is a
  deleted domain. Nothing applies it automatically. Apply is a manual,
  operator-only CLI action (`.railway/README.md`).

## 2. Authoritative DNS before the cutover

Queried directly against `ns1`–`ns4.zonedns.vn` from inside Railway on
2026-09-24T09:10Z. This sandbox's resolver intercepts authoritative queries,
so the probe ran on prod-validator. At least one nameserver answered every
query authoritatively. The occasional single-attempt UDP timeouts showed no
disagreement between servers.

| Name | Records | TTL |
| --- | --- | --- |
| `agentnet.io.vn` (apex) | A `139.180.143.222`; no AAAA, CNAME, TXT or CAA | 300 |
| `dashboard` | A `139.180.143.222`; no AAAA, CNAME, TXT or CAA | 300 |
| `payment` | A `139.180.143.222`; nothing else | 300 |
| `staging` | A `139.180.143.222`; nothing else | 300 |
| `api` | **NXDOMAIN** | — |
| `www` | **NXDOMAIN** | — |
| NS | `ns1`–`ns4.zonedns.vn` | 3600 |
| SOA serial | `2026092101` | — |
| DS / DNSKEY | none: the zone is unsigned | — |
| CAA | none at the apex or at any name probed | — |

**TTL:** the only record being replaced (`dashboard` A) already has TTL 300,
so no TTL lowering has to happen ahead of time. New records go in at TTL 300.

**TLS preflight: CLEAR.** With no CAA record anywhere in the tree, any CA may
issue, including Let's Encrypt, which Railway uses. With no DNSSEC, a
validating resolver cannot fail on a broken chain.

**Conflicts:**

* `api`: nothing exists, so the CNAME and the TXT are pure ADDs.
* `dashboard`: an A record exists. A CNAME cannot coexist with any other
  record at the same name (RFC 1034 §3.6.2), so the A record must be deleted
  first. This is a REPLACE.
* Railway's TXT must sit at the name Railway shows, which differs from the
  CNAME's name. If a panel ever refuses a TXT because a CNAME exists at the
  same name, the TXT name was typed wrong. Re-read it from Railway.

## 3. OWNER DNS TABLE (ZoneDNS)

The CNAME targets below are the exact `requiredValue` Railway returned for
each domain. The Railway connector does **not** expose the TXT ownership
record. Its name and value are shown only in the Railway dashboard, and this
document does not guess them. Copy them from:

* Railway → AgentNet → **production** → **prod-registry** → Settings → Networking → `api.agentnet.io.vn`
* Railway → AgentNet → **production** → **prod-dashboard** → Settings → Networking → `dashboard.agentnet.io.vn`

| # | Action | Type | Name (host) | Value | TTL |
| --- | --- | --- | --- | --- | --- |
| 1 | ADD | CNAME | `api` | `0m6buta9.up.railway.app` | 300 |
| 2 | ADD | TXT | *the TXT name Railway shows for `api.agentnet.io.vn`* | *the TXT value Railway shows for it* | 300 |
| 3 | ADD | TXT | *the TXT name Railway shows for `dashboard.agentnet.io.vn`* | *the TXT value Railway shows for it* | 300 |
| 4 | DELETE | A | `dashboard` | `139.180.143.222` | — |
| 5 | ADD | CNAME | `dashboard` | `b0vdfe25.up.railway.app` | 300 |

Entry notes:

* If ZoneDNS wants the host relative to the zone, type `api` / `dashboard`,
  and type Railway's TXT name with the zone suffix removed. Do not end up
  with `api.agentnet.io.vn.agentnet.io.vn`.
* Row 3 (the dashboard TXT) goes in BEFORE the swap. It sits at its own name,
  so it can coexist with the old A record, and Railway can verify ownership
  as soon as the CNAME lands.
* Rows 4 and 5 go in back to back. Between them `dashboard` does not resolve,
  and a resolver that asks during the gap may cache the negative answer up
  to the zone's SOA minimum.
* **The real dashboard window.** Railway can obtain the certificate only
  after the CNAME points at it. From row 5 until the certificate is `ISSUED`
  (A7), HTTPS visitors get a certificate error. That usually takes minutes,
  and Railway says normally within the hour. Doing `api` first (A3) proves
  the verification and certificate flow on this zone, and shows how long it
  takes, before the one name people reach today is touched.
* Not in this table, and so not to be touched: the apex, `www`, `payment`,
  `staging`, MX, NS, and every record under `mail.agentnet.io.vn` (the Resend
  DKIM, SPF and MX records that email delivery depends on).

## 4. Stage A — the cutover (after 15:22Z, owner-authorised)

Nothing in this stage runs automatically. Each step is taken by a person, in
this order, and the next step waits for the previous one's check.

**A0 — preconditions.** Now ≥ 2026-09-24T15:22:00Z. The cutover PR is green.
The production health matrix is still green: registry `/readyz` 200 from
prod-validator and no crash loop on any prod-* service. Both domains still
exist with the ids in §1.

**A1 — apply the public CORS origin on prod-registry.** It is not a secret.

```
prod-registry  CORS_ALLOWED_ORIGINS
  http://prod-dashboard.railway.internal:8080   ->   https://dashboard.agentnet.io.vn
```

A variable change redeploys prod-registry from the same `production` commit
`60559d7f`. Nothing else changes. Wait for SUCCESS, then check `/readyz` from
the validator. `c1232e72` becomes the immediate rollback target. prod-payment
keeps `http://prod-dashboard.railway.internal:8080`: payment is private
forever, and that value only satisfies its refusal to start without a list.

Why CORS needs any change at all: today's dashboard renders server-side and
calls the registry over private DNS (`REGISTRY_URL`), so it makes no
cross-origin browser call. The public origin is the one browser origin any
future in-browser call would come from. The rule stays the same: never `*`,
never a Railway-generated domain.

**A2 — merge the cutover PR** (`cutover/production-public-cors`). This is the
IaC record, and from then on main's production file equals live again.

**A3 — owner: DNS rows 1 and 2** (`api` CNAME + TXT). Wait until `api`
shows `verified: true` and certificate `ISSUED` (A6/A7 below) before touching
`dashboard`. `api` resolves nowhere today, so nothing can break while it settles.

**A4 — owner: DNS row 3** (`dashboard` TXT, alongside the still-present A record).

**A5 — owner: DNS rows 4 and 5, back to back** (`dashboard` A → CNAME).

**A6 — wait for ownership verification.** Railway shows each domain's DNS
record as valid and `verified: true`. Check propagation against the
authoritative servers directly:

```bash
for ns in ns1 ns2 ns3 ns4; do
  dig +norecurse +short CNAME api.agentnet.io.vn       @$ns.zonedns.vn
  dig +norecurse +short CNAME dashboard.agentnet.io.vn @$ns.zonedns.vn
  dig +norecurse +short A     dashboard.agentnet.io.vn @$ns.zonedns.vn   # must be EMPTY
done
```

**A7 — wait for the certificate:** status `ISSUED` on both domains. Railway
issues Let's Encrypt certificates once ownership is verified, normally within
the hour. A certificate error that Railway marks retryable goes through
`retry-domain-certificate`. Never delete and re-add the domain to get one.

**A8 — external edge smoke** (§5). **A9 — real human signup** (§6).

Stage A is PASS only when A8 and A9 both pass. Stage B does not start before that.

## 5. External edge smoke (A8)

Run the smoke from any machine on the public internet. It sends no burst and
drives no rate limit. It touches no money: no task, no wallet, no escrow. It
prints no credential.

```bash
API=https://api.agentnet.io.vn
DASH=https://dashboard.agentnet.io.vn
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
```

| # | Check | Command | PASS |
| --- | --- | --- | --- |
| E1 | liveness | `code $API/healthz` | `200` |
| E2 | readiness: DB + Redis reachable | `code $API/readyz` | `200` (a `503` names only `db`/`redis`, nothing more) |
| E3 | dashboard liveness | `code $DASH/healthz` | `200` |
| E4 | dashboard renders | `code $DASH/` then `code -L $DASH/` | `302` to `/metaverse` (the root redirects), then `200` HTML after following it |
| E5 | TLS: valid chain + hostname | `curl -sS -o /dev/null $API/healthz && curl -sS -o /dev/null $DASH/healthz` (no `-k`) | both succeed |
| E6 | TLS: certificate detail | `openssl s_client -connect api.agentnet.io.vn:443 -servername api.agentnet.io.vn </dev/null 2>/dev/null \| openssl x509 -noout -issuer -enddate -ext subjectAltName` (same for `dashboard`) | Let's Encrypt issuer; SAN names the host; `notAfter` in the future |
| E7 | HTTP → HTTPS | `curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' http://api.agentnet.io.vn/healthz` (same for `dashboard`) | a 30x to `https://…`. A `200` over plain http is a FINDING to record, not a pass |
| E8 | HSTS | `curl -sS -o /dev/null -D - $API/healthz \| grep -i strict-transport` (a GET: `curl -I` sends HEAD, which the API answers 405) | `max-age=31536000; includeSubDomains` |
| E9 | CORS admits the dashboard origin | `curl -sS -o /dev/null -D - -X OPTIONS $API/v1/auth/user/login -H 'Origin: https://dashboard.agentnet.io.vn' -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: content-type'` | `200`; `access-control-allow-origin: https://dashboard.agentnet.io.vn`; `access-control-allow-credentials: true` |
| E10 | CORS refuses any other origin | same, with `-H 'Origin: https://evil.example'` | `400`; **no** `access-control-allow-origin` header |
| E11 | anonymous mutation refused | `code -X POST $API/v1/agents/ -H 'content-type: application/json' -d '{"name":"edge-probe","description":"x","capabilities":["x"],"endpoint":"https://example.com/h","public_key":"x"}'` | `401`/`403`/`404`, never 2xx (the same expectation as validator SEC01) |
| E12 | garbage bearer refused | `code $API/v1/agents/ -H 'Authorization: Bearer not-a-real-token'` | `401`/`403` |
| E13 | forged XFF / X-Real-IP served normally | `code $API/healthz -H 'X-Forwarded-For: 203.0.113.9, 198.51.100.9' -H 'X-Real-IP: 198.51.100.7'`, then Railway → prod-registry → HTTP logs for that request | `200`, and the edge logged the caller's real address, not the forged values |
| E14 | payment is not public | `code https://payment.agentnet.io.vn/healthz` | not served by Railway: the name still points at the VPS until Stage B. `dig` shows no `up.railway.app` target |
| E15 | no secret-shaped content | fetch `/`, `/healthz`, `/readyz`, `/openapi.json`, `/metrics` and `$DASH/metaverse` and grep for `eyJ[A-Za-z0-9_-]{10,}\.`, `re_[A-Za-z0-9]{16,}`, `-----BEGIN`, `postgres(ql)?://`, `redis://`, `SMTP_PASSWORD`, `JWT_SECRET`, `railway.internal`, `sk-` | no match |

E13 shows only the edge's own view of the caller, and that forged headers
don't break serving. The registry doesn't log client addresses, so E13
cannot show which address reached the app. That property, and the
rate-limit bucket that depends on it, is proven by the staging spoof test
(`docs/RAILWAY_STAGING.md` §11): same edge, same code, same
`TRUST_X_REAL_IP=true`. By owner instruction the burst is not re-driven
against production.

**Known exposure, not a blocker.** Once `api` resolves, three routes become
publicly reachable: the registry's Prometheus `/metrics` (request and escrow
counters, no credentials), and FastAPI's `/docs` and `/openapi.json`. The
staging registry has served them publicly since Phase 4. They carry no
secret, and E15 re-checks that. Restricting them is a code change, so it goes
through the normal release gate after the cutover, not into the frozen
candidate.

## 6. Real human signup (A9)

This is the first time the whole path is walked by a human, through the public
edge, with a real inbox. The owner does it once with their own address. No
database read and no validator are involved: the link in the inbox is the only
source of the token.

The dashboard has no signup or login page. Those routes were removed before
the Railway move and render as inert `#` links. So the signup goes through the
public API directly. The only browser step is clicking the link.

Save the script as a file and run it with `python3 signup.py`. Don't pipe it
through `python3 -` with a heredoc: stdin would then be the script itself,
and the first prompt would fail with `EOFError`. Type the address in
lowercase. Registration normalises the stored address, but login compares
what you type, so a mixed-case domain fails login with `401` even though the
account exists.

```python
# signup.py
import getpass, json, urllib.request, urllib.error
API = "https://api.agentnet.io.vn"
def call(method, path, body=None, token=None):
    req = urllib.request.Request(API + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"content-type": "application/json",
                 **({"authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
email = input("your own inbox address (lowercase): ").strip().lower()
pw = getpass.getpass("new password (>=12, upper+lower+digit): ")
print("register ->", call("POST", "/v1/auth/user/register", {"email": email, "password": pw})[0], "(expect 201)")
print("login before verify ->", call("POST", "/v1/auth/user/login", {"email": email, "password": pw})[0], "(expect 403)")
input("open the email, click the link in a browser, then press Enter ")
status, body = call("POST", "/v1/auth/user/login", {"email": email, "password": pw})
jwt = (body or {}).get("access_token", "")
print("login after verify ->", status, "(expect 200; token", "issued" if jwt else "MISSING", "- never printed)")
if jwt:
    s, tasks = call("GET", "/v1/tasks/", token=jwt)
    print("authenticated task list ->", s, "empty" if tasks == [] else "NOT EMPTY", "(expect 200 empty)")
```

| Step | PASS |
| --- | --- |
| register | `201`. Registration is atomic with delivery, so a 201 means SMTP accepted the message |
| login before verify | `403` |
| the message | arrives from `AgentNet <noreply@mail.agentnet.io.vn>`; the link starts with `https://api.agentnet.io.vn/v1/auth/verify-email?token=` |
| clicking the link | the browser shows `{"ok":true,"message":"verified"}` over a valid certificate. If it shows `400` instead, a mail link scanner may have used the single-use link first. That is not a FAIL as long as the next row passes |
| clicking it again | `400` "Invalid or expired token" (single use) |
| login after verify | `200`, token issued (never printed) |
| authenticated read | `200`, an empty task list |

**Resend check (post-cutover).** In Resend → Emails (or the Resend connector's
`list-emails`), the newest message to the owner's address is from
`AgentNet <noreply@mail.agentnet.io.vn>`, with last event `delivered`. Read
only its status. The body contains the token.

## 7. Rollback

The VPS `139.180.143.222` stays online, unchanged, through the cutover and after it.

| Symptom | Action | Converges in |
| --- | --- | --- |
| dashboard broken on Railway | owner: delete CNAME `dashboard`; re-add **A `dashboard` → `139.180.143.222`, TTL 300**; delete the dashboard TXT | ≤ 300 s (the TTL) |
| api broken | owner: delete CNAME `api` and its TXT. The name returns to NXDOMAIN, as before | ≤ 300 s, plus negative caching up to the zone's SOA minimum |
| registry misbehaves after A1 | set prod-registry `CORS_ALLOWED_ORIGINS` back to `http://prod-dashboard.railway.internal:8080`, or roll prod-registry back to `c1232e72` (restores image and variables together) | one deployment |
| certificate never issues | leave DNS in place; `retry-domain-certificate` if retryable; otherwise DNS rollback as above | — |

Leaving the Railway custom domains in place after a DNS rollback is harmless:
without DNS pointing at them, nothing routes to them. Delete them only if the
cutover is abandoned. Recreating them later produces new CNAME targets and TXT
values.

### Recovery state (captured 09:17Z)

| Service | Running deployment | Commit | Rollback kind |
| --- | --- | --- | --- |
| prod-registry | `c1232e72` (2026-09-24T08:13Z) | `60559d7f` | image rollback available (within the Hobby plan's 72 h retention). After A1 it is the rollback target. The older `94276750` reaches 72 h at ≈15:21Z, so do not count on it |
| prod-payment | `80dd8d66` (2026-09-21T01:04Z) | `2adda709` (runtime subtree identical to `60559d7f`; the release SKIPPED it by watch path) | older than 72 h, so **redeploy/restart only** |
| prod-worker | `42f2afba` (2026-09-21T01:04Z) | `2adda709` (same) | redeploy/restart only |
| prod-dashboard | `f2339d74` (2026-09-21T01:04Z) | `2adda709` (same) | redeploy/restart only |
| prod-postgres | `037a330a` | image `postgres-ssl:18` + volume | public image; the data lives in the volume |
| prod-redis | `a1cd9245` | image `redis:8.2` + volume | public image; the data lives in the volume |

**Public routing rollback is DNS rollback.** It is the fastest and the most
complete, and it is the primary one.

## 8. Stage B — after Stage A PASS (separate owner decisions)

1. **Apex `agentnet.io.vn`**: a ZoneDNS URL redirect (301) to
   `https://dashboard.agentnet.io.vn`, replacing the apex A record. Check
   whether ZoneDNS's redirect service terminates HTTPS for the apex. If it
   serves plain HTTP only, `https://agentnet.io.vn` shows a certificate error.
   The alternative is to serve the apex from Railway, which needs CNAME
   flattening/ALIAS at the apex. Not every DNS host offers that, and it uses
   another custom-domain slot. Decide after checking; not part of Stage A.
2. **`payment`**: delete A `payment → 139.180.143.222`. Production payment is
   private forever, so nothing replaces the name.
3. **`staging`**: delete A `staging → 139.180.143.222` once nothing needs the
   legacy VPS stack. Railway staging uses its `up.railway.app` domains.
4. **The VPS stays online** until a later, explicit decision to retire it.

## 9. Housekeeping around the cutover

* **prod-validator:** private, inert (restart policy NEVER, idle start
  command), no domain, no Society variables. Keep it until the edge validation
  passes. Its `SMTP_PASSWORD` variable exists with a blank value. The Railway
  connector has no variable-delete action, so removing it is an owner click:
  prod-validator → Variables → `SMTP_PASSWORD` → Delete. A plan of main
  already lists the whole service for removal. Deleting it is a later,
  separate decision.
* **Production ruleset drift:** live ruleset `23748786` allows
  `merge`/`squash`/`rebase`, while `deploy/github/production-ruleset.json`
  declares `squash`/`merge`. The owner removes `rebase` in GitHub → Settings
  → Rules. The connectors have no ruleset-admin authority.
* After Stage A PASS: `CURRENT_STATE.md` and `docs/PRODUCTION_RUNBOOK.md` move
  from "pending cutover" to the live public hostnames, in a docs-only change.
