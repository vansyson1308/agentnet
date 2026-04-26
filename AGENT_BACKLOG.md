# AgentNet -- Agent Backlog

> Source of truth for what the agent fleet should build next.
> Format: YAML block. Planner reads this, picks first `status: open` item,
> dispatches to Builder. After QA passes, status -> `done`.
> User can edit this file directly to add/reprioritize tasks.
>
> Acceptance criteria are EXECUTABLE BASH ONE-LINERS run from /opt/agentnet/.
> Available env vars in QA context: $TOKEN (user JWT), $AGENT_ID (hermes-brain id),
> $OPENCLAW_ID (openclaw-workhorse id).

```yaml
backlog:
- id: AB-001
  title: Add /v1/agents/{id}/capabilities endpoint
  priority: high
  files_to_modify:
  - services/registry/app/api/routes/agents.py
  description: 'Add a dedicated GET /v1/agents/{agent_id}/capabilities endpoint that

    returns just the capabilities array. Useful for clients doing

    capability discovery without fetching full agent metadata.


    Implementation hints:

    - Add new route in routes/agents.py (after the existing GET /{agent_id} route).

    - Use the existing get_db dependency.

    - Query the Agent model and return its capabilities field.

    - Return 404 if agent not found.

    - The endpoint MUST be unauthenticated (read-only public info) to match other discovery endpoints.

    '
  acceptance:
  - test "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/v1/agents/$AGENT_ID/capabilities)"
    = "200"
  - curl -s http://127.0.0.1:8000/v1/agents/$AGENT_ID/capabilities | python3 -c "import sys, json; d =
    json.load(sys.stdin); assert isinstance(d, list) and len(d) > 0, f\"got {d}\""
  status: done
  thread_id: 60d49688-55a0-49a7-8aef-bd4090f06225
  retries: 1
  shipped_at: '2026-04-25T10:19:03Z'
- id: AB-002
  title: 'CORS: allow trycloudflare.com origin pattern'
  priority: high
  files_to_modify:
  - services/registry/app/main.py
  description: 'Production CORS list misses cloudflare tunnel hostnames. Update the

    CORS middleware to include allow_origin_regex matching r"https://.*\.trycloudflare\.com$".


    Find the existing add_middleware(CORSMiddleware, ...) call and add

    allow_origin_regex parameter alongside allow_origins. Keep all other

    params (allow_credentials, allow_methods, allow_headers) intact.

    '
  acceptance:
  - grep -q "trycloudflare" services/registry/app/main.py
  - grep -q "allow_origin_regex" services/registry/app/main.py
  status: done
  thread_id: 707918bd-0e74-4cfb-befc-13d591d6ca58
  retries: 1
  shipped_at: '2026-04-25T10:20:35Z'
- id: AB-003
  title: 'Dashboard: dark mode toggle (CSS skeleton + button)'
  priority: low
  files_to_modify:
  - services/dashboard/app/static/css/dark.css
  - services/dashboard/app/templates/base.html
  description: 'Add a theme toggle button to the dashboard navbar. Provide

    services/dashboard/app/static/css/dark.css with overrides for

    body/card/text colors when class ''theme-dark'' is on body.

    Add a small inline JS snippet in base.html that toggles the class

    on click and persists choice to localStorage.


    Keep changes minimal -- do not refactor unrelated styles.

    '
  acceptance:
  - test -s services/dashboard/app/static/css/dark.css
  - grep -q "theme-toggle" services/dashboard/app/templates/base.html
  - grep -q "theme-dark" services/dashboard/app/static/css/dark.css
  status: done
  thread_id: b12226af-07be-4789-b4af-2d9fc00ef16e
  retries: 4
  blocked_by: qa-failed-4x
  shipped_at: '2026-04-25T10:23:37Z'
- id: AB-004
  title: Add /v1/stats/by-capability summary endpoint
  priority: medium
  files_to_modify:
  - services/registry/app/api/routes/stats.py
  description: 'Add an endpoint that aggregates task volume per capability across

    the workplace. Returns array of objects with shape:

    [{capability: str, total_tasks: int, completed_count: int}].


    Use existing Task model + group by capability. Sort descending by total_tasks.

    Mount the new route at GET /v1/stats/by-capability.

    '
  acceptance:
  - 'test "$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/stats/by-capability)"
    = "200"'
  - 'curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/stats/by-capability | python3
    -c "import sys, json; d = json.load(sys.stdin); assert isinstance(d, list), f\"got {type(d)}\""'
  status: done
  thread_id: a498aebe-6648-42c8-98ce-3b79121cbf47
  retries: 1
  shipped_at: '2026-04-25T10:24:38Z'
- id: AB-005
  title: Add ROADMAP.md to repo root
  priority: low
  files_to_modify:
  - ROADMAP.md
  description: 'Create a top-level ROADMAP.md describing AgentNet''s near-term direction:

    - Q2 2026 focus: agent reputation system, on-chain settlement extension.

    - Q3 2026 focus: marketplace UI for human users, capability search.

    Use clear markdown with H1 title, two H2 sections, bullet points.

    '
  acceptance:
  - test -s ROADMAP.md
  - grep -q "^# " ROADMAP.md
  - grep -qE "Q2 2026|Q3 2026" ROADMAP.md
  status: done
  thread_id: bb9f1511-1dec-42ca-87ef-7fa1a8d77f8b
  shipped_at: '2026-04-25T10:25:38Z'
- id: AB-006
  title: Add /v1/health/deep with subsystem checks
  priority: medium
  files_to_modify:
  - services/registry/app/api/routes/health.py
  - services/registry/app/api/routes/__init__.py
  description: 'Current /health is shallow. Add a new health.py routes file with GET /v1/health/deep that
    returns: { "ok": bool, "postgres": {"ok": bool, "latency_ms": float}, "redis": {"ok": bool, "latency_ms":
    float} }

    ok = postgres.ok && redis.ok. Use try/except around each subsystem check. Mount via routes/__init__.py. '
  acceptance:
  - test "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/v1/health/deep)" = "200"
  - 'curl -s http://127.0.0.1:8000/v1/health/deep | python3 -c "import sys, json; d = json.load(sys.stdin);
    assert all(k in d for k in (\"postgres\", \"redis\", \"ok\")), f\"keys missing: {list(d.keys())}\""'
  status: blocked
  thread_id: c16b5155-df4f-4792-82b9-27b03bd043a8
  retries: 6
  blocked_by: qa-failed-6x
- id: AB-007
  title: Daily ship-log story poster (storyteller v4)
  priority: medium
  files_to_modify:
  - hermes_storyteller_v4.py
  description: 'A new file (Python script) that, when run, reads /opt/agentnet/SHIP_LOG.md

    from the last 24 hours, formats a markdown story like:


    # Daily Progress -- 2026-04-25

    Shipped today (3): AB-001, AB-002, AB-006

    In progress (1): AB-004

    Blocked (0):


    Then POSTs to /v1/stories/ with title "Daily Progress -- <date>".


    Designed to be run by cron once per day. Uses /v1/auth/user/login.

    '
  acceptance:
  - test -s hermes_storyteller_v4.py
  - grep -q "Daily Progress" hermes_storyteller_v4.py
  - grep -q "SHIP_LOG.md" hermes_storyteller_v4.py
  status: done
  thread_id: da31c97e-889a-4a2e-987c-b46e8d33142e
  retries: 1
  shipped_at: '2026-04-25T10:37:40Z'
- id: AB-008
  title: 'README badge: agent fleet status'
  priority: low
  status: review
  files_to_modify:
  - README.md
  description: 'Add a simple status section near the top of README.md (after the title

    badges block) listing the active 24/7 agent services and their roles:


    ## Agent Fleet (24/7)

    - hermes-brain (DeepSeek V4) -- orchestrator via Telegram

    - openclaw-workhorse (DeepSeek V4 Flash) -- bulk research/fetch

    - hermes-planner-v4 -- backlog reader, dispatches to builder

    - hermes-builder-v6 -- DeepSeek codegen + git commit

    - hermes-qaagent-v6 -- acceptance test runner

    - hermes-storyteller-v3 -- daily ship-log narration

    '
  acceptance:
  - grep -q "Agent Fleet" README.md
  - grep -q "hermes-builder-v6" README.md
  thread_id: 8d06323f-d64f-4112-9139-c74f0069a1ee
- id: AB-100
  title: Add /v1/agents/leaderboard endpoint (top 5 by success rate)
  priority: high
  description: 'Add a public, unauthenticated GET endpoint /v1/agents/leaderboard that returns the top
    5 agents sorted by success_rate descending. Only agents with completed_tasks > 0 are included. The
    response is a JSON array of objects each containing: id (int), name (str), success_rate (float), completed_tasks
    (int). success_rate is computed as successful_tasks / completed_tasks (both fields exist on the Agent
    model). Use the existing Agent model from services/registry/app/models.py. The endpoint must be added
    to the agents router in services/registry/app/api/routes/agents.py. Implementation: define a new async
    function leaderboard() with @router.get(''/leaderboard''). Get a database session via Depends(get_db).
    Query Agent, filter where completed_tasks > 0, order by (successful_tasks / completed_tasks).desc(),
    limit(5). Return a list of dicts. Use result.scalars().all() to get agent objects, then build dicts.
    No authentication dependency. No rate limiting. Ensure the endpoint is registered (the router is included
    in app).'
  status: done
  files_to_modify:
  - services/registry/app/api/routes/agents.py
  acceptance:
  - curl -s http://127.0.0.1:8000/v1/agents/leaderboard | jq '. | length' | xargs test 0 -lt
  - curl -s http://127.0.0.1:8000/v1/agents/leaderboard | jq '.[].success_rate' | sort -r -n | head -1
    | xargs test $(curl -s http://127.0.0.1:8000/v1/agents/leaderboard | jq '.[0].success_rate') -eq
  - curl -s http://127.0.0.1:8000/v1/agents/leaderboard | jq '.[].completed_tasks' | grep -q '0' && exit
    1 || exit 0
  enriched_at: '2026-04-25T10:42:22Z'
  enriched_with_research: false
  thread_id: 2a3875f3-7c5c-4e9f-ac86-7e7e9d6da4cd
  shipped_at: '2026-04-25T10:44:42Z'
- id: AB-101
  title: Add agent reputation history tracking (best practice)
  priority: medium
  description: Add agent reputation history tracking by creating a new database migration (08-reputation-history.sql)
    with table agent_reputation_history (agent_id UUID references agents, snapshot_date DATE, reputation_tier
    TEXT, success_rate DOUBLE PRECISION, created_at TIMESTAMPTZ DEFAULT NOW()), unique constraint on (agent_id,
    snapshot_date). In reputation.py, add function record_reputation_snapshot(agent_id, tier, success_rate)
    that upserts a row for today's date using ON CONFLICT (agent_id, snapshot_date) DO UPDATE SET (reputation_tier,
    success_rate) = (excluded.*). In agents.py, add GET /agents/{agent_id}/reputation/history endpoint
    (authenticated) that returns a list of snapshot records for the agent ordered by snapshot_date DESC.
    Deploy the migration via the existing init-db mechanism (e.g., run 08-reputation-history.sql on startup).
    This follows best practice for time-series data by using a separate table with a date-based unique
    constraint, enabling efficient daily snapshots without duplicates.
  research_query: best practice storing time-series agent reputation history PostgreSQL 2026
  status: blocked
  enrich_failed_at: 1777115021.6147885
  files_to_modify:
  - services/registry/init-db/08-reputation-history.sql
  - services/registry/app/reputation.py
  - services/registry/app/api/routes/agents.py
  acceptance:
  - 'curl -s -o /dev/null -w ''%{http_code}'' -H ''Authorization: Bearer $TOKEN'' http://127.0.0.1:8000/agents/$AGENT_ID/reputation/history
    | grep -q 200'
  - 'curl -s -H ''Authorization: Bearer $TOKEN'' http://127.0.0.1:8000/agents/$AGENT_ID/reputation/history
    | grep -q ''\['''
  enriched_at: '2026-04-25T11:14:00Z'
  enriched_with_research: false
  thread_id: 0610d3dd-454c-4ec2-b61e-88d3d61fc344
  retries: 3
  blocked_by: qa-failed-3x
- id: AB-200
  title: Sua UI UX cua website AgentNet
  priority: high
  description: Refactor base.html to include a responsive navigation bar (navbar) with logo, user login
    button, and links to Dashboard, Directory, and Offers. Use a fixed-top navbar with light background
    and proper spacing. Update dark.css to define CSS variables for consistent theming, improve typography
    (system font stack), and add styles for cards (agent cards in directory), grid layout, and hover effects.
    Modify directory.html to wrap agent items in a grid container with card classes. This improves visual
    hierarchy, mobile responsiveness, and overall user experience without introducing a heavy framework.
  status: blocked
  files_to_modify:
  - services/dashboard/app/templates/base.html
  - services/dashboard/app/static/css/dark.css
  - services/dashboard/app/templates/directory.html
  acceptance:
  - curl -s http://127.0.0.1:8000 | grep -q '<nav class="navbar"'
  - curl -s http://127.0.0.1:8000 | grep -q '<main class="container"'
  - curl -s http://127.0.0.1:8000/directory | grep -q 'class="grid-container"'
  enriched_at: '2026-04-25T11:22:13Z'
  enriched_with_research: false
  thread_id: 621ea446-8a28-48bb-b33e-42ac0a9c8390
  retries: 3
  blocked_by: qa-failed-3x
- id: AB-201
  title: 'Upgrade agentnet.io.vn website: research + detailed plan'
  priority: high
  description: 'Create a detailed multi-phase plan document (UPGRADE_PLAN.md) for upgrading the agentnet.io.vn
    website. The plan is based on a full audit of the current React SPA (Vite+TS+Tailwind) served via
    Nginx, and the backend services (registry, payment, simulation, dashboard). It covers 6 phases:


    Phase 1: UX Audit & Quick Wins – inventory all existing pages (Dashboard, Agents, Offers, Chat, Network,
    Marketplace, Collaboration, Chronicle) and their components; list missing pages (pricing, docs, signup
    flow, agent directory search, task history) and propose adding them; identify low-hanging improvements
    like loading spinners, error boundaries, mobile responsiveness.


    Phase 2: Performance & Code Quality – implement lazy-loading, code splitting per route, bundle size
    optimization, caching strategy (Service Worker for static assets), and replace any deprecated dependencies.


    Phase 3: Marketing Landing Page – build a separate marketing landing page (e.g., /landing) with SEO-optimized
    content, meta tags, Open Graph, and structured data; consider a SSG or static export for that page.


    Phase 4: Integration Improvements – enhance WebSocket reconnection logic, API error boundaries with
    user-friendly fallbacks, and authentication flow consistency (e.g., token refresh, redirect after
    login).


    Phase 5: Blog/Docs Section – add a markdown-based blog or documentation sub-site (e.g., /blog, /docs)
    using React Router and a markdown renderer; maybe integrate with a headless CMS or just static markdown
    files.


    Phase 6: Monitoring & Analytics – integrate analytics (e.g., Plausible), performance monitoring (Lighthouse
    CI), and user feedback widgets. Each phase includes concrete acceptance criteria in the plan document.'
  research_query: best practices React dashboard UX upgrade 2026 microservices marketplace
  status: done
  files_to_modify:
  - UPGRADE_PLAN.md
  acceptance:
  - test -f UPGRADE_PLAN.md
  - 'grep -q ''Phase 1: UX Audit & Quick Wins'' UPGRADE_PLAN.md'
  - 'grep -q ''Phase 6: SEO & Documentation Site'' UPGRADE_PLAN.md'
  enriched_at: '2026-04-25T11:26:04Z'
  enriched_with_research: false
  thread_id: 9ab66829-b017-4a85-96bb-84b187f677b2
  shipped_at: '2026-04-25T11:32:07Z'
- id: AB-202
  title: 'Phase 1: UX Audit & Quick Wins'
  priority: high
  files_to_modify:
  - UPGRADE_PLAN.md
  description: Inventory all existing pages (Dashboard, Agents, Offers, Chat, Network, Marketplace, Collaboration,
    Chronicle) and their components. Identify missing pages (pricing, docs, signup flow, agent directory
    search, task history). Add loading spinners for async data, implement error boundaries with user-friendly
    fallbacks, improve mobile responsiveness (320px-768px), add missing meta tags for existing pages.
  acceptance:
  - test -f UPGRADE_PLAN.md
  - grep -q 'Phase 1' UPGRADE_PLAN.md
  status: done
  thread_id: 08ef2f9e-b8bb-4de7-ae89-7acf788f2bfc
  shipped_at: '2026-04-25T11:58:54Z'
- id: AB-203
  title: 'Phase 2: Performance & Code Quality'
  priority: high
  description: 'Add a service worker for cache-first static asset caching (CSS, JS, images) to improve
    performance and offline resilience. Create `services/dashboard/app/static/js/service-worker.js` that
    installs and activates, then intercepts fetch requests for static assets (e.g., /static/*) and serves
    cached responses with a cache-first strategy. Register the service worker in the <head> of `base.html`
    after checking `navigator.serviceWorker` availability. Audit `requirements.txt`: remove any deprecated
    or unused packages (e.g., if `deprecated-pkg` is present), update remaining packages to latest compatible
    versions (use `pip list --outdated` to identify, then bump versions in requirements.txt while preserving
    compatibility with the Python 3.9+ runtime). No React or Vite build is present in this repo, so code
    splitting and bundle analysis are not applicable. Instead, the performance focus is on caching and
    dependency hygiene.'
  status: done
  files_to_modify:
  - services/dashboard/app/static/js/service-worker.js
  - services/dashboard/app/templates/base.html
  - services/dashboard/requirements.txt
  acceptance:
  - test -f services/dashboard/app/static/js/service-worker.js
  - grep -q "navigator.serviceWorker.register" services/dashboard/app/templates/base.html
  - pip install -r services/dashboard/requirements.txt >/dev/null 2>&1
  enriched_at: '2026-04-25T11:56:02Z'
  enriched_with_research: false
  thread_id: 35f102ee-aa32-4938-8cb2-f6bde3e1055a
  shipped_at: '2026-04-25T12:00:12Z'
- id: AB-204
  title: 'Phase 3: Marketing Landing Page'
  priority: medium
  description: Build SEO-optimized marketing landing page at /landing as static export or standalone entry.
    Add meta tags, Open Graph tags, JSON-LD structured data. Ensure page can be served independently from
    main SPA (via Nginx or subdomain).
  status: done
  files_to_modify:
  - UPGRADE_PLAN.md
  acceptance:
  - grep -q 'Phase 3' UPGRADE_PLAN.md
  thread_id: bcf93805-5c81-43f3-bbc6-703c192dcad3
  shipped_at: '2026-04-25T12:01:42Z'
- id: AB-205
  title: 'Phase 4: Integration Improvements'
  priority: high
  description: Implement WebSocket exponential backoff reconnection with max retries. Wrap all API calls
    in unified error handler with toast notifications. Ensure seamless token refresh (401 → refresh →
    retry). Standardize loading/error/empty states across all data-fetching components.
  status: done
  files_to_modify:
  - UPGRADE_PLAN.md
  acceptance:
  - grep -q 'Phase 4' UPGRADE_PLAN.md
  thread_id: b6efd1c6-3f67-46a2-88f3-d2739e1b3447
  shipped_at: '2026-04-25T12:02:43Z'
- id: AB-206
  title: 'Phase 5: Blog/Docs Section'
  priority: medium
  description: Add React Router routes for /blog and /docs. Use react-markdown to render .md files from
    /content/ dir. Apply Tailwind prose classes. /blog lists posts with title/date/excerpt. /docs has
    sidebar nav. Syntax highlighting for code blocks.
  status: done
  files_to_modify:
  - UPGRADE_PLAN.md
  acceptance:
  - grep -q 'Phase 5' UPGRADE_PLAN.md
  thread_id: 033a0fa4-9f6e-4746-a5e6-069208a12e5d
  shipped_at: '2026-04-25T12:03:44Z'
- id: AB-207
  title: 'Phase 6: SEO & Documentation Site'
  priority: medium
  description: Integrate Plausible analytics for privacy-friendly tracking. Set up Lighthouse CI for performance
    regression detection. Add feedback widget on Dashboard, Agents, Marketplace. Ensure all public pages
    have meta tags, XML sitemap, robots.txt. Implement structured data (JSON-LD) for agents, reviews,
    pricing.
  status: done
  files_to_modify:
  - UPGRADE_PLAN.md
  acceptance:
  - grep -q 'Phase 6' UPGRADE_PLAN.md
  thread_id: 750d7129-07d7-40e5-8faa-1b44cfe3e393
  shipped_at: '2026-04-25T12:04:44Z'
- id: AB-300
  title: Mount RateLimitMiddleware in registry main.py
  priority: high
  files_to_modify:
  - services/registry/app/main.py
  description: 'Mount the existing RateLimitMiddleware (already imported from app.api.rate_limiter) onto
    the FastAPI app instance. Use defaults: 60 req/min/IP for general endpoints. Place app.add_middleware
    call AFTER CORSMiddleware setup. Do NOT modify any existing endpoints.'
  acceptance:
  - grep -q "add_middleware(RateLimitMiddleware" services/registry/app/main.py
  status: done
  thread_id: f5f3ce82-6d2a-4954-93fd-f195bfec98ca
  shipped_at: '2026-04-25T14:51:35Z'
- id: AB-301
  title: Mount RateLimitMiddleware in payment main.py
  priority: high
  files_to_modify:
  - services/payment/app/main.py
  description: Same pattern as AB-300 but for payment service. Import RateLimitMiddleware from .api.rate_limiter
    (or wherever it lives in payment service -- check existing imports). Mount with default 60 req/min/IP.
    If RateLimitMiddleware doesn't exist in payment, create a copy file payment/app/api/rate_limiter.py
    mirroring the registry one (in-memory token bucket).
  acceptance:
  - grep -qE "(RateLimit|add_middleware.*Rate)" services/payment/app/main.py
  status: done
  thread_id: 03c642db-b9b1-4372-8bec-fa80f84407cf
  shipped_at: '2026-04-25T14:52:35Z'
- id: AB-302
  title: Add audit_log table migration
  priority: high
  files_to_modify:
  - services/registry/init-db/02_audit_log.sql
  - services/registry/app/models.py
  description: 'Add a new audit_log table for security event tracking. Create migration file 02_audit_log.sql
    with: CREATE TABLE IF NOT EXISTS audit_log (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), actor_user_id
    uuid, actor_ip inet, action text NOT NULL, target_id text, payload_summary text, success boolean DEFAULT
    true, created_at timestamptz NOT NULL DEFAULT now()). Add index on (action, created_at). Also add
    corresponding SQLAlchemy model AuditLog in models.py.'
  acceptance:
  - test -f services/registry/init-db/02_audit_log.sql
  - grep -q "CREATE TABLE.*audit_log" services/registry/init-db/02_audit_log.sql
  - grep -q "class AuditLog" services/registry/app/models.py
  status: done
  thread_id: 568ed55a-1937-4b2b-890d-4ff75479fbc7
  shipped_at: '2026-04-25T14:54:06Z'
- id: AB-303
  title: Strip sensitive fields from public GET /v1/agents/{id}
  priority: medium
  files_to_modify:
  - services/registry/app/api/routes/agents.py
  description: 'The GET /v1/agents/{agent_id} endpoint currently returns the full agent record including
    endpoint URL and public_key. For info disclosure protection, when caller is unauthenticated (no Bearer
    token or invalid one), return a redacted version: omit `endpoint` and `public_key` fields. When caller
    IS authenticated, return full record. Implement using a helper function or response model variant.
    Do NOT change other agent endpoints.'
  acceptance:
  - grep -qE "(endpoint.*pop|exclude.*endpoint|RedactedAgent|public_key.*pop)" services/registry/app/api/routes/agents.py
  status: blocked
  thread_id: 8fe37b11-becd-4348-98bd-ca20fefc5471
  retries: 3
  blocked_by: qa-failed-3x
- id: AB-310
  title: Add password policy validator on user register
  priority: high
  files_to_modify:
  - services/registry/app/api/routes/auth.py
  description: 'In the POST /v1/auth/user/register endpoint, before creating the user, validate the password
    meets policy: at least 12 characters, contains at least one uppercase letter, one lowercase letter,
    one digit. If fails, raise HTTPException 400 with detail explaining which rule failed. Use a helper
    function _validate_password(pw: str) that returns None on pass or error message on fail.'
  acceptance:
  - grep -q "_validate_password\|password.*12\|len(.*password.*>= 12" services/registry/app/api/routes/auth.py
  status: done
  thread_id: 97d92f15-eff4-4074-ba55-05c988ee1fe0
  shipped_at: '2026-04-25T15:00:40Z'
- id: AB-311
  title: Add email_verification_tokens table
  priority: high
  files_to_modify:
  - services/registry/init-db/03_email_verification.sql
  description: 'Migration: create table email_verification_tokens (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid REFERENCES users(id) ON DELETE CASCADE, token text UNIQUE NOT NULL, expires_at timestamptz
    NOT NULL, consumed_at timestamptz, created_at timestamptz DEFAULT now()). Index on token. Also ALTER
    TABLE users ADD COLUMN IF NOT EXISTS is_email_verified boolean DEFAULT false.'
  acceptance:
  - test -f services/registry/init-db/03_email_verification.sql
  - grep -q "email_verification_tokens" services/registry/init-db/03_email_verification.sql
  - grep -q "is_email_verified" services/registry/init-db/03_email_verification.sql
  status: done
  thread_id: 723ba1c3-0d2e-4992-8d97-e39a13fb324c
  shipped_at: '2026-04-25T15:01:40Z'
- id: AB-312
  title: Add /v1/auth/verify-email endpoint
  priority: high
  files_to_modify:
  - services/registry/app/api/routes/auth.py
  description: 'Add a new endpoint GET /v1/auth/verify-email?token=XXX that: 1) looks up email_verification_tokens
    by token, 2) checks expires_at > now() AND consumed_at IS NULL, 3) sets users.is_email_verified=true
    for that user_id, 4) sets consumed_at=now() on the token, 5) returns {ok: true, message: ''verified''}
    on success or 400 on expired/invalid. Also add POST /v1/auth/resend-verification (requires email in
    body) that creates a new token and (TODO logs token to /var/log/agentnet-verify.log since SMTP not
    yet configured).'
  acceptance:
  - grep -qE "(verify-email|verify_email)" services/registry/app/api/routes/auth.py
  - grep -q "is_email_verified" services/registry/app/api/routes/auth.py
  status: done
  thread_id: fde58bf2-1699-4333-8ed1-241a44289492
  shipped_at: '2026-04-25T15:03:06Z'
- id: AB-320
  title: Add /marketplace public landing page on dashboard
  priority: medium
  files_to_modify:
  - services/dashboard/app/templates/marketplace.html
  - services/dashboard/app/main.py
  description: 'Create a public landing page route GET /marketplace on the dashboard service. The HTML
    template should: - explain what AgentNet is (1-paragraph), - show a signup form (email + password
    fields, ToS checkbox), - POST to /v1/auth/user/register on submit (via JS fetch), - on success show
    ''Check your email for verification link'', - include link to /docs (FastAPI auto OpenAPI). Use minimal
    inline CSS, no external frameworks. The route should NOT require auth.'
  acceptance:
  - test -f services/dashboard/app/templates/marketplace.html
  - grep -q "marketplace" services/dashboard/app/main.py
  - grep -q "AgentNet" services/dashboard/app/templates/marketplace.html
  status: done
  thread_id: 7774fca0-2177-4fa8-80ac-280c89b212b0
  retries: 1
  shipped_at: '2026-04-25T15:06:13Z'
```

## How to add a new task

1. Edit this file (commit to git or just save).
2. Append a new entry under `backlog:` with a unique `id` (next: AB-009, AB-010, ...).
3. Set `status: open` and at least one `acceptance` criterion as an executable bash one-liner.
4. Planner picks it up next cycle (every ~30s).

## Manual override

To pause an agent: set `status: blocked` and add `blocked_by: manual-hold`.
To force-rerun a "done" task: change `status` back to `open` (creates a new commit).
