# Authoritative DNS: ZoneDNS → Cloudflare, and the apex on Railway

**Status: PREPARED, NOT DELEGATED.** The Cloudflare zone exists in `pending`
state with the complete final record set, SSL mode, and a prepared redirect
rule. Nothing public has changed: ZoneDNS is still authoritative, and
production still runs `60559d7f`. The owner does one thing at Nhân Hòa: replace
the ZoneDNS nameservers with the two Cloudflare nameservers (§7). Everything
after that is the post-delegation sequence in §8.

| | |
| --- | --- |
| Why | the apex `https://agentnet.io.vn` has no HTTPS listener. ZoneDNS serves it with a URL-redirect A record (`103.28.36.94`) that answers only on port 80. ZoneDNS offers no apex CNAME/ALIAS, so Railway cannot serve the apex while ZoneDNS is authoritative |
| Registrar | **Nhân Hòa**, unchanged. No transfer. Only the delegation (NS) changes |
| Cloudflare account | `5e30088859e3aaa23f829fc8c072ce60` ("Sonnv.hd34@gmail.com's Account"). It is the only account the connector can reach, and it holds the `sofa-proxy` and `bongda365` Workers |
| Zone | `agentnet.io.vn`, id `3a07bbdcc045e2388a2f1023f353cb95`, type `full`, plan **Free Website**, created 2026-09-25T04:13Z, status `pending` |
| Assigned nameservers | **`aarav.ns.cloudflare.com`**, **`leanna.ns.cloudflare.com`** |
| Production runtime | branch `production` @ `60559d7f`, frozen. No release is part of this migration |

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
| `agentnet.io.vn` (apex) | prod-dashboard:8080 | `e9d21cc2-fe75-4cb5-9783-9c9bb8c021d5` (created 2026-09-25T04:1xZ, no redeploy) | `rfnmkrkb.up.railway.app` | name and value shown only in the Railway dashboard (§2.1) |

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
It is public DNS data, not a credential.

## 3. The record set (Cloudflare zone, loaded before delegation)

| Type | Name | Content | Proxy | Purpose |
| --- | --- | --- | --- | --- |
| CNAME | `@` | `rfnmkrkb.up.railway.app` | **proxied** | apex → prod-dashboard (Cloudflare flattens the apex CNAME) |
| TXT | *apex name from Railway* | *apex value from Railway* | — | Railway ownership for the apex (§2.1) |
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

### 4.1 Redirect rule (prepared, disabled)

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

It is created **disabled**. The first post-delegation step enables it once the
apex answers 200 over HTTPS. Enabled at delegation, it would send every
dashboard user to an apex that Railway may not have verified yet. A 301 is also
cached by browsers, so it must not point at a broken target even briefly.

## 5. DNSSEC / DS gate

Checked 2026-09-25 from the production network (Railway egress): no DS for
`agentnet.io.vn` at the parent, and no DNSKEY at ZoneDNS. With no DS to go
stale, the delegation change cannot produce SERVFAIL. Cloudflare DNSSEC is
`disabled` and stays so until after activation.

## 6. Pre-delegation proof

The pending zone is served by the two assigned nameservers before delegation.
The proof queries **both** of them directly, never a recursive resolver, for
every name in §3, and compares each answer with the table
(`CLOUDFLARE PRE-DELEGATION DNS`). Proxied names answer with Cloudflare
anycast A/AAAA addresses, not the CNAME. That is how the proxy works, and the
CNAME target is checked through the API instead. The recorded result is in
§10.

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

### 8.1 Client identity through Cloudflare: prediction and fix path

The registry trusts only `X-Real-IP`, set by Railway's edge
(`app/proxy_headers.py`, `TRUST_X_REAL_IP=true`). Railway's edge sets it to
the TCP peer. Once Cloudflare proxies a name, the peer is a **Cloudflare edge
address**, so:

* **Spoofing stays closed.** A client-supplied `X-Real-IP`,
  `X-Forwarded-For` or `CF-Connecting-IP` is still overwritten or ignored.
  The post-delegation test repeats the S8 identity test with all three
  headers forged: `X-RateLimit-Remaining` must keep decrementing on one
  bucket.
* **Identity collapses** to Cloudflare's egress addresses. Everyone behind one
  Cloudflare egress IP shares a rate-limit bucket. That causes false 429s under
  load, and one abuser can drain a bucket shared with others. It is not a
  bypass, but it is a real regression in fairness.
* **Direct-to-origin still works.** Requests sent straight to Railway with the
  public Host skip Cloudflare and are identified by their own address.

If the post-delegation test confirms the collapse, the fix is a runtime change
through the normal release gate, not a header allow-list. Trust
`CF-Connecting-IP` **only** when the Railway-set `X-Real-IP` falls in
Cloudflare's published ranges, pinned in the repository. Keep `X-Real-IP`
otherwise, so a direct-to-origin caller cannot pick an identity. Test both
paths. Whether the collapse blocks the LIVE verdict is decided on the measured
result, not assumed here.

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
| I1 | 5 requests on one connection: plain, then forged `X-Forwarded-For`, `X-Real-IP`, `CF-Connecting-IP`, and all three | `X-RateLimit-Remaining` strictly decreasing by 1: one identity, never a fresh bucket |
| I2 | the same from a second egress (a direct-to-origin request with the public Host, if reachable) | its own bucket, independent of I1 |
| E1 | fresh signup with a new address | registered → email from `noreply@mail.agentnet.io.vn` → link on `https://api.agentnet.io.vn` → verified → login 200 |
| E2 | Resend domain `mail.agentnet.io.vn` | `verified`; the E1 message `delivered` |
| P1 | Railway: payment, worker, Postgres, Redis | no domain, no TCP proxy |
| P2 | Railway variable names | no forbidden credential names; Society flags off |

## 9. DNSSEC later

After the zone has been active and stable: enable DNSSEC in Cloudflare, add the
DS it shows at Nhân Hòa, and verify with `dig +dnssec` and a validating resolver.
That is a separate change, never mixed into the delegation.

## 10. Recorded evidence

*Filled in by the preparation run. See the final section of this document's commit.*

## 11. Rollback

| Symptom after delegation | Action | Converges in |
| --- | --- | --- |
| anything DNS-wide | owner: put `ns1`–`ns4.zonedns.vn` back at Nhân Hòa. The ZoneDNS zone was never modified, so it serves exactly the pre-migration records | NS TTL at the parent (up to the `.vn` delegation TTL) |
| apex broken, the rest fine | leave the redirect disabled; dashboard.* keeps serving the UI | immediate |
| redirect misbehaves | disable rule `83096c03…` | immediate at the edge (browsers may keep a cached 301) |
| origin TLS errors (525/526) | confirm SSL mode is `full`, not `strict` | immediate |
| CORS change misbehaves | set prod-registry `CORS_ALLOWED_ORIGINS` back to `https://dashboard.agentnet.io.vn` | one deployment |
