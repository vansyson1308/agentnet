# The public-surface contract and the Society's eyes

The Society could repair software, but it could not see the product. The
dashboard's routes to `/marketplace`, `/login` and `/register` were lost in
May 2026, when the retired Hermes backlog bot rewrote `main.py`. Its LLM
elided most routes as `# ... (rest of file preserved, unchanged)`, a marker
later reworded to `[TRUNCATED -- preserve when editing]`. A fallback,
`_stale_template_link`, then turned every missing `url_for` into `#`. Every
missing page answered `302 → /landing → 200`. Required CI never saw it,
because the dashboard's own tests live outside `pytest tests/`, and no
monitor watched the public pages.

This document describes the fix for that class of blindness.

## 1. One contract

`services/registry/app/society/public_surface_contract.json` is the single
machine-readable description of the public product. For each item it states:

| Field | Meaning |
| --- | --- |
| `name`, `origin` (`ui`/`api`), `path` | what is requested, anonymously |
| `initial_status` | allowed first status (e.g. `[200]`, or a redirect for `/`) |
| `final_status`, `final_path` | where the request must end after redirects |
| `markers` | page-specific text that must be present (all of them) |
| `severity` | `critical` (P0/P1-like), `major`, `minor` |
| `monitor` | whether the Society's monitor checks it |
| `intent` | one line of product intent |

Items:

| Item | Path | Final | Markers | Severity |
| --- | --- | --- | --- | --- |
| `ui_root` | `/` | `/metaverse` | Metaverse Command Center | critical |
| `landing` | `/landing` | same | Explore Marketplace | major |
| `metaverse` | `/metaverse` | same | Metaverse Command Center | major |
| `network` | `/network` | same | AgentNet A2A Network | major |
| `marketplace` | `/marketplace` | same | Agent Registry | major |
| `login` | `/login` | same | Sign In, `name="password"` | critical |
| `register` | `/register` | same | Initialize Access, `name="password"` | critical |
| `api_health` | api `/healthz` | same | `"status"` | critical |
| `api_ready` | api `/readyz` | same | `"status"` | critical |
| `a2a_agent_card` | api `/.well-known/agent-card.json` | same | supportedInterfaces, JSONRPC | critical |

Two more rules apply:

* **`navigation`** (major). This is a bounded crawl from landing,
  metaverse, network, marketplace, login and register. Every same-origin
  link and form action must reach its own page, or the login page for a
  protected one. A bare `#` or a `javascript:` link counts as a dead link.
* **`assets`** (minor). Local stylesheets, scripts and images must load;
  an empty `204` counts as missing.

**A 200 is not enough.** A missing page that redirects to the landing page
fails as `masked_by_landing_redirect`. A 200 placeholder fails on its
markers. Every failure is one of these classes: `unreachable`, `timeout`,
`server_error`, `client_error`, `unexpected_status`,
`masked_by_landing_redirect`, `wrong_final_path`, `marker_missing`,
`redirect_loop`, `offsite_redirect`, `response_too_large`,
`placeholder_link`, `empty_asset`.

## 2. One checker, three consumers

`services/registry/app/society/surface.py` implements the checks with
deterministic HTTP (`httpx`). It never uses a model. An observation holds
**structure only**:

* statuses, final path, hops and latency;
* which of the contract's own markers were missing;
* a failure class chosen by the code.

Page text is never copied into an observation. A link found on a page is
reported only if its path matches `^/[A-Za-z0-9._~/-]{0,127}$`. Anything
else is counted, not quoted, and offsite links are ignored.

The three consumers are:

1. **The Society's synthetic monitor** (`surface_monitor.py`, §3).
2. **`deploy/public_surface_validate.py`**, for local, staging and
   production runs. It is anonymous and read-only. It prints one `SURFACE`
   line per observation and one `SURFACE-JSON` line, and exits 1 when a
   major or critical item fails.

   ```bash
   python deploy/public_surface_validate.py --env production
   python deploy/public_surface_validate.py --env staging
   python deploy/public_surface_validate.py --ui http://localhost:8080 --api http://localhost:8000
   ```

3. **The dashboard acceptance gates**,
   `services/dashboard/tests/test_public_surface.py`. They run the same
   checks against the Flask app through `httpx.WSGITransport`:
   * every contract item;
   * the navigation, form and asset crawl;
   * every `url_for` and literal `href` in a rendered template names a real
     route;
   * no `url_build_error_handlers` fallback;
   * no truncation markers;
   * anonymous requests end on the page or at `/login`, never 404, 500 or
     `/landing`;
   * an unknown path answers 404;
   * the login and register forms post to real handlers.

   These are the gates a dashboard repair must pass (Society QA runs them
   as acceptance tests). They become a required CI gate once the repair
   lands (docs/TEST_MATRIX.md).

## 3. The monitor

The staging society-worker runs the monitor inside the one Society control
plane. There is no external cron, no second loop and no model call per
probe. Production has no Society, and `config.py` refuses
`SOCIETY_PUBLIC_SURFACE_MONITOR_ENABLED=true` there.

| Setting | Default | |
| --- | --- | --- |
| `SOCIETY_PUBLIC_SURFACE_MONITOR_ENABLED` | `false` | staging IaC declares `true`; it runs only while `SOCIETY_RUNTIME_ENABLED` is on |
| `SOCIETY_PUBLIC_SURFACE_MONITOR_INTERVAL_SECONDS` | 600 | ≥ 60 |
| `SOCIETY_PUBLIC_SURFACE_FAILURE_THRESHOLD` | 2 | ≥ 2: one timeout is noise |
| `SOCIETY_PUBLIC_SURFACE_COOLDOWN_SECONDS` | 21600 | one anomaly per distinct failure set per window (≥ 600); staging IaC uses 3600 so a persisting regression is re-raised hourly |
| `SOCIETY_PUBLIC_SURFACE_MAX_EVENTS_PER_DAY` | 6 | hard cap |
| `SOCIETY_PUBLIC_SURFACE_TIMEOUT_SECONDS` | 10 | per request |
| `PUBLIC_PRODUCT_UI_ORIGIN` / `PUBLIC_PRODUCT_API_ORIGIN` | the public production origins | bare `https://` origins |

**How a probe runs.** Probes run in a thread (`asyncio.to_thread`), so
public HTTP never stalls Society work. A monitor exception is logged and
counted (`society_public_surface_monitor_errors_total`). It never raises,
and the next probe waits a full interval.

**Events.**

* `public.surface.anomaly` is system-authored, and only the Scout wakes on
  it. It is emitted when a **major or critical** failure repeats on
  `threshold` consecutive checks. Minor findings never become Society work.
  * The payload is structural: failing items with status, final path,
    expected path, missing contract markers, failure class, consecutive
    count and the item's contract intent. It also carries the contract
    file, the verification test and the product source directory.
  * It is idempotent per failure set and cooldown window, and capped per day.
  * It is a world signal, so a Scout proposal must cite evidence.
* `public.surface.recovered` is emitted exactly once per anomaly (its
  idempotency key is the anomaly id), after `threshold` consecutive healthy
  checks. It shares the anomaly's correlation, so the story reads end to end.

**Incident freeze.** An availability failure (`unreachable`, `timeout`,
`server_error`) on a critical item opens an incident freeze with source
`public_surface`. That stops autonomous merges until an operator lifts it.
A wrong or missing page does **not** freeze: its repair is itself a merge,
and every gate still applies to it.

**Untrusted content.** The Scout sees the payload wrapped `_untrusted`,
like every event payload (context.py). It holds only contract-owned strings
and code-chosen classes, so a hostile page cannot become a prompt
(tests/test_public_surface_contract.py,
tests/society/test_public_surface_monitor.py).

**Operator view.** `GET /v1/society/company` (operator) now includes
`public_surface`:

* where the monitor runs. The registry serves this view, but the monitor
  runs only in the society-worker, whose own `SOCIETY_PUBLIC_SURFACE_*`
  settings decide. So the view names the worker and its liveness signal
  (`society_public_surface_checks_total` on the worker's `/metrics`) and
  does not claim the worker's settings;
* the open anomaly and recent anomaly/recovery events;
* the Society's **workstream** on the newest anomaly: runs by role, and
  candidates with risk tier, QA and Security verdicts and promotions
  (PR, CI).

## 4. Repair, and what the Society may not do

The normal path is:

`public.surface.anomaly` → Scout (`CREATE_IMPROVEMENT` with evidence) →
Governor → Architect (`REQUEST_CODE_CHANGE`, acceptance tests from
`verification_tests`) → Builder → QA → Security → fitness → promotion.

* **The Builder edits real files.** A `SUBMIT_CODE_CANDIDATE` edit is
  either a whole file (`content`) or exact-text `replacements` in an
  existing file. Each `old` must occur exactly once, and all edits apply or
  none do (engineering/workspace.py). Only the changed text costs output
  tokens, so a real change to a large file fits the model's budget.
* **QA can import the dashboard.** The registry image, which the
  society-worker runs, pins the dashboard's runtime (`flask`, `requests`) at
  the dashboard's exact versions (tests/test_dashboard_runtime_parity.py).
* **The contract and its tests are evaluation criteria.** `risk.py`
  classifies `public_surface_contract.json`, `surface.py` and
  `services/dashboard/tests/test_public_surface.py` as **RED**. A candidate
  that edits them cannot auto-merge and needs owner approval. QA's
  no-self-judging rule also refuses a candidate that edits its own
  acceptance tests. Changing a route and the expectation that judges it in
  one autonomous change is evaluation laundering, and it is refused.
* **Dashboard Python is AMBER.** A repair to `services/dashboard/app/*.py`
  never auto-merges: the Society opens the PR, and the owner approves and
  merges it. Templates and static files alone are GREEN.
* **Future changes to the product.** A deliberate product change that
  retires a page changes the contract in its own reviewed change, not
  inside a repair.

The Society may never:

* deploy production;
* change Cloudflare, billing or GitHub rules;
* raise its own budgets;
* edit constitutional gates;
* approve its own RED work.

A production anomaly flows like this:

1. staging sees trusted evidence;
2. a Society candidate is made;
3. it lands on main and staging, GREEN or owner-approved;
4. the trusted operator runs a production release (`deploy/production/release.py`);
5. the owner releases.
