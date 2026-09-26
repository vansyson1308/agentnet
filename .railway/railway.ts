/**
 * AgentNet — Railway staging topology (Infrastructure as Code).
 *
 * Applied with the Railway CLI from a linked checkout:
 *   railway link --project AgentNet --environment staging
 *   railway config plan      # preview
 *   railway config apply     # apply after confirmation
 *
 * Scope: the STAGING environment only. Applying to any other environment is
 * refused below — Phase 4 deploys no production. No secret value appears
 * here: JWT_SECRET_KEY, FLASK_SECRET_KEY and INTERNAL_WORKER_TOKEN are
 * shared variables the operator creates once from stdin
 * (`openssl rand -hex 32 | railway variable set JWT_SECRET_KEY --stdin`).
 * Managed Postgres/Redis credentials are Railway reference variables.
 *
 * Not expressible here (set once in the dashboard/CLI after apply, see
 * docs/RAILWAY_STAGING.md): generated public domains, Wait for CI, watch
 * paths, restart policy (ALWAYS where the plan allows it), the pre-deploy
 * timeout, and marking Postgres/Redis "no public networking" (the default).
 */
import { defineRailway, github, group, postgres, project, redis, service, volume } from "railway/iac";

const REPO = "vansyson1308/agentnet";
const BRANCH = "main";

export default defineRailway((ctx) => {
  if (ctx.environment !== "staging") {
    throw new Error(
      `AgentNet IaC targets the "staging" environment only (linked: "${ctx.environment}"). ` +
        "Production is not deployed in Phase 4 — link the staging environment and re-run.",
    );
  }

  const db = postgres("postgres");
  const cache = redis("redis");

  // AgentNet's configuration contract (POSTGRES_*/REDIS_*) wired to the managed
  // databases through reference variables; databases stay private (no TCP proxy).
  const dbEnv = {
    POSTGRES_HOST: db.env.PGHOST,
    POSTGRES_PORT: db.env.PGPORT,
    POSTGRES_USER: db.env.PGUSER,
    POSTGRES_PASSWORD: db.env.PGPASSWORD,
    POSTGRES_DB: db.env.PGDATABASE,
  };
  const redisEnv = {
    REDIS_HOST: cache.env.REDISHOST,
    REDIS_PORT: cache.env.REDISPORT,
    REDIS_PASSWORD: cache.env.REDISPASSWORD,
  };
  const common = {
    ENVIRONMENT: "staging",
    JAEGER_ENABLED: "false",
    // FORWARDED_ALLOW_IPS is deliberately NOT set: "*" would make uvicorn
    // honour the client-controlled leftmost X-Forwarded-For entry (rate-limit
    // bypass). The registry trusts X-Real-IP instead (TRUST_X_REAL_IP below;
    // ADR-0006 D5 has the boundary justification and the spoof test).
  };
  const societyOff = {
    SOCIETY_RUNTIME_ENABLED: "false",
    SOCIETY_AUTONOMOUS_CODE_ENABLED: "false",
    SOCIETY_STAGING_DEPLOY_ENABLED: "false",
    SOCIETY_PROMOTION_PROVIDER: "disabled",
    SOCIETY_GITHUB_CREDENTIAL_PROVIDER: "disabled",
    SOCIETY_AUTO_MERGE_ENABLED: "false",
    SOCIETY_DEPLOYMENT_PROVIDER: "disabled",
    SOCIETY_MODEL_PROVIDER: "scripted",
  };

  // A2A 1.0 (ADR-0009): the inbound server and the operator federation catalog
  // are product features and run on staging. The Society's use of them (the
  // A2A client intents and the company cycle) is a Society faculty and, like
  // SOCIETY_RUNTIME_ENABLED, is switched on at the service for a proof window.
  // A2A_CREDENTIAL_KEY is a shared variable (the registry seals, the
  // society-worker's federation pump unseals) created once by the operator.
  const a2a = {
    A2A_SERVER_ENABLED: "true",
    A2A_PUBLIC_BASE_URL: "https://${{registry.RAILWAY_PUBLIC_DOMAIN}}",
    A2A_FEDERATION_ENABLED: "true",
    A2A_CREDENTIAL_KEY: ctx.shared.A2A_CREDENTIAL_KEY,
    A2A_SOCIETY_CLIENT_ENABLED: "false",
    SOCIETY_COMPANY_CYCLE_ENABLED: "false",
  };

  // ── registry: public API; owns the schema through its pre-deploy step ──
  const registry = service("registry", {
    source: github(REPO, { branch: BRANCH, rootDirectory: "services/registry" }),
    // One deployment path owns bootstrap + `alembic upgrade head` + the
    // idempotent Society fleet seed; it runs in a separate container before the
    // new deployment starts and must exit non-zero on failure. The runtime
    // container then starts with SKIP_DB_BOOTSTRAP=true.
    preDeploy: "sh -c 'SKIP_DB_BOOTSTRAP=false /app/entrypoint.sh true && python -m app.society.seed'",
    healthcheck: "/readyz",
    healthcheckTimeout: 300,
    env: {
      ...common,
      ...dbEnv,
      ...redisEnv,
      PORT: "8000",
      SKIP_DB_BOOTSTRAP: "true",
      // Port 8000 is reachable only through Railway's edge (which sets
      // X-Real-IP) and the first-party private mesh — see ADR-0006 D5.
      TRUST_X_REAL_IP: "true",
      JWT_SECRET_KEY: ctx.shared.JWT_SECRET_KEY,
      JWT_ALGORITHM: "HS256",
      JWT_EXPIRATION: "3600",
      PUBLIC_BASE_URL: "https://${{RAILWAY_PUBLIC_DOMAIN}}",
      CORS_ALLOWED_ORIGINS: "https://${{dashboard.RAILWAY_PUBLIC_DOMAIN}}",
      RATE_LIMIT_PER_MINUTE: "60",
      // Staging has no SMTP. `log` is an explicit opt-in, never a fallback,
      // and is REFUSED in production (services/registry/app/email_delivery.py)
      // -- without it staging registration answers 503 and the staging
      // validator cannot create its canary user.
      EMAIL_DELIVERY_PROVIDER: "log",
      ORCHESTRATOR_ENABLED: "false",
      PUBLIC_AGENT_REGISTRATION_ENABLED: "false",
      AUTO_SCALER_ENABLED: "false",
      SOCIETY_OPERATOR_BOOTSTRAP_EMAILS: "",
      ...societyOff,
      ...a2a,
    },
  });

  // ── payment: private (no public domain); wallets/approvals API ──
  const payment = service("payment", {
    source: github(REPO, { branch: BRANCH, rootDirectory: "services/payment" }),
    healthcheck: "/readyz",
    healthcheckTimeout: 300,
    env: {
      ...common,
      ...dbEnv,
      ...redisEnv,
      PORT: "8001",
      JWT_SECRET_KEY: ctx.shared.JWT_SECRET_KEY,
      JWT_ALGORITHM: "HS256",
      RATE_LIMIT_PER_MINUTE: "60",
      INTERNAL_WORKER_TOKEN: ctx.shared.INTERNAL_WORKER_TOKEN,
    },
  });

  // ── worker: private; metrics on 9100 double as the deploy healthcheck ──
  const worker = service("worker", {
    source: github(REPO, { branch: BRANCH, rootDirectory: "services/worker" }),
    healthcheck: "/metrics",
    healthcheckTimeout: 120,
    env: {
      ...common,
      ...dbEnv,
      ...redisEnv,
      PORT: "9100",
      WORKER_METRICS_PORT: "9100",
      WORKER_POLL_INTERVAL_SEC: "30",
      // The worker talks to PostgreSQL/Redis and (for presence) the registry
      // only; it never calls the payment service, so it gets neither its URL
      // nor INTERNAL_WORKER_TOKEN (least privilege — that secret stays on payment).
      REGISTRY_API_URL: "http://${{registry.RAILWAY_PRIVATE_DOMAIN}}:8000",
    },
  });

  // ── dashboard: public Flask UI; talks to the registry over private DNS ──
  const dashboard = service("dashboard", {
    source: github(REPO, { branch: BRANCH, rootDirectory: "services/dashboard" }),
    healthcheck: "/healthz",
    healthcheckTimeout: 120,
    env: {
      ENVIRONMENT: "staging",
      PORT: "8080",
      FLASK_RUN_PORT: "8080",
      BEHIND_PROXY: "true",
      FLASK_SECRET_KEY: ctx.shared.FLASK_SECRET_KEY,
      // The registry is the only backend the dashboard calls (api_client.py);
      // payment is reached by nobody but the registry's own clients.
      REGISTRY_URL: "http://${{registry.RAILWAY_PRIVATE_DOMAIN}}:8000",
    },
  });

  // ── society-worker: registry image, custom start, ONE persistent volume ──
  const societyWorkspace = volume("society-workspace", { sizeMB: 2048 });
  const societyWorker = service("society-worker", {
    source: github(REPO, { branch: BRANCH, rootDirectory: "services/registry" }),
    // Volumes mount only at runtime: the checkout is cloned/refreshed and
    // aligned to RAILWAY_GIT_COMMIT_SHA by the start script, never at build or
    // pre-deploy time. SKIP_DB_BOOTSTRAP keeps it from racing registry migrations.
    start: "sh /app/start-society-railway.sh",
    healthcheck: "/metrics",
    healthcheckTimeout: 300,
    volumeMounts: { "/workspace": societyWorkspace },
    env: {
      ...common,
      ...dbEnv,
      ...redisEnv,
      PORT: "9101",
      SOCIETY_METRICS_PORT: "9101",
      SKIP_DB_BOOTSTRAP: "true",
      JWT_SECRET_KEY: ctx.shared.JWT_SECRET_KEY,
      SOCIETY_REPO_ROOT: "/workspace/repo",
      SOCIETY_WORKSPACE_ROOT: "/workspace/worktrees",
      SOCIETY_REPO_URL: "https://github.com/vansyson1308/agentnet.git",
      SOCIETY_REPO_REF: BRANCH,
      SOCIETY_WORKER_ID: "railway-staging-society-worker",
      SOCIETY_MODEL_OUTPUT_FORMAT: "auto",
      // Phase 4.1 (ADR-0007): the live DeepSeek posture is set on the service
      // (deepseek / disabled / none / json_object); the IaC keeps the
      // provider-neutral defaults so a non-DeepSeek endpoint never receives a
      // DeepSeek-only request field.
      SOCIETY_MODEL_CAPABILITY_PROFILE: "generic",
      SOCIETY_MODEL_THINKING_MODE: "auto",
      SOCIETY_MODEL_REASONING_EFFORT: "auto",
      SOCIETY_HEARTBEAT_INTERVAL_SECONDS: "3600",
      // The Society's eyes on the PUBLIC product (docs/PUBLIC_SURFACE_CONTRACT.md):
      // deterministic anonymous HTTP, effective only while SOCIETY_RUNTIME_ENABLED
      // is on. It needs no credential. The public origins it reads are the code
      // defaults (PUBLIC_PRODUCT_UI_ORIGIN / PUBLIC_PRODUCT_API_ORIGIN); this file
      // names no production host.
      SOCIETY_PUBLIC_SURFACE_MONITOR_ENABLED: "true",
      SOCIETY_PUBLIC_SURFACE_MONITOR_INTERVAL_SECONDS: "600",
      SOCIETY_PUBLIC_SURFACE_FAILURE_THRESHOLD: "2",
      ...societyOff,
      ...a2a,
    },
  });

  const data = group("Data", [db, cache, societyWorkspace]);
  const apps = group("AgentNet staging", [registry, payment, worker, dashboard, societyWorker]);
  return project("AgentNet", { resources: [data, apps] });
});
