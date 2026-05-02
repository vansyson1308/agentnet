# Paperclip + AgentNet Integration Plan

**Date:** 2026-04-27
**Status:** Draft for Andrew review

---

## 1. Why Paperclip

Paperclip (59k⭐, MIT) solves exactly what AgentNet is struggling with:

| Problem | Paperclip Solution |
|---------|-------------------|
| YAML backlog fragile, sequential | DB-backed issues with atomic checkout, retry, delegation |
| Agents self-orchestrate (loose) | Server heartbeat engine controls lifecycle |
| No org hierarchy (flat fleet) | Org chart with `reports_to`, roles, SVG visualization |
| No budget control | Budget policies with hard-stop, auto-pause |
| No plugin system | Adapter pattern + plugin SDK |
| Flask dashboard basic | React UI with full dashboard |

**Key insight:** Paperclip is a *control plane* for agent companies. AgentNet is an *execution plane* (escrow, wallets, tracing, A2A cards). They are **complementary**, not competitive.

---

## 2. Architecture — Dual Layer

```
┌─────────────────────────────────────────────────┐
│               PAPERCLIP (Control Plane)          │
│                                                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────┐ │
│  │  Issues  │ │Heartbeat │ │  Budget  │ │ Org  │ │
│  │  (Tasks) │ │Engine    │ │  Control │ │Chart │ │
│  └──────────┘ └──────────┘ └──────────┘ └──────┘ │
│                                                   │
│  ┌──────────────────────────────────────────┐     │
│  │         AgentNet Adapter (agentnet_http)  │     │
│  │  Translates heartbeat → AgentNet task     │     │
│  └──────────────────────────────────────────┘     │
├─────────────────────────────────────────────────┤
│           AGENTNET (Execution Plane)              │
│                                                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────┐ │
│  │ Registry │ │ Payment  │ │ Workers  │ │A2A   │ │
│  │ API      │ │Escrow/   │ │(Planner, │ │Cards │ │
│  │ (agents) │ │Wallets   │ │ Builder, │ │      │ │
│  │          │ │          │ │ QA, etc) │ │      │ │
│  └──────────┘ └──────────┘ └──────────┘ └──────┘ │
│                                                   │
│  Postgres DB │ Redis Pub/Sub │ Jaeger Tracing     │
└─────────────────────────────────────────────────┘
```

---

## 3. What Gets REPLACED

### Backlog YAML → Paperclip Issues API
- `AGENT_BACKLOG.md` → Paperclip issues (DB-backed, status: backlog/todo/in_progress/in_review/done/cancelled)
- Planner v5 stops reading YAML file, reads from Paperclip API instead
- Acceptance criteria stored as issue metadata (JSON field)

### Agent Process Loops → Paperclip Heartbeat
- Instead of Planner v5 running as a Python cron loop:
  - Paperclip heartbeat engine fires wakeup requests
  - AgentNet adapter receives heartbeat → creates/updates issue
  - AgentNet agents (builder, QA, storyteller) register as Paperclip employees
  - Paperclip tracks: PID, log streaming, cancellation, timeout

### Flask Dashboard → Paperclip React UI (optional, keep both in migration)
- Paperclip's UI is production-ready: org chart, issue board, budget dashboard, agent config
- AgentNet Flask dashboard can remain for wallet/tracing details
- Or we build a wallet plugin for Paperclip UI

---

## 4. What Gets KEPT (AgentNet strengths)

### Escrow/Wallet System
Paperclip has NO payment layer — only cost tracking. AgentNet's escrow model is more advanced.

### Registry API + A2A Cards
AgentNet's agent discovery + A2A standard is valuable. External agents register here.

### Jaeger Tracing
Paperclip has run logs but no distributed tracing. Keep Jaeger for debugging.

### Payment Service
AgentNet's dual-currency wallet (credits + USDC), transactions, approval workflows.

---

## 5. Adapter Design — `agentnet_http`

Paperclip already has an `http` adapter type (`server/src/adapters/http/index.ts`). But we need a custom adapter that understands AgentNet's task model.

### ServerAdapterModule interface (from Paperclip adapter-plugin.md):

```typescript
type ServerAdapterModule = {
  type: string;              // "agentnet_http"
  execute: (ctx) => Promise<AdapterExecutionResult>;  // Main invocation
  testEnvironment?: (ctx) => Promise<void>;
  listSkills?: (ctx) => Promise<Skill[]>;
  syncSkills?: (ctx) => Promise<void>;
  sessionCodec?: AdapterSessionCodec;
  models?: Model[];
  agentConfigurationDoc?: string;  // Config UI schema
};
```

### Flow:

1. **Paperclip Heartbeat** → selects agent with type `agentnet_http`
2. **Adapter `execute()`** → sends HTTP POST to AgentNet Worker (port 8003)
3. **AgentNet Worker** → creates task in registry DB, dispatches to agent process
4. **Agent completes** → reports back via callback `POST /api/agentnet/task-complete`
5. **Paperclip** → updates issue status, logs cost events

### AgentNet Side — New Service: `agentnet-paperclip-worker`

A lightweight FastAPI service (port 8003) that:
- Receives Paperclip heartbeat → translates to AgentNet task
- Maps org chart → agent permissions
- Reports cost back to Paperclip
- Handles cancellation/timeout signals

---

## 6. Data Mapping

| Paperclip | AgentNet | Notes |
|-----------|----------|-------|
| Company | Workspace/Namespace | AgentNet has no company model yet |
| Issue | Task (backlog item) | Status: backlog→todo→in_progress→in_review→done |
| Agent | Agent (DB record) | Paperclip agent = AgentNet agent with `adapter_type: agentnet_http` |
| Skill | Capability | Paperclip skills ↔ AgentNet capabilities |
| Cost Event | Transaction | Dual reporting: Paperclip for budget, AgentNet for escrow |
| Budget Policy | n/a | New to AgentNet — no existing equivalent |
| Org Chart | Agent roles | AgentNet flat → Paperclip hierarchical |

---

## 7. Deployment Plan

### Phase 1 (Week 1) — MVP Integration
1. Deploy Paperclip server on VPS (Docker, separate port 3100)
2. Write `agentnet_http` adapter package
3. Create `agentnet-paperclip-worker` service (port 8003)
4. Migrate AB-410 issue from YAML → Paperclip API
5. Planner v5 reads from Paperclip instead of YAML

### Phase 2 (Week 2) — Full Pipeline
1. Register builder, QA, storyteller as Paperclip employees
2. Map org chart: Planner → PM role, Builder → Engineer, QA → QA
3. Budget policy per agent
4. Heartbeat scheduling for daily tasks (storyteller)

### Phase 3 (Week 3-4) — Polish
1. Migrate remaining backlog items (AB-411 → AB-414)
2. Wallet plugin for Paperclip UI
3. Dual dashboard (keep Flask + Paperclip UI)
4. A2A card → Paperclip skill sync

---

## 8. Quick Feasibility Check

**Prerequisites already on VPS:**
- ✅ Node.js 20+ (`node -v` → v22)
- ✅ pnpm available (check)
- ✅ Docker running
- ✅ Postgres available (can share DB or create separate)

**Paperclip server starts with:**
```bash
pnpm install
pnpm dev
# API: http://localhost:3100
# UI: http://localhost:3100 (served by API)
```

**Risk:** Paperclip uses PGlite by default (embedded). For production, needs `DATABASE_URL` pointing to existing Postgres — need to check schema compatibility.

---

## 9. Recommendation

**Start with Phase 1:**
- Deploy Paperclip server (takes 1 hour)
- Write agentnet_http adapter (takes 1-2 days)
- Point Planner v5 at Paperclip API (takes 1 day)
- THIS FIXES the current deadlock immediately

**Then iterate.**
