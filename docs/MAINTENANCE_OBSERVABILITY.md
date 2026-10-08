# Maintenance Observability

## Two-tier synthetic monitoring (no model call per probe)

| Tier | Collector | Cadence | Checks | Feeds |
|---|---|---|---|---|
| Fast | `society/surface.py` via `surface_monitor.py` (staging society-worker) | every 10 min (`SOCIETY_PUBLIC_SURFACE_MONITOR_INTERVAL_SECONDS`) | availability, status, redirects, final path, contract markers, API health/readiness, Agent Card, navigation crawl | Society events (unchanged) **and**, with `MAINTENANCE_MONITORING_ENABLED`, `surface_ingest.py` → incidents + SLI samples |
| Deep | `maintenance/browser.py` via `deploy/maintenance/browser_probe.py` | hourly or less (scheduled job with a browser) | navigation, console/JS errors, failed requests, dead links, WCAG 2.2 AA contrast, form labels, raw structured values, unexpected error banners, overflow, critical content, keyboard focus, axe-core, performance budgets | `POST /v1/maintenance/observations/browser` (event-producer JWT, strict structural schema) → incidents + `browser_journey` SLI |

Everything recorded is **structural** (item/rule id, safe path, selector class, counts, numbers,
statuses). Page text, screenshots and HTML never leave the probe and never reach the Society.

## Current real findings (deterministic, 2026-09-27, local dashboard with fixture data)

`python deploy/maintenance/browser_probe.py --local-dashboard` on `main` finds, without any
screenshot from chat:

- `text_contrast` on every critical page, worst ratio **1.06:1** (body text `rgb(0,0,0)` on
  `rgb(5,9,20)`); axe-core `color-contrast` agrees;
- `raw_structured_value` on `/metaverse` (`span.trust-badge` renders a capability dict);
- `unexpected_error_banner` (`div.alert.alert-warning`) on `/metaverse` after a 200;
- `dead_link` placeholders (`#` from the url-build fallback) on every page;
- `navigation`: `/marketplace`, `/login`, `/register` land on `/landing` (the routing incident).

These are two separate incident families — FUNCTIONAL ROUTING (`public_surface`) and EXPERIENCE
QUALITY (`browser_experience`) — each with its own fingerprints; the repairs belong to the Society.
The trusted gates that judge them are `services/dashboard/tests/test_public_surface.py` and
`services/dashboard/tests/test_experience_contract.py` (HELD out of required CI until they pass).

## SLIs and SLOs

| SLI | Source | SLO (7 d) | Min samples | Availability class |
|---|---|---|---|---|
| `availability` | apex + API health items | 99.5% | 50 | yes |
| `api_readiness` | API readiness | 99.5% | 50 | yes |
| `a2a_discovery` | Agent Card | 99% | 50 | yes |
| `auth_journey` | login/register items | 99% | 50 | yes |
| `public_pages` | other contract items | 98% | 100 | no |
| `browser_journey` | deep-tier pages | 95% | 20 | no |
| repair latency | cases | P0 ≤ 6 h, P1 ≤ 24 h, 90% | — | reported |

Targets fit a single-region early-stage product on Railway Hobby behind Cloudflare Free; they gate
change velocity (MAINTENANCE_POLICY.md), not paging.

## Naming (OpenTelemetry-compatible, bounded cardinality)

Contract item names and rule ids play the role of `http.route` (never raw URLs — the X1 fix stays);
deployments are named by Railway deployment id and commit SHA; cases by state and priority; releases
by status. The durable `repair_transitions` log is the trace of record. New OTel span instrumentation
of the kernel is a follow-up (not needed for correctness).

## KPIs and toil

`GET /v1/maintenance/kpis` (30 days): cases, outcomes, MTTD (first observation → confirmation), MTTR
(first observation → AUTO_REPAIRED/AUTO_ROLLED_BACK), auto-repair/rollback/escalation/false-positive
rates, attempts per incident, model cost per resolved incident, recurrence rate, and toil counts
(owner approvals/refusals, freeze lifts, manual merges/releases/abandons/memory corrections).
