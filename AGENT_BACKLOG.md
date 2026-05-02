# AgentNet -- Agent Backlog

> Source of truth for what the agent fleet should build next.
> Format: YAML block. Planner reads this, picks first item with `status: dispatched` or `status: todo`,
> dispatches to Builder. After QA passes, status -> `done`.
> User can edit this file directly to add/reprioritize tasks.
>
> Acceptance criteria are EXECUTABLE BASH ONE-LINERS run from /opt/agentnet/.
> Available env vars in QA context: $TOKEN (user JWT), $AGENT_ID (hermes-brain id),
> $OPENCLAW_ID (openclaw-workhorse id).

```yaml
backlog:
- id: AB-409
  title: Redesign AgentNet dashboard UI — dark theme, agent activity timeline, mobile-responsive
  priority: high
  status: done
  shipped_at: '2026-04-27T03:40:00Z'
  files_to_modify:
  - services/dashboard/app/static/css/dark.css
  - services/dashboard/app/static/css/timeline.css
  - services/dashboard/app/templates/base.html
  description: Redesign AgentNet dashboard UI with dark theme, real-time agent activity timeline, and
    mobile responsiveness. Define CSS variables for dark theme, add timeline.css with vertical timeline
    styles, add media queries for 768px/1024px breakpoints, hamburger menu on mobile. Add timeline-root
    placeholder to index page.
  acceptance:
  - curl -s http://127.0.0.1:8000/ | grep -q '<html.*class="dark-theme"'
  - curl -s http://127.0.0.1:8000/ | grep -q 'timeline-container'
  - test -f services/dashboard/app/static/css/timeline.css
- id: AB-410
  title: Add public agent marketplace landing page with search + filter
  priority: high
  status: done
  shipped_at: '2026-04-27T19:24:54Z'
  files_to_modify:
  - services/dashboard/app/templates/marketplace.html
  - services/dashboard/app/main.py
  - services/dashboard/app/api_client.py
  description: 'Add a public agent marketplace landing page at /marketplace. The page displays all registered
    agents in a responsive grid layout. Each agent card shows name, capabilities, rating (stars or numeric),
    and price (if set). Features: a search bar at top that filters agent names/capabilities via query
    parameter "search", a filter dropdown for capability/category (query param "category"), and sort controls
    for rating or price (query params "sort" and "order"). The page reads from the existing GET /v1/agents
    endpoint (in registry service) which already supports query parameters "search", "category", "sort",
    "order". Modify the dashboard"s main.py to add a GET /marketplace route that calls api_client.fetch_agents(query_params)
    and renders the marketplace template. Update api_client.py with a fetch_agents method that calls the
    registry API. Overwrite the existing marketplace.html template with a new design using responsive
    CSS grid the same as dark.css theme. Use JavaScript for dynamic search/filter without page reload,
    or keep it server-side for simplicity. Ensure the page is accessible without authentication (public).
    Mobile-responsive: must work on 320px-768px.'
  acceptance:
  - curl -s http://127.0.0.1:8080/marketplace | grep -q 'search-bar'
  - curl -s 'http://127.0.0.1:8080/marketplace?search=echo&category=utility' | grep -q 'Echo Agent'
  - curl -s 'http://127.0.0.1:8080/marketplace?sort=rating&order=desc' | grep -q 'grid-layout'
  status: migrated-to-paperclip
  thread_id: ebcfae15-17c2-4bbe-8076-20227a3c4c69
  qa_feedback: null
  retries: 3
  blocked_by: done
- id: AB-411
  title: Build agent referral leaderboard — top agents by task count/success rate
  priority: medium
  files_to_modify:
  - services/registry/app/api/routes/stats.py
  - services/dashboard/app/templates/leaderboard.html
  - services/dashboard/app/main.py
  description: 'Add a public leaderboard page at /leaderboard served by the dashboard service, and a new
    API endpoint /api/v1/leaderboard in the registry service (stats.py). The endpoint aggregates data
    from the tasks table: for each agent, compute total tasks (count), completed tasks (status="completed"),
    success rate = (completed / total)*100, and total earnings from task rewards. Accept query parameter
    sort_by with values "tasks", "success_rate", "earnings". Return a JSON array sorted descending. Limit
    to top 50 agents. Dashboard template leaderboard.html renders a table with all 50 entries. Each agent
    name links to /agents/{agent_id}. Mobile-responsive: table scrolls horizontally on small screens.'
  acceptance:
  - curl -s http://127.0.0.1:8000/api/v1/leaderboard?sort_by=tasks | python3 -c "import sys,json; d=json.load(sys.stdin);
    assert len(d)>0; print('OK')"
  - curl -s http://127.0.0.1:8080/leaderboard | grep -q 'Leaderboard'
  status: migrated-to-paperclip
  thread_id: 2247291d-2007-4f89-a6a9-e26be6877015
  qa_feedback: null
  retries: 3
  blocked_by: qa-failed-3x
- id: AB-412
  title: Add live task execution timeline with WebSocket streaming to dashboard
  priority: medium
  files_to_modify:
  - services/registry/app/api/routes/websocket.py
  - services/dashboard/app/templates/tasks.html
  - services/dashboard/app/static/js/werewolf.js
  description: 'Add a live task execution timeline to the dashboard that displays tasks moving through
    states: created -> enriched -> dispatched -> in_progress -> review -> done/failed. The registry service
    exposes a WebSocket endpoint at /ws/tasks/timeline that pushes task state change events. Each event
    includes: task_id, agent_name, escrow_amount, current_state, previous_state, duration (seconds since
    creation), and timestamp. The dashboard"s tasks.html template should include a new timeline container
    (<div id="task-timeline">) with auto-scroll to latest entry. The werewolf.js file should connect to
    the WebSocket, parse incoming JSON, and render each task as a row showing: agent name, current state
    (with color coding), duration, and escrow amount. The timeline should show the last 50 tasks and auto-scroll
    to the newest. Mobile-responsive: timeline should stack vertically on mobile.'
  acceptance:
  - 'curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/ws/tasks/timeline
    | grep 101'
  - grep -q 'ws://127.0.0.1:8000/ws/tasks/timeline' services/dashboard/app/static/js/werewolf.js
  - grep -q 'timeline' services/dashboard/app/templates/tasks.html
  status: migrated-to-paperclip
  thread_id: c7b5a48d-5097-491f-8796-9a74922559ce
  qa_feedback: null
  retries: 3
  blocked_by: qa-failed-3x
- id: AB-413
  title: Implement agent auto-scaling — spawn worker agent on high backlog load
  priority: low
  files_to_modify:
  - services/registry/app/auto_scaler.py
  - services/registry/app/main.py
  - services/registry/requirements.txt
  description: Implement auto-scaling for Builder agents. Create a new module auto_scaler.py that periodically
    (every 60 seconds) queries the task queue depth. If backlog exceeds 50 and Builder is busy, spawn
    a new Docker container via python docker SDK. When backlog drops below 20, stop the spawned container
    and clean up. Low priority — skip if other items pending.
  acceptance:
  - curl -s http://127.0.0.1:8000/agents | python3 -c "import sys,json; data=json.load(sys.stdin); agents=[a
    for a in data if a.get('agent_type')=='builder-scaling']; print(len(agents))" | grep -q '0'
  status: migrated-to-paperclip
  thread_id: 8ade54e9-6e45-4c07-ba84-eb540cba251a
  qa_feedback: null
  retries: 3
  blocked_by: qa-failed-3x
- id: AB-414
  title: Add werewolf game spectator mode with mobile-responsive layout
  priority: medium
  files_to_modify:
  - services/dashboard/app/templates/werewolf_arena.html
  - services/dashboard/app/main.py
  - services/dashboard/app/static/css/werewolf.css
  description: 'Add a spectator mode to the Werewolf game page. When a user visits /werewolf/spectate,
    they see the game state (players, roles revealed on death, day/night cycle, vote history) without
    being a player. Mobile-responsive: layout stacks vertically on screens <768px, game board scales to
    fit viewport. Add spectator-specific CSS in werewolf.css.'
  acceptance:
  - curl -s http://127.0.0.1:8000/werewolf/spectate | grep -q 'spectator'
  status: migrated-to-paperclip
  thread_id: 846abc5e-c84e-41ea-b853-ca2d97490630
  qa_feedback: null
  retries: 3
  blocked_by: qa-failed-3x
```

## How to add a new task

1. Edit this file (commit to git or just save).
2. Append a new entry under `backlog:` with a unique `id` (next: AB-009, AB-010, ...).
3. Set `status: migrated-to-paperclip` and at least one `acceptance` criterion as an executable bash one-liner.
4. Planner picks it up next cycle (every ~30s).

## Manual override

To pause an agent: set `status: migrated-to-paperclip` and add `blocked_by: manual-hold`.
To force-rerun a "done" task: change `status` back to `open` (creates a new commit).

- id: PAP-5-METAVERSE-1
  title: '[METAVERSE] Three.js 3D agent space — WebGL scene + orbit controls + grid floor'
  priority: high
  status: done
  description: |
    Tạo trang /metaverse trong dashboard service. Dùng Three.js CDN tạo scene với:
    - Grid floor (10x10) làm mặt sàn ảo
    - OrbitControls cho phép xoay/zoom/pan
    - Ambient light + directional light
    - Camera bắt đầu góc nhìn từ trên cao
    - Responsive canvas fill viewport
  acceptance:
    - 'curl -s http://127.0.0.1:8080/metaverse | grep -q "three.js"'
    - 'curl -s http://127.0.0.1:8080/metaverse | grep -q "OrbitControls"'

- id: PAP-5-METAVERSE-2
  title: '[METAVERSE] Agent avatar spheres — render agents as 3D objects with labels'
  priority: high
  status: dispatched
  description: |
    Fetch agents từ API /v1/agents, render mỗi agent thành sphere 3D trên grid:
    - Sphere màu random theo agent type
    - Label tên agent floating trên sphere (CSS2DRenderer hoặc sprite)
    - Position ngẫu nhiên trên grid floor
    - Sphere animation nhẹ (pulse scale)
  acceptance:
    - 'curl -s http://127.0.0.1:8080/metaverse | grep -q "MeshStandardMaterial"'
    - 'curl -s http://127.0.0.1:8080/metaverse | grep -q "agent.position"'

- id: PAP-5-METAVERSE-3
  title: '[METAVERSE] Real-time WebSocket sync — agent positions update live'
  priority: medium
  status: dispatched
  description: |
    Kết nối WebSocket tới AgentNet registry /ws để nhận agent activity:
    - Khi agent online/offline → sphere chuyển màu (xanh/đỏ)
    - Task execution → particle effect + movement animation
    - Reconnect logic nếu WS disconnect
    - Hiển thị event feed ở sidebar
  acceptance:
    - 'curl -s http://127.0.0.1:8080/metaverse | grep -q "new WebSocket"'
    - 'curl -s http://127.0.0.1:8080/metaverse | grep -q "agent-activity"'

- id: PAP-6-MARKET-1
  title: '[MARKETPLACE] Public landing page — hero, features grid, pricing tiers, CTA'
  priority: high
  status: dispatched
  description: |
    Trang /public-landing cho AgentNet marketplace:
    - Hero section với animated gradient + tagline "AI Agents for Hire"
    - Features grid 3 cột (Discover, Hire, Pay) với icons
    - Pricing tiers: Free, Pro ($10/mo), Enterprise ($50/mo)
    - CTA button "Get Started" → /register
    - Footer với links
    - Responsive (mobile stack)
  acceptance:
    - 'curl -s http://127.0.0.1:8080/public-landing | grep -c "pricing-tier" | grep "3"'
    - 'curl -s http://127.0.0.1:8080/public-landing | grep -q "hero-gradient"'

- id: PAP-6-MARKET-2
  title: '[MARKETPLACE] Agent search + filter — discover agents with real-time filtering'
  priority: high
  status: dispatched
  description: |
    Nâng cấp trang /marketplace với real-time search:
    - Search bar filter agent name + capabilities
    - Category dropdown (utility, creative, devops, gaming)
    - Sort by rating, price, recent
    - Grid layout responsive (2 col mobile, 3 col tablet, 4 col desktop)
    - Fetch từ /v1/agents với query params
  acceptance:
    - 'curl -s "http://127.0.0.1:8080/marketplace?search=echo" | grep -q "Echo Agent"'
    - 'curl -s "http://127.0.0.1:8080/marketplace?category=utility" | grep -q "grid-layout"'

- id: PAP-6-MARKET-3
  title: '[MARKETPLACE] Agent registration wizard — multi-step form for public users'
  priority: medium
  status: dispatched
  description: |
    Multi-step wizard form /agent/register:
    - Step 1: Basic info (name, type, endpoint)
    - Step 2: Capabilities selector (checkboxes)
    - Step 3: Pricing (credits per task)
    - Step 4: Review + Submit → POST /v1/agents
    - Progress indicator, back/next buttons, validation per step
    - No auth required (public users can register agents)
  acceptance:
    - 'curl -s http://127.0.0.1:8080/agent/register | grep -c "form-step" | grep "4"'
    - 'curl -s http://127.0.0.1:8080/agent/register | grep -q "wizard-progress"'

- id: AB-415
  title: '[APP] Provisioning Catalog — service discovery endpoint for agents'
  priority: high
  status: dispatched
  description: |
    Build a provisioning catalog API mirroring Stripe Projects' `stripe projects catalog`.
    - GET /v1/catalog → JSON array of provisionable services (domain, hosting, storage, db, API keys...)
    - Each entry: {provider, service, description, pricing, regions, tiers, required_params}
    - Provider registry: Cloudflare, Vultr, GitHub, HuggingFace, etc.
    - Agents query this to discover what they can auto-provision
    - DB table: provisioning_services + provisioning_providers
  research_query: "Stripe Projects catalog API format JSON schema service discovery 2026"

- id: AB-416
  title: '[APP] Scoped API Token — per-resource, per-agent credentials with limits'
  priority: high
  status: dispatched
  description: |
    Scoped API tokens system — giống Stripe Shared Payment Token + Cloudflare scoped token.
    - POST /v1/tokens → create token scoped to specific resource+agent, with: spending_cap, expiry, allowed_actions
    - Token chỉ có quyền trên resource được cấp (domain X, bucket Y), không full account
    - DB: scoped_tokens table (token_hash, agent_id, resource_type, resource_id, caps, expires_at)
    - Integration với escrow system: khi agent dùng token, check cap trước khi trừ wallet
  research_query: "Cloudflare scoped API token architecture per-resource permissions 2026"

- id: AB-417
  title: '[APP] Projects API — group resources into persistent projects for agents'
  priority: medium
  status: dispatched
  description: |
    Projects concept — mirroring Stripe Projects' state.json.
    - POST /v1/projects → create project (name, agent_id)
    - GET /v1/projects/{id} → list resources in project + their status
    - POST /v1/projects/{id}/resources → add resource to project
    - DB: projects table + project_resources table
    - Project state exportable as JSON (state.json equivalent) cho CI/CD
  research_query: "Stripe Projects state.json schema resource grouping best practices"

- id: AB-418
  title: '[APP] Platform Orchestrator API — third-party provisioning endpoint'
  priority: high
  status: dispatched
  description: |
    Orchestrator API — cho phép platform bên thứ 3 (như Stripe, Vercel, Netlify) gọi AgentNet
    để provision account + resources cho user của họ. Mirror Cloudflare's "one API call to
    provision a new Cloudflare account".
    - POST /v1/orchestrator/provision → nhận user identity từ platform → tạo AgentNet account
      + project + resources → trả về scoped API token
    - OAuth2 flow: platform redirect user → AgentNet authorize → callback với token
    - Partner registry: platform đăng ký làm orchestrator, được cấp client_id + client_secret
    - Webhook events: resource.created, resource.deleted, token.expired
  research_query: "Cloudflare agent provisioning API orchestrator partner integration OAuth 2026"
- id: QA-TEST-001
  title: 'QA Pipeline Test — add health endpoint comment to README'
  priority: low
  status: done
  description: |
    SIMPLE TEST: Add a comment line "# Health endpoint: GET /health" to README.md.
    This is a pipeline test — Planner should enrich + dispatch, Builder should codegen + commit, QA should verify.
  acceptance:
    - 'grep -q "Health endpoint: GET /health" /opt/agentnet/README.md && echo "PASS" || echo "FAIL"'

