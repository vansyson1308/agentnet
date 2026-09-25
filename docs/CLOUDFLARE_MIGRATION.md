# Authoritative DNS: ZoneDNS → Cloudflare, and the apex on Railway

**Status: PUBLIC PRODUCTION LIVE (2026-09-25).** Cloudflare is authoritative
and active. Railway verified all three custom domains. `https://agentnet.io.vn`
serves the production dashboard. `dashboard.agentnet.io.vn` answers 301 to the
apex, keeping the path and query. Live CORS admits exactly
`https://agentnet.io.vn`.

The final edge validation had found two pre-existing registry defects (§10.2):
* a rate-limit identity bypass through unverified bearer tokens (B1);
* request paths leaking into a public `/metrics` (X1).

Both are closed. The fix (#43) shipped through the trusted release gate as
`production` @ `95830331` (#44), and both were re-proven on the public edge
(§10.3).

| | |
| --- | --- |
| Why | the apex `https://agentnet.io.vn` has no HTTPS listener. ZoneDNS serves it with a URL-redirect A record (`103.28.36.94`) that answers only on port 80. ZoneDNS offers no apex CNAME/ALIAS, so Railway cannot serve the apex while ZoneDNS is authoritative |
| Registrar | **Nhân Hòa**, unchanged. No transfer. Only the delegation (NS) changes |
| Cloudflare account | `5e30088859e3aaa23f829fc8c072ce60` ("Sonnv.hd34@gmail.com's Account"). It is the only account the connector can reach, and it holds the `sofa-proxy` and `bongda365` Workers |
| Zone | `agentnet.io.vn`, id `3a07bbdcc045e2388a2f1023f353cb95`, type `full`, plan **Free Website**, created 2026-09-25T04:13Z, status **`active`** since 2026-09-25T07:57:22Z |
| Assigned nameservers | **`aarav.ns.cloudflare.com`**, **`leanna.ns.cloudflare.com`** |
| Production runtime | branch `production` @ `60559d7f` during the migration (no release was part of it); `95830331` since the B1/X1 security release (§10.3) |

## 1. Final architecture

```
https://agentnet.io.vn            -> Cloudflare (proxied) -> Railway prod-dashboard:8080   canonical UI
https://dashboard.agentnet.io.vn  -> Cloudflare 301 -> https://agentnet.io.vn/<path>?<query>   compatibility host
https://api.agentnet.io.vn        -> Cloudflare (proxied) -> Railway prod-registry:8000
mail.agentnet.io.vn (Resend)      -> DNS only: DKIM TXT, return-path MX/TXT/CNAME, never proxied
prod-payment, prod-worker, prod-postgres, prod-redis   -> private forever, no DNS name
```

The dashboard compatibility domain stays attached on Railway, so it keeps
routing and a certificate. Its only visible behaviour is the edge 301.

## 2. Railway custom domains

| Domain | Service:port | Railway id | CNAME target (`requiredValue`) | Ownership TXT |
| --- | --- | --- | --- | --- |
| `api.agentnet.io.vn` | prod-registry:8000 | `9b1bd905-a1c3-46a6-84e5-c31d16c599d9` | `0m6buta9.up.railway.app` | `_railway-verify.api` = `railway-verify=b9d28e46f55a0dce9ed5779a71045881eb65382ed6d009b32f7345682c70bf25` |
| `dashboard.agentnet.io.vn` | prod-dashboard:8080 | `a9d8d553-57b5-4f9d-849f-3e34a83ce636` | `b0vdfe25.up.railway.app` | `_railway-verify.dashboard` = `railway-verify=3893ac0628e856af7a2d211214d7dd3ea6e972a0639ec5759f2d3338dd96c6a2` |
| `agentnet.io.vn` (apex) | prod-dashboard:8080 | `e9d21cc2-fe75-4cb5-9783-9c9bb8c021d5` (created 2026-09-25T04:1xZ, no redeploy) | `rfnmkrkb.up.railway.app` | `_railway-verify` = `railway-verify=43bfe3f41ff59f899b2fd3fbe398555a53d19640b2873ea0a8bc9bcbf461c16d` (§2.1) |

The api and dashboard TXT values were copied byte-for-byte from the
authoritative ZoneDNS answers (`AA` set, one string segment each), not typed.

Plan limit: Hobby allows 2 custom domains per service. prod-dashboard now holds
exactly 2 (apex + dashboard); prod-registry holds 1.

### 2.1 The apex ownership TXT

Railway requires the CNAME **and** a TXT ownership record. Without the TXT, the
custom domain answers 404 even after the CNAME resolves (Railway docs,
*Working with Domains → Custom domains*). The Railway connector's
`generate-domain` and `domain-status` return only the CNAME. The Railway agent
cannot read the verification record either, and no Railway CLI token exists
here. The values are per domain (api and dashboard differ), so the apex value
cannot be derived.

Source of truth: Railway → AgentNet → **production** → **prod-dashboard** →
Settings → Networking → `agentnet.io.vn`. The record goes into the Cloudflare
zone as `TXT <name Railway shows> "<value Railway shows>"`, DNS-only, TTL Auto.
It is public DNS data, not a credential. The owner read it from that panel on
2026-09-25, and it was loaded as record `681af9d00909019c07aa3de53212f1a3`.

## 3. The record set (Cloudflare zone, loaded before delegation)

| Type | Name | Content | Proxy | Purpose |
| --- | --- | --- | --- | --- |
| CNAME | `@` | `rfnmkrkb.up.railway.app` | **proxied** | apex → prod-dashboard (Cloudflare flattens the apex CNAME) |
| TXT | `_railway-verify` | `railway-verify=43bfe3f4…61c16d` (full value §2) | — | Railway ownership for the apex |
| CNAME | `api` | `0m6buta9.up.railway.app` | **proxied** | prod-registry |
| TXT | `_railway-verify.api` | `railway-verify=b9d28e46…70bf25` (full value §2) | — | Railway ownership |
| CNAME | `dashboard` | `b0vdfe25.up.railway.app` | **proxied** | prod-dashboard compatibility host (edge 301 to apex) |
| TXT | `_railway-verify.dashboard` | `railway-verify=3893ac06…96c6a2` (full value §2) | — | Railway ownership |
| TXT | `resend._domainkey.mail` | `p=MIGfMA0GCSq…M637QIDAQAB` (218 chars, byte-identical to ZoneDNS and to Resend) | — | Resend DKIM |
| MX | `send.mail` | `10 feedback-smtp.us-east-1.amazonses.com` | — | Resend return path |
| TXT | `send.mail` | `v=spf1 include:amazonses.com ~all` | — | Resend SPF |
| CNAME | `rsend.mail` | `send.forge.rmta.net` | **DNS only** | Resend return path. Never orange-cloud mail infrastructure |

TTL is Auto everywhere. The zone's NS and SOA are Cloudflare's own and are not
records in this table.

**Not carried over, deliberately:**

| ZoneDNS today | Why it is not in Cloudflare |
| --- | --- |
| apex `A 103.28.36.94` | the ZoneDNS URL-redirect server, HTTP only. It is the defect being fixed; replaced by the apex CNAME |
| `NS ns1–4.zonedns.vn`, `SOA ns1.zonedns.vn` | the old authority |
| — | `payment`, `staging`, `www` are NXDOMAIN at ZoneDNS today and stay absent. No VPS address (`139.180.143.222`) exists anywhere |

Cloudflare's import found **zero** records (API-created zone, no scan), so there
was nothing stale to delete. There is no DMARC record at ZoneDNS, and this
migration does not create one.

## 4. Zone settings (baseline, and what was set)

| Setting | Value | Note |
| --- | --- | --- |
| SSL/TLS mode | **Full** (set explicitly 2026-09-25T04:23:50Z) | Railway's Cloudflare guidance. Not Flexible (Railway's HTTP→HTTPS redirect would loop). Not Full (strict): Railway may not issue a certificate for a proxied name and then presents its `*.up.railway.app` certificate |
| Automatic SSL/TLS | `custom` | Cloudflare will not change the mode on its own |
| Universal SSL | **enabled** | apex + first-level names only, so no Advanced Certificate Manager is needed. The certificate pack is issued at activation |
| CNAME flattening | `flatten_at_root` (default); `flatten_all_cnames: false` | only the apex is flattened, so `rsend.mail` keeps answering as a real CNAME |
| HSTS (edge) | off | no edge HSTS, no preload. The registry's own HSTS header is unchanged |
| DNSSEC | **disabled** | enabled only after activation, as a separate change with the DS at Nhân Hòa (§9) |
| Rocket Loader, Polish, Mirage, Always Online | off | defaults, untouched |
| Cache | default (`cache_level: aggressive` = standard, no cache rules) | no Cache Everything. Dynamic `/v1/*`, auth, wallet, tasks and health responses are not cached by default |
| Bot Fight Mode, Under Attack, WAF custom rules, Access, Turnstile, rate limiting, Workers, Pages, R2, KV, D1 | none | only the Free managed ruleset and L7 DDoS, both present by default |
| Browser Integrity Check | zone default `on`; **off for `api.agentnet.io.vn` only** | a configuration rule added after activation, because BIC blocked non-browser API clients (§4.2) |

### 4.1 Redirect rule (enabled 2026-09-25T13:03:48Z)

Zone ruleset `bc20463c0b2b4630b12322e06f46c7bf`, phase
`http_request_dynamic_redirect`, rule `83096c03992c49e281a0d11d137de729`
(`ref: dashboard_to_apex`):

```
when   http.host eq "dashboard.agentnet.io.vn"
       and not starts_with(http.request.uri.path, "/.well-known/acme-challenge/")
then   301 -> concat("https://agentnet.io.vn", http.request.uri.path), preserve_query_string: true
```

`/foo?a=1` → `https://agentnet.io.vn/foo?a=1`. No other host is matched, so api,
mail and the verification names are untouched. The ACME path is excluded so
Railway can still validate the compatibility host.

It was created **disabled** and enabled at 2026-09-25T13:03:48Z (ruleset
version 2), after the apex had answered 200 over HTTPS. Enabled at delegation, it would send every
dashboard user to an apex that Railway may not have verified yet. A 301 is also
cached by browsers, so it must not point at a broken target even briefly.

### 4.2 Configuration rule: Browser Integrity Check off for the API host only

Zone ruleset `94f0bf6b232b44f8ac4a0dd396ef9d81`, phase `http_config_settings`,
rule `b2071e0ea2f54190bceabf34d204f97a` (`ref: api_no_bic`), added
2026-09-25T08:13Z:

```
when   http.host eq "api.agentnet.io.vn"
then   set_config  bic: false
```

Found by measurement, not assumed. After activation, public registration
through `https://api.agentnet.io.vn` answered **403** to the email-flow
validator (deployment `44da21ba`, E01). Cloudflare's GraphQL analytics
attributed it to `securitySource: bic`. Browser Integrity Check is on by
default on every zone. It challenges requests whose User-Agent looks
non-browser, and a JSON API is called by exactly such clients: SDKs, agents,
`curl`, Python. The registry has its own authentication and rate limiter, so
BIC adds nothing there and breaks legitimate API clients. The rule turns it off
for the API host only. The apex and `dashboard.*` keep the zone default (`on`),
and nothing else changed: no WAF, rate-limiting or bot product was added. The
re-run (deployment `fae33b11`) passed 11/11.

## 5. DNSSEC / DS gate

Checked 2026-09-25 from the production network (Railway egress): no DS for
`agentnet.io.vn` at the parent, and no DNSKEY at ZoneDNS. With no DS to go
stale, the delegation change cannot produce SERVFAIL. Cloudflare DNSSEC is
`disabled` and stays so until after activation.

## 6. Pre-delegation proof

The pending zone is served by the two assigned nameservers before delegation.
The proof queries **both** of them directly, never a recursive resolver, for
every name in §3, and compares each answer with the table
(`CLOUDFLARE PRE-DELEGATION DNS`).

While the zone is **pending**, Cloudflare serves proxied records DNS-only.
`api` and `dashboard` answer with their plain CNAME to the Railway target. The
apex answers with the flattened A record of `rfnmkrkb.up.railway.app` (Railway's
edge, `69.46.46.47` when measured), not with Cloudflare anycast addresses.
Proxying begins at activation, and the post-delegation check D3 then expects
anycast answers. The proxied flag itself is verified through the API.

Consequence for the delegation window: between the NS change and activation,
a resolver that already follows Cloudflare reaches Railway directly.
`api` and `dashboard` keep working that way, because their Railway certificates
are valid. The apex works once Railway has verified its ownership TXT. This
is one more reason the redirect rule stays disabled until the apex is proven.
Activation can be requested early through the API (`activation_check`) once the
parent shows the new NS.

A Free zone that stays **pending for 28 days is deleted automatically**
(Cloudflare, *Pending nameservers*). If the delegation is postponed that long,
the zone and its records must be recreated, and the assigned nameservers may
change. The recorded result is in §10.

## 7. The owner action

In **Nhân Hòa**, replace the current ZoneDNS nameservers
(`ns1.zonedns.vn` … `ns4.zonedns.vn`) with exactly:

```
aarav.ns.cloudflare.com
leanna.ns.cloudflare.com
```

Replace, do not add alongside. A mixed NS set would split resolvers between two
different zones. Leave the ZoneDNS zone itself untouched: it is the rollback
target (§11).

## 8. Post-delegation sequence (a later run; nothing here runs before the owner confirms)

1. **Parent delegation.** The `.vn` parent servers return the two Cloudflare NS
   for `agentnet.io.vn`; `1.1.1.1` and `8.8.8.8` agree.
2. **Zone activation.** Cloudflare status `active`, Universal SSL certificate
   pack `active` for `agentnet.io.vn` and `*.agentnet.io.vn`.
3. **Public DNS.** Every record of §3 is answered identically by both Cloudflare
   NS, `1.1.1.1` and `8.8.8.8`. ZoneDNS-only data is gone.
4. **TLS.** `https://agentnet.io.vn`, `https://dashboard.agentnet.io.vn` and
   `https://api.agentnet.io.vn` all present a valid Cloudflare edge
   certificate.
5. **Railway.** `domain-status` shows the apex ownership verified, and the apex
   serves the dashboard: `GET /healthz` 200 and `GET /` 302 → `/metaverse`.
   api and dashboard are unchanged.
6. **Enable the redirect** (§4.1), then validate
   `/` → `https://agentnet.io.vn/`, `/metaverse` → `…/metaverse` and
   `/foo?a=1` → `…/foo?a=1`, all 301, and api not redirected.
7. **CORS cutover.** prod-registry `CORS_ALLOWED_ORIGINS` →
   `https://agentnet.io.vn`, set as a variable change. That creates a genuine
   new deployment of the same `60559d7f` image, never a `redeploy`. Prove it:
   the apex origin is allowed with credentials, while `https://dashboard.agentnet.io.vn`
   and a foreign origin are refused. The registry accepts a comma-separated
   list of exact origins (`app/security.py:get_cors_origins`), so a two-origin
   transition value is possible. The dashboard needs none, because it calls the
   registry server-side over private DNS and its browser code only makes
   same-origin requests. Change CORS once, straight to the final value.
8. **IaC.** Merge the PR that declares the three domains and
   `PUBLIC_UI_ORIGIN = "https://agentnet.io.vn"`, only after step 7, so the
   file never declares a value production does not have. Until then, main's IaC
   omits the apex domain. An apply from main would **destroy** it: the offline
   plan shows `!! DESTROY custom domain agentnet.io.vn`. Nobody applies
   production IaC without reading the plan (`docs/PRODUCTION_RUNBOOK.md`), and
   no automation holds a Railway CLI token.
9. **API edge.** Health, readiness, anonymous-mutation reject, bad bearer
   reject, a malformed request answered 4xx, no secret in any response or log.
10. **Proxy-header identity** (§8.1).
11. **Signup.** A fresh address: register → the email from
    `noreply@mail.agentnet.io.vn` → the link on `https://api.agentnet.io.vn`
    → verified → login.
12. **Resend.** The domain is still `verified`, and the delivery shows
    `delivered`.
13. **Private services.** payment, worker, Postgres and Redis have no domain and
    no TCP proxy.
14. **Secrets.** A log and response scan finds no credential.

### 8.1 Client identity through Cloudflare: measured, not predicted

The registry trusts only `X-Real-IP`, set by Railway's edge
(`app/proxy_headers.py`, `TRUST_X_REAL_IP=true`). The pre-delegation version of
this section predicted that, once Cloudflare proxied a name, Railway's edge
would see a Cloudflare address as the peer and every client behind one
Cloudflare egress would share a rate-limit bucket. **The measurement
(2026-09-25, deployment `02d50b82`) disproved that prediction.** Railway's
edge recognises the Cloudflare hop and sets `X-Real-IP` to the real client:

| Test | What was sent | Measured |
| --- | --- | --- |
| I1 (through Cloudflare) | plain ×3, forged `X-Forwarded-For`, forged `X-Real-IP`, forged `True-Client-IP` | one bucket, `X-RateLimit-Remaining` 91→90→89→88→87→86, strictly decreasing: no forged header opened a fresh bucket |
| I1 (through Cloudflare) | forged `CF-Connecting-IP`, and all headers forged at once | **403 at the Cloudflare edge**: the request never reaches the origin |
| I2 | Cloudflare path, then direct to the Railway origin with the public Host, then Cloudflare again | 85, 84 → 83, 82 → 81: **the same bucket** on both paths, so the Cloudflare path is keyed on the real client address, not on a Cloudflare egress |
| I3 (direct to origin) | plain, then forged `CF-Connecting-IP`, `True-Client-IP`, `X-Real-IP`, `X-Forwarded-For`, all at once, plain | one bucket 80→74, strictly decreasing: a direct caller cannot choose an identity either |

So spoofing stays closed on both paths, identity does not collapse, and no
code change or header allow-list is needed. The registry keeps trusting only
the Railway-set `X-Real-IP`. `CF-Connecting-IP` is not read anywhere, and a
forged one is refused before the origin. If Railway ever stops recognising
the Cloudflare hop, I2 is the test that shows it: the two paths would land in
different buckets. The fix would then be the pinned-range rule (trust
`CF-Connecting-IP` only when the Railway-set peer is inside Cloudflare's
published ranges), shipped through the release gate.

### 8.2 Post-delegation test matrix (prepared; run from prod-validator, which has real egress)

| ID | Check | Pass when |
| --- | --- | --- |
| D1 | parent `.vn` servers, `NS agentnet.io.vn` | exactly `aarav` + `leanna.ns.cloudflare.com` |
| D2 | `1.1.1.1` and `8.8.8.8`, `NS` / `SOA` | Cloudflare NS and SOA (`dns.cloudflare.com`) |
| D3 | both Cloudflare NS, then `1.1.1.1`, then `8.8.8.8`, for every §3 name | same answers as the table; proxied names give Cloudflare anycast A/AAAA |
| D4 | `payment`, `staging`, `www`, `_dmarc` | NXDOMAIN everywhere; no `139.180.143.222` or `103.28.36.94` anywhere |
| T1–T3 | TLS to apex, dashboard, api | handshake OK, hostname matches, Cloudflare Universal SSL issuer, not expiring |
| A1 | `GET https://agentnet.io.vn/healthz` | 200 from prod-dashboard |
| A2 | `GET https://agentnet.io.vn/` | 302 → `/metaverse`, then 200 |
| A3 | `GET https://api.agentnet.io.vn/healthz`, `/readyz` | 200, 200 |
| R1 | `GET https://dashboard.agentnet.io.vn/` (after enabling the rule) | 301, `Location: https://agentnet.io.vn/` |
| R2 | `…/metaverse` | 301 → `https://agentnet.io.vn/metaverse` |
| R3 | `…/foo?a=1` | 301 → `https://agentnet.io.vn/foo?a=1` |
| R4 | `http://dashboard.agentnet.io.vn/x?y=2` | ends at `https://agentnet.io.vn/x?y=2` |
| R5 | `https://api.agentnet.io.vn/healthz` | 200, **not** redirected |
| C1 | preflight from `https://agentnet.io.vn` (after the CORS change) | 200, ACAO = the apex, ACAC `true` |
| C2 | preflight from `https://dashboard.agentnet.io.vn` and `https://evil.example` | 400, no ACAO |
| S1 | anonymous `POST` to a mutating route | 401/403, never 2xx |
| S2 | `Authorization: Bearer <random>` | 401 |
| S3 | malformed JSON body | 4xx (422/400), no stack trace |
| S4 | `/docs`, `/openapi.json`, `/metrics` | recorded as today (public). No new exposure |
| S5 | response bodies and headers | no secret-shaped value (the validator's L1 scan) |
| I1 | through Cloudflare: plain, then forged `X-Forwarded-For`, `X-Real-IP`, `True-Client-IP`, `CF-Connecting-IP`, and all at once | accepted requests strictly decreasing `X-RateLimit-Remaining` (one identity, never a fresh bucket); a forged `CF-Connecting-IP` may be refused at the edge (403) |
| I2 | Cloudflare path, then direct to the Railway origin with the public Host, then Cloudflare again | recorded: the same bucket means the Cloudflare path is keyed on the real client (§8.1) |
| I3 | direct to the origin with every header forged | one bucket strictly decreasing: the origin path is not spoofable either |
| E1 | fresh signup with a new address | registered → email from `noreply@mail.agentnet.io.vn` → link on `https://api.agentnet.io.vn` → verified → login 200 |
| E2 | Resend domain `mail.agentnet.io.vn` | `verified`; the E1 message `delivered` |
| P1 | Railway: payment, worker, Postgres, Redis | no domain, no TCP proxy |
| P2 | Railway variable names | no forbidden credential names; Society flags off |

## 9. DNSSEC later

After the zone has been active and stable: enable DNSSEC in Cloudflare, add the
DS it shows at Nhân Hòa, and verify with `dig +dnssec` and a validating resolver.
That is a separate change, never mixed into the delegation.

## 10. Recorded evidence

All DNS evidence was captured from **prod-validator** (Railway egress), because
the operator sandbox's DNS is intercepted: an authoritative query there comes
back SERVFAIL without the AA bit. Public DNS data only. No secret was read or
printed.

**ZoneDNS truth, deployment `476ace83`, 2026-09-25T04:18–04:34Z.** Every name
was queried non-recursively at `ns1`–`ns4.zonedns.vn`. ZoneDNS drops many UDP
queries, so each answer is taken from the servers that replied with `AA`.

| Name | Authoritative answer (TTL 300 unless noted) |
| --- | --- |
| apex | `A 103.28.36.94` (URL redirect), `NS ns1–4.zonedns.vn` (3600), `SOA ns1.zonedns.vn … 2026092505`; no TXT, MX, CAA or AAAA |
| `api` | `CNAME 0m6buta9.up.railway.app` |
| `dashboard` | `CNAME b0vdfe25.up.railway.app` |
| `_railway-verify.api` / `.dashboard` | the two TXT values of §2, one string each |
| `resend._domainkey.mail` | TXT, 4 strings that concatenate to the 218-char key; byte-identical to Resend |
| `send.mail` | `MX 10 feedback-smtp.us-east-1.amazonses.com`, `TXT "v=spf1 include:amazonses.com ~all"` |
| `rsend.mail` | `CNAME send.forge.rmta.net` |
| `mail` | exists, no data (NODATA) |
| `www`, `payment`, `staging`, `_railway-verify` (apex), `_dmarc`, `_dmarc.mail`, `_acme-challenge{,.api,.dashboard}`, `send`, `resend._domainkey`, random label | NXDOMAIN (no wildcard) |
| AXFR | refused by all four servers |

**DNSSEC gate.** All eight parent servers (`a`–`h.dns-servers.vn`, which
serve both `io.vn` and `vn`) delegate to `ns1`–`ns4.zonedns.vn` and answer
`DS agentnet.io.vn` authoritatively **empty**. `1.1.1.1` and `8.8.8.8` return
no DS and no DNSKEY; ZoneDNS returns no DNSKEY. **No DS blocker.**

**Current edge.** The apex answers `tcp/443` connection-refused and `tcp/80`
open. `https://api…/healthz`, `/readyz` and `https://dashboard…/healthz`,
`/readyz` are all 200, and both certificates are Let's Encrypt, valid to
2026-12-24. Society flags are all off (`scripted`, `disabled`, fleet 0). Live
CORS: the dashboard origin is allowed with credentials, while the apex and
foreign origins get 400 with no ACAO.

**Cloudflare pre-delegation proof, deployment `b58dd602`, 2026-09-25T04:41Z.**
Both `aarav` and `leanna.ns.cloudflare.com` were queried directly:

* SOA and NS authoritative (`aarav.ns.cloudflare.com. dns.cloudflare.com.`,
  NS = exactly the two).
* Routing, pending mode: apex `A 69.46.46.47` (= `rfnmkrkb.up.railway.app`),
  `api CNAME 0m6buta9.up.railway.app`, `dashboard CNAME b0vdfe25.up.railway.app`.
* `_railway-verify.api`, `_railway-verify.dashboard`, DKIM and SPF TXT exact
  byte-for-byte. MX exact. `rsend.mail` a real CNAME (not proxied).
* `payment`, `staging`, `www`, `_dmarc` and a random label: NXDOMAIN. No
  `139.180.143.222` or `103.28.36.94` in any answer.
* Parity: every carried record is identical to what ZoneDNS serves at the same
  moment.
* **Result 49/51.** The two failures are the apex ownership TXT, absent on both
  nameservers (§2.1).

**Final pre-delegation proof, deployment `7eb4cc88`, 2026-09-25T07:47Z**, after
the apex TXT was loaded: both nameservers serve `_railway-verify` exactly, and
every other check above repeats. **Result 51/51:
`CLOUDFLARE PRE-DELEGATION DNS: PASS`.** SOA serial `2415808028`.

**Cloudflare API state.**
* All 10 intended records are loaded. The apex ownership TXT was added after
  the owner supplied it (§2.1).
* Proxied: `@`, `api`, `dashboard`. DNS only: `rsend.mail`.
* Rulesets: only the redirect entrypoint (1 rule, disabled) and Cloudflare's
  default managed sets. No cache, custom firewall, rate-limit, transform or
  origin rules; no page rules; no Worker routes.

**Railway.** No production service was redeployed. registry `785df9b2`,
dashboard `f2339d74`, payment `80dd8d66`, worker `42f2afba`, postgres
`037a330a` and redis `a1cd9245` are all still the live deployments. The apex
domain shows `DNS_RECORD_STATUS_REQUIRES_UPDATE` / `VALIDATING_OWNERSHIP`,
which is expected before delegation.

### 10.1 Post-delegation evidence (2026-09-25)

**Delegation.** The owner replaced `ns1`–`ns4.zonedns.vn` at Nhân Hòa with
exactly `aarav` + `leanna.ns.cloudflare.com` (replaced, not added alongside).
All eight parent servers `a`–`h.dns-servers.vn` delegate to the two Cloudflare
NS, with a delegation **TTL of 43200 s (12 h)**. There is still no DS at the
parent.

**Activation.** The zone became `active` at **2026-09-25T07:57:22Z**. The
Universal SSL pack `99a75511…` (Let's Encrypt, `agentnet.io.vn` +
`*.agentnet.io.vn`, expires 2026-12-24) became `active` about five minutes
later.

**The TLS gap.** Between activation and certificate deployment (about
07:57–08:02Z), the edge had no certificate for the zone. HTTPS handshakes to
api and dashboard failed for those few minutes (edge probe `62319800`, 4/22).
It resolved on its own, and no rollback was needed. The gap is inherent to
activating a Free full-setup zone whose records are already proxied, because
Cloudflare issues Universal SSL only after activation. For a future migration
of a live name, keep the proxied records DNS-only through activation and switch
proxying on once the certificate pack is `active`.

**Public DNS.**
* The first delegation probe (`92a98419`, 07:57Z) scored 53/68. The zone was
  not yet active, so proxied names still answered DNS-only. `8.8.8.8` also
  still served its cached ZoneDNS delegation.
* By 08:14Z (`f4972843`), the Railway system resolver (`fd12::10`), `1.1.1.1`,
  `8.8.8.8`, `9.9.9.9` and OpenDNS all returned the Cloudflare NS and the exact
  apex ownership TXT.
* Both Cloudflare NS answer SOA serial `2415808984` authoritatively. The apex
  answers Cloudflare anycast (`104.21.48.12`, `172.67.175.181`) and
  `_railway-verify` exactly (`d5114e5d`, 08:23Z).

**The old authority is still up.** `ns1`–`ns4.zonedns.vn` still answer
`agentnet.io.vn` authoritatively with the pre-migration zone: apex
`A 103.28.36.94`, `NXDOMAIN` for `_railway-verify`, `NS` TTL 3600, SOA
`2026092505`. That keeps rollback instant (§11). It also means a resolver that
cached the old delegation before the change can keep seeing the old zone until
that cache expires, at most the 12 h parent TTL.

**Edge** (prod-validator, pinned-resolution probe, deployment `02d50b82`,
08:10Z):
* TLS to all three names: handshake OK, hostname match, Cloudflare Universal
  SSL. api `/healthz` and `/readyz` 200. The dashboard is served through
  Cloudflare.
* S1–S6 pass: anonymous mutation 401/403, random bearer 401, malformed JSON
  4xx without a stack trace, `/docs` `/openapi.json` `/metrics` unchanged, and
  no secret-shaped value in 66 bodies and headers.
* I1–I3 as in §8.1.

**Signup** (deployment `fae33b11`, 08:14Z, validator source `60559d7f`):
E01–E11 11/11 through `https://api.agentnet.io.vn`. Register 201, login before
verification 403, the delivered token verifies (200), a replay is refused
(400), login 200, own task list 200 and empty, a foreign task 404, own wallet
200 with exactly one zero-balance wallet, a foreign wallet 404, an anonymous
wallet list 401. The message from `noreply@mail.agentnet.io.vn` was
`delivered` by Resend and landed in the Gmail inbox. The Resend domain
`mail.agentnet.io.vn` is still `verified`: every mail record moved byte-for-byte
and stayed DNS-only. The verification link carries its single-use token in the
query string by design. Edge request logs may record it, but E05 proves a
consumed token is refused.

**Private services.** payment, worker, Postgres, Redis and the validator have
no domain. There is no TCP proxy on any production service. No forbidden
credential name appears among the variables of registry, payment or worker.

**Logs.** From 07:30Z on:
* prod-registry emitted no deploy-log lines after its 2026-09-24T23:46Z start
  (it logs no requests).
* prod-dashboard emitted request lines only.
* A filter for `eyJ`, `Bearer`, `Authorization`, `token=`, `SMTP_PASSWORD`,
  `BEGIN`, `re_`, `password` and `secret` matched **0** lines in both.

**Frozen runtime.** No production application service was redeployed. registry
`785df9b2`, dashboard `f2339d74`, payment `80dd8d66`, worker `42f2afba`,
postgres `037a330a` and redis `a1cd9245` are all still the live deployments.

**Railway apex.** Domain `e9d21cc2` stays `verified: false` /
`CERTIFICATE_STATUS_TYPE_VALIDATING_OWNERSHIP`, so Railway answers the apex
with 404 (A1/A2 fail).
* Everything on the DNS side is in place and public: the proxied flattened
  CNAME, SSL Full, and the exact TXT on every resolver measured.
* A same-port `update-domain` was a no-op. One `retry-domain-certificate`
  (Railway's documented non-destructive re-check) was requested at 08:24Z.
* Railway re-checks on its own schedule and documents up to 72 h. The 12 h
  parent TTL above bounds any stale delegation cache.
* The redirect stays disabled and live CORS stays on the dashboard origin until
  the apex serves 200 (§8 steps 5–7).

### 10.2 Canonical cutover and final validation (2026-09-25)

**Railway.** `agentnet.io.vn` (`e9d21cc2`), `dashboard.agentnet.io.vn` and
`api.agentnet.io.vn` are all `verified: true`, certificate `VALID`. The apex
verified hours after delegation. Two `retry-domain-certificate` requests had
changed nothing earlier, so the most likely cause was cached delegation data
(parent TTL 12 h).

**Phase 0**, before any mutation (probe deployment `3185d5a2`, 12:58Z):
* TLS to all three names valid (Let's Encrypt YE2, apex + wildcard).
* Apex `/healthz` and `/readyz` 200.
* Apex `/` 302 → `/metaverse`, which answers 200 (8.5 kB, "J.A.R.V.I.S. —
  Metaverse"). No loop, no Railway 404, same-origin assets 200.
* The API's health and readiness 200.

**Mutations, in order:**
1. The redirect rule enabled (13:03:48Z).
2. prod-registry `CORS_ALLOWED_ORIGINS=https://agentnet.io.vn`, as a variable
   change. That was a genuine new deployment `34bd9c7b`, `SUCCESS` 13:06:51Z,
   source `60559d7f`. The rollback target is `785df9b2`.

No other production service was redeployed.

**Phase 1** (probe deployment `2024e6fd`, 13:18Z), **38/40**:

| Area | Result |
| --- | --- |
| Redirect | `http://` and `https://dashboard…/` → 301 `https://agentnet.io.vn/`; `/metaverse`, `/metaverse?x=1&y=2`, `/a/b%20c?x=1&y=2`, `http://…/x?y=2` all keep path and query; the target loads 200; api not redirected |
| CORS | apex admitted on preflight and on a simple GET (ACAO = apex, credentials `true`, `Vary: Origin`); `https://dashboard.agentnet.io.vn` and a foreign origin refused (400, no ACAO) |
| Security | anonymous mutation 401, random bearer 401, malformed and 1.5 MiB bodies 422, anonymous object reference 404, unknown route 404, no 5xx, no secret-shaped value in 136 bodies and headers |
| Identity (headers) | I1–I3 as in §8.1: forged `X-Forwarded-For`, `X-Real-IP`, `True-Client-IP` ignored; forged `CF-Connecting-IP` refused at the edge; the Cloudflare path and the direct path share the real client's bucket |
| **B1: FAIL** | an **unverified** `Authorization: Bearer <random>` gets its own bucket at the agent tier (limit 300, remaining 299), each time. The caller chooses its rate-limit identity, so the limiter can be bypassed on login and register |
| **X1: FAIL** | public `/metrics` labels requests with the **literal** path. The route-template lookup never sees `scope["route"]`, because the edge client-address middleware copies the scope. Real object UUIDs and every scanner path (`/.env`, `/.aws/credentials`, …) appear there (199 series after one day), and every new URL creates a new series |

`/docs`, `/redoc` and `/openapi.json` are public by design. They carry no secret
and describe routes only. There are no debug or admin endpoints (`/debug`,
`/admin`, `/v1/debug`, `/.env`, `/config` all 404).

**The fix** was prepared with regression tests (at this point nothing was pushed or released):
* **Rate limiter:** only a JWT the registry signed and that has not expired
  names a bucket (keyed on its subject). Anything unverified falls back to
  the edge-reported peer address at the default tier.
* **Metrics:** labels come from the route table (`<unmatched>` otherwise).
  `/metrics` is not served when `ENVIRONMENT=production`.

The new tests fail on the current code and pass on the fix, and the full
suite passes. The fix shipped through the normal gate: PR → CI → main →
staging evidence → `deploy/production/release.py`. §10.3 records the release
and the re-run of B1 and X1.

**Signup and email after the cutover** (validator deployment `3accd540`, source
`60559d7f`, 13:20Z, a fresh canary address, all through
`https://api.agentnet.io.vn`): **11/11**.
* Register 201. Login before verification 403.
* The delivered token verifies (200), and a replay is refused (400).
* Login 200. Own task list 200 and empty. A foreign task 404.
* Own wallet 200 (one wallet at zero). A foreign wallet 404. Anonymous
  wallet list 401.

Resend shows the message ("Verify your AgentNet email address", sender
`AgentNet <noreply@mail.agentnet.io.vn>` per IaC) as `delivered`.
The Resend domain `mail.agentnet.io.vn` is still `verified`, and each record
(DKIM, MX, SPF, return path) is `verified`.

### 10.3 Security closure release (2026-09-25)

**Source.**
* #42 (docs/IaC, `6d00af9a`) and #43 (the B1/X1 fix, `57dab99c`) merged to
  `main` through the ruleset.
* **FINAL_MAIN_SHA = `57dab99c0c8f38cd6f0958b65bc3e5283e281422`**, pinned.
* Main CI run `36144428064`: success, six jobs.
* Diff from production `60559d7f`: 12 files. The runtime part is
  `services/registry/app/api/rate_limiter.py` and
  `services/registry/app/health.py` only (payment, worker and dashboard: 0
  files). The rest is the #42 IaC declaration, docs and tests. The service
  diff is byte-identical to the reviewed patch.

**Gate.**
* `release.py` without `--allow-sensitive` refused on exactly one category,
  `release_machinery` (`.railway/production.ts`, the #42 declaration).
* Every other check passed: target on `main`, main CI green, no freeze.
* Staging evidence:
  * registry deployed the target itself (staging `b2a72e6f`, SUCCESS);
  * payment, worker and dashboard subtrees are unchanged.
* Re-run with the owner's scoped acknowledgement and `--execute` to create
  `release/prod-57dab99c0c8f`, whose tree equals the target tree
  (`ec2dae95`).
* #44 into `production`: PR CI `36146838123` green, merged with a merge
  commit (`95830331`, parents `60559d7f` + `57dab99c`), tree `ec2dae95` =
  target.
* Production push CI `36148686204`: success.

**Deploy.**
* Wait for CI held prod-registry `8c903a22` (WAITING 14:37Z) until CI
  succeeded. It then built and reached `SUCCESS` at 14:55:07Z after
  `/readyz`. The migration step was a no-op.
* prod-payment, prod-worker, prod-dashboard and prod-validator were
  `SKIPPED` by their watch paths, so no unchanged service was redeployed.
* Rollback target: registry `34bd9c7b` (source `60559d7f`, apex CORS).

**B1/X1 re-proof, run first** (validator deployment `be9f8d5c`, 15:05Z,
public edge), **10/10**:

| Check | Result |
| --- | --- |
| B1 `GET /` | 5 distinct unverified bearers (random 24/1/80 chars, a forged HS256 "agent" JWT and an `alg=none` "agent" JWT), interleaved with plain requests on one connection. **One** bucket: limit 100, remaining 99 → 89, exactly −1 per request |
| B1 login | same sequence on `POST /v1/auth/user/login` with an empty body (422, no credential is ever tried): one bucket, 88 → 78 |
| B1 register | same on `POST /v1/auth/user/register` (422, no account is created): one bucket, 77 → 67 |
| B1 tier | every unverified request stays at the default tier (100); the agent tier (300) is never granted |
| X1 | `/metrics`, `/metrics/` and `/metrics?x=1` all return 404 with no Prometheus payload. The apex does not proxy it |
| Society | public `/v1/society/status` reports `runtime_enabled=false` |

The probe was dry-run first against the real middleware. The released
limiter passed 10/10. The pre-release limiter (`60559d7f`) failed every B1
check: separate buckets, and the 300 tier for short garbage tokens. No load
was used; counters are the evidence.

**Full edge suite** (validator `c8a27846`, 15:11Z): **40/40**.
* Apex, TLS, redirect, CORS and the security checks all pass.
* B1 passes. Identity checks I1 and I3 pass: forged `X-Forwarded-For`,
  `X-Real-IP` and `True-Client-IP` are ignored, and a forged
  `CF-Connecting-IP` is refused at the edge.
* X1 returns 404.
* No secret-shaped value appears in 142 response bodies and headers.

**Signup** (validator `85f01a11`, source `95830331`, 15:14Z, fresh canary
address): **11/11**.
* Register 201. Login before verification 403.
* The delivered token verifies (200), and a replay is refused (400).
* Login 200. A foreign task 404. Own wallet 200 (one wallet at zero). A
  foreign wallet 404. Anonymous wallet list 401.

Resend shows the message as `delivered`, and `mail.agentnet.io.vn` is still
`verified`.

**Audits.**
* **Private services:** payment, worker, Postgres and Redis have no domain
  and no TCP proxy. The registry has only `api.*`; the dashboard only the
  apex and `dashboard.*`.
* **Secret audit:** clean for the new registry deployment and the live
  payment, worker and dashboard deployments. The registry logs no requests,
  and Railway's HTTP log records paths without query strings, so the
  verification token never appears. No JWT, Authorization header, SMTP
  password, Resend key, DB/Redis password, GitHub or model credential.
* **Society boundary:** there is no production Society service. No model or
  GitHub credential variable exists on prod-registry. The Society runtime is
  inert (`runtime_enabled=false`), auto-merge is declared `false` and
  promotion `disabled`. The staging Society is untouched: its only change is
  the routine auto-deploy of `main`.

**Non-blocking follow-ups (not part of the LIVE verdict):**
* the dashboard still runs `flask run`; move it to a WSGI server;
* the owner should remove `rebase` from the `production` ruleset's allowed
  merge methods, since releases always use a merge commit;
* enable DNSSEC and publish the DS record at Nhân Hòa (§9).

Also observed: the Railway origin is reachable directly, bypassing
Cloudflare (I2). That path shares the client's rate-limit bucket (I3), so it
is not an identity bypass.

## 11. Rollback

| Symptom after delegation | Action | Converges in |
| --- | --- | --- |
| anything DNS-wide | owner: put `ns1`–`ns4.zonedns.vn` back at Nhân Hòa. The ZoneDNS zone was never modified, so it serves exactly the pre-migration records | NS TTL at the parent (up to the `.vn` delegation TTL) |
| apex broken, the rest fine | leave the redirect disabled; dashboard.* keeps serving the UI | immediate |
| redirect misbehaves | disable rule `83096c03…` | immediate at the edge (browsers may keep a cached 301) |
| origin TLS errors (525/526) | confirm SSL mode is `full`, not `strict` | immediate |
| an API client is challenged or blocked at the edge | confirm configuration rule `api_no_bic` (§4.2) is enabled; check GraphQL `securitySource` for the blocking product | immediate |
| CORS change misbehaves | set prod-registry `CORS_ALLOWED_ORIGINS` back to `https://dashboard.agentnet.io.vn` | one deployment |
