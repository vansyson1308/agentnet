/**
 * AgentNet — Railway PRODUCTION topology (Infrastructure as Code).
 *
 * Applied with the Railway CLI from a linked checkout:
 *   railway link --project AgentNet --environment production
 *   railway config plan --config .railway/production.ts   # preview
 *   railway config apply --config .railway/production.ts  # apply after confirmation
 *
 * This is a SEPARATE declaration from .railway/railway.ts on purpose. One file
 * with staging/production conditionals would put the two environments one typo
 * apart; here each file refuses the other's environment outright (ADR-0008 D1).
 *
 * What production deliberately does NOT contain:
 *   - no society-worker, and no Society workspace volume: the Society is a
 *     staging faculty. Production runs the public application only (ADR-0008 D8).
 *   - no model credential and no GitHub App key. Their NAMES never appear here,
 *     so they cannot be set by this file even by accident.
 *   - no Jaeger, no simulation, no validator.
 *
 * Secrets: JWT_SECRET_KEY, FLASK_SECRET_KEY and INTERNAL_WORKER_TOKEN are
 * production-scoped SHARED variables the operator creates once, from stdin,
 * and never reads back. They are NOT copied from staging — a shared JWT secret
 * would make a staging token valid in production (ADR-0008 D9).
 *
 * Not expressible here (set once after apply — docs/PRODUCTION_RUNBOOK.md):
 * generated public domains, Wait for CI, and marking Postgres/Redis "no public
 * networking" (the default). Sealing variables is a UI action; ADR-0008 D9.
 */
import { defineRailway, github, group, postgres, project, redis, service } from "railway/iac";

const REPO = "vansyson1308/agentnet";

/**
 * Production deploys from a dedicated branch that the Society cannot write.
 * NOT `main`: main is autonomously merged, so following it would hand the
 * Society production authority through the back door (ADR-0008 D8).
 */
const BRANCH = "production";

export default defineRailway((ctx) => {
  if (ctx.environment !== "production") {
    throw new Error(
      `AgentNet production IaC targets the "production" environment only (linked: "${ctx.environment}"). ` +
        "Staging is declared in .railway/railway.ts — link the right environment and re-run.",
    );
  }

  const db = postgres("postgres");
  const cache = redis("redis");

  // Managed Postgres/Redis wired through reference variables. These are NEW
  // resources in the production environment: production must never point at
  // staging data, and the service ids differ so isolation is checkable.
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
    ENVIRONMENT: "production",
    JAEGER_ENABLED: "false",
    // FORWARDED_ALLOW_IPS stays unset: "*" would make uvicorn honour the
    // client-controlled leftmost X-Forwarded-For entry (rate-limit bypass).
    // The registry trusts X-Real-IP instead — ADR-0006 D5.
  };

  /**
   * Defense in depth. No society-worker exists in production, so nothing here
   * reads most of these — which is the point: if a future change ever gave the
   * production registry a Society surface, it would come up inert.
   * `config.py` additionally refuses runtime/autonomous-code/auto-merge in
   * production outright, so this file agrees with the code rather than
   * substituting for it.
   */
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

  // ── registry: public API; sole owner of the schema (ADR-0008 D4) ──
  const registry = service("registry", {
    source: github(REPO, { branch: BRANCH, rootDirectory: "services/registry" }),
    // Runs in a separate container BEFORE the new deployment starts and must
    // exit non-zero on failure. No Society fleet seed: production runs no
    // Society, so those rows would be dead weight the public app never reads.
    preDeploy: "sh -c 'SKIP_DB_BOOTSTRAP=false /app/entrypoint.sh true'",
    healthcheck: "/readyz",
    healthcheckTimeout: 300,
    env: {
      ...common,
      ...dbEnv,
      ...redisEnv,
      PORT: "8000",
      SKIP_DB_BOOTSTRAP: "true",
      TRUST_X_REAL_IP: "true",
      JWT_SECRET_KEY: ctx.shared.JWT_SECRET_KEY,
      JWT_ALGORITHM: "HS256",
      JWT_EXPIRATION: "3600",
      PUBLIC_BASE_URL: "https://${{RAILWAY_PUBLIC_DOMAIN}}",
      CORS_ALLOWED_ORIGINS: "https://${{dashboard.RAILWAY_PUBLIC_DOMAIN}}",
      RATE_LIMIT_PER_MINUTE: "60",
      ORCHESTRATOR_ENABLED: "false",
      PUBLIC_AGENT_REGISTRATION_ENABLED: "false",
      AUTO_SCALER_ENABLED: "false",
      // No operator is bootstrapped into production: the Society operator API
      // is a staging faculty and must have no privileged identity here.
      SOCIETY_OPERATOR_BOOTSTRAP_EMAILS: "",
      // Verification email delivery. `disabled` fails registration honestly
      // rather than creating an account nobody can ever log into; switching to
      // `smtp` needs SMTP_* on this service (docs/PRODUCTION_RUNBOOK.md).
      EMAIL_DELIVERY_PROVIDER: "disabled",
      ...societyOff,
    },
  });

  // ── payment: PRIVATE (no public domain); wallets/approvals API ──
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

  // ── worker: PRIVATE; metrics on 9100 double as the deploy healthcheck ──
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
      // Least privilege: the worker talks to PostgreSQL/Redis and (for
      // presence) the registry. It never calls payment, so it gets neither
      // payment's URL nor INTERNAL_WORKER_TOKEN.
      REGISTRY_API_URL: "http://${{registry.RAILWAY_PRIVATE_DOMAIN}}:8000",
    },
  });

  // ── dashboard: public Flask UI; reaches the registry over private DNS ──
  const dashboard = service("dashboard", {
    source: github(REPO, { branch: BRANCH, rootDirectory: "services/dashboard" }),
    healthcheck: "/healthz",
    healthcheckTimeout: 120,
    env: {
      ENVIRONMENT: "production",
      PORT: "8080",
      FLASK_RUN_PORT: "8080",
      BEHIND_PROXY: "true",
      FLASK_SECRET_KEY: ctx.shared.FLASK_SECRET_KEY,
      // The registry is the only backend the dashboard calls (api_client.py).
      REGISTRY_URL: "http://${{registry.RAILWAY_PRIVATE_DOMAIN}}:8000",
    },
  });

  const data = group("Data", [db, cache]);
  const apps = group("AgentNet production", [registry, payment, worker, dashboard]);
  return project("AgentNet", { resources: [data, apps] });
});
