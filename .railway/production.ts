/**
 * AgentNet — Railway PRODUCTION topology (Infrastructure as Code).
 *
 * Applied with the Railway CLI from a linked checkout:
 *   railway link --project AgentNet --environment production
 *   railway config plan --file .railway/production.ts    # preview; READ it
 *   railway config apply --file .railway/production.ts   # only after the plan is clean
 *
 * This is a SEPARATE declaration from .railway/railway.ts on purpose. One file
 * with staging/production conditionals would put the two environments one typo
 * apart; here each file refuses the other's environment outright (ADR-0008 D1).
 *
 * THIS FILE DESCRIBES WHAT IS LIVE (ADR-0008 D13). It was rewritten to match the
 * running environment exactly, resource for resource, after a pre-DNS audit
 * showed the first version would have been destructive to apply:
 *
 *   - Railway services are project-wide; the unprefixed names (registry,
 *     payment, worker, dashboard) are the STAGING services. Production runs
 *     prod-* services. Declaring the unprefixed names here would have pulled
 *     staging's services into production.
 *   - In a one-file project, omitting a resource means DELETING it. The old
 *     file declared none of the prod-* services, so an apply would have deleted
 *     prod-postgres (and its volume) and prod-redis, then created empty ones.
 *   - postgres()/redis() are database-product helpers with their own image and
 *     mount defaults (redis: railwayapp/redis:8.2 on /bitnami). The live data
 *     services are plain image services with volumes, so they are declared as
 *     exactly that: service(image(...)) + volume(...).
 *   - `https://${{RAILWAY_PUBLIC_DOMAIN}}` renders to `https://` while
 *     production is dark (no public domain exists), which the registry's own
 *     config refuses at boot; payment requires CORS_ALLOWED_ORIGINS outside
 *     development and the old file never set it.
 *
 * Every variable a service has live is declared, because an omitted variable is
 * a deleted variable. Values that are secrets are never written here: they are
 * references (`${{prod-postgres.POSTGRES_PASSWORD}}`, `${{shared.JWT_SECRET_KEY}}`)
 * or `preserve()`, which means "keep the value already set in Railway".
 *
 * What production deliberately does NOT contain:
 *   - no society-worker, and no Society workspace volume: the Society is a
 *     staging faculty. Production runs the public application only (ADR-0008 D8).
 *   - no model credential and no GitHub App key. Their NAMES never appear here,
 *     so they cannot be set by this file even by accident.
 *   - no Jaeger, no simulation.
 *   - no public surface beyond exactly three custom domains
 *     (docs/PRODUCTION_CUTOVER.md, docs/CLOUDFLARE_MIGRATION.md):
 *     api.agentnet.io.vn -> prod-registry :8000; agentnet.io.vn (the canonical
 *     public UI) -> prod-dashboard :8080; and dashboard.agentnet.io.vn ->
 *     prod-dashboard :8080, kept as a compatibility host that the Cloudflare
 *     edge answers with a 301 to the apex. No Railway service domain, no TCP
 *     proxy, and never a domain on payment, worker, Postgres or Redis. An
 *     undeclared custom domain would be DELETED on apply, which is why all three
 *     are declared here.
 *   - no prod-validator. It is a disposable operator instrument, not part of
 *     the application; while it exists a plan lists it for removal, which is
 *     the correct outcome and is marked destructive (docs/PRODUCTION_RUNBOOK.md).
 *
 * Secrets: JWT_SECRET_KEY, FLASK_SECRET_KEY and INTERNAL_WORKER_TOKEN are
 * production-scoped SHARED variables the operator creates once and never reads
 * back. They are NOT copied from staging — a shared JWT secret would make a
 * staging token valid in production (ADR-0008 D9). SMTP_PASSWORD is an
 * OWNER-MANAGED secret (the Resend sending key): set by the owner directly in
 * Railway, never in git, never read back. If it is ever absent, registration
 * fails closed with 503 and creates nothing (registration is atomic with
 * delivery), so its absence is loud and harmless.
 */
import { defineRailway, github, image, preserve, project, service, volume } from "railway/iac";

const REPO = "vansyson1308/agentnet";

/**
 * Production deploys from a dedicated branch that the Society cannot write.
 * NOT `main`: main is autonomously merged, so following it would hand the
 * Society production authority through the back door (ADR-0008 D8).
 */
const BRANCH = "production";

/** The single region every production resource runs in today. */
const REGION = "us-east4-eqdc4a";

/** The canonical public API origin. Verification links are built from it. */
const PUBLIC_API_ORIGIN = "https://api.agentnet.io.vn";

export default defineRailway((ctx) => {
  if (ctx.environment !== "production") {
    throw new Error(
      `AgentNet production IaC targets the "production" environment only (linked: "${ctx.environment}"). ` +
        "Staging is declared in .railway/railway.ts — link the right environment and re-run.",
    );
  }

  /**
   * An app service built from this repository's `production` branch.
   * `checkSuites: true` is Railway's "Wait for CI": a push to `production` is
   * not deployed until GitHub reports the CI workflow run succeeded on it.
   */
  const app = (root: string) => ({
    source: github(REPO, { branch: BRANCH, rootDirectory: `/${root}`, checkSuites: true }),
    build: { builder: "RAILPACK" as const, watchPatterns: [`/${root}/**`] },
    deploy: { restartPolicyType: "ALWAYS" as const },
    regions: { [REGION]: 1 },
  });

  // ── data: private, persistent, never recreated ─────────────────────────
  // Declared as the plain image services they are live. Their generated
  // passwords were created once in Railway and are preserved, never re-set.
  const postgresVolume = volume("prod-postgres-volume", { region: REGION, sizeMB: 5000 });
  const db = service("prod-postgres", {
    source: image("ghcr.io/railwayapp-templates/postgres-ssl:18"),
    regions: { [REGION]: 1 },
    volumeMounts: { "/var/lib/postgresql/data": postgresVolume },
    env: {
      POSTGRES_USER: "agentnet",
      POSTGRES_DB: "agentnet",
      POSTGRES_PASSWORD: preserve(),
      PGDATA: "/var/lib/postgresql/data/pgdata",
      SSL_CERT_DAYS: "820",
      PGHOST: "${{RAILWAY_PRIVATE_DOMAIN}}",
      PGPORT: "5432",
      PGUSER: "${{POSTGRES_USER}}",
      PGPASSWORD: "${{POSTGRES_PASSWORD}}",
      PGDATABASE: "${{POSTGRES_DB}}",
      RAILWAY_DEPLOYMENT_DRAINING_SECONDS: "60",
    },
  });

  const redisVolume = volume("prod-redis-volume", { region: REGION, sizeMB: 5000 });
  const cache = service("prod-redis", {
    source: image("redis:8.2"),
    // Fail closed: a Redis that would start without a password refuses to
    // start at all. Proven necessary during bring-up (ADR-0008 D11).
    start:
      "/bin/sh -c 'if [ -z \"$REDIS_PASSWORD\" ]; then echo \"FATAL: REDIS_PASSWORD is empty; refusing to start an unauthenticated Redis\"; exit 1; fi; " +
      "echo \"redis: requirepass will be set (length ${#REDIS_PASSWORD})\"; rm -rf \"$RAILWAY_VOLUME_MOUNT_PATH/lost+found/\"; " +
      "exec docker-entrypoint.sh redis-server --requirepass \"$REDIS_PASSWORD\" --save 60 1 --dir \"$RAILWAY_VOLUME_MOUNT_PATH\"'",
    deploy: { restartPolicyType: "ALWAYS" },
    regions: { [REGION]: 1 },
    volumeMounts: { "/data": redisVolume },
    env: {
      REDIS_PASSWORD: preserve(),
      REDISHOST: "${{RAILWAY_PRIVATE_DOMAIN}}",
      REDISPORT: "6379",
      REDISUSER: "default",
      REDISPASSWORD: "${{REDIS_PASSWORD}}",
    },
  });

  // Wired exactly as live: host by private DNS, credentials by reference.
  const dbEnv = {
    POSTGRES_HOST: db.env.RAILWAY_PRIVATE_DOMAIN,
    POSTGRES_PORT: "5432",
    POSTGRES_USER: db.env.POSTGRES_USER,
    POSTGRES_PASSWORD: db.env.POSTGRES_PASSWORD,
    POSTGRES_DB: db.env.POSTGRES_DB,
  };
  const redisEnv = {
    REDIS_HOST: cache.env.RAILWAY_PRIVATE_DOMAIN,
    REDIS_PORT: "6379",
    REDIS_PASSWORD: cache.env.REDIS_PASSWORD,
  };

  const common = {
    ENVIRONMENT: "production",
    JAEGER_ENABLED: "false",
    // FORWARDED_ALLOW_IPS stays unset: "*" would make uvicorn honour the
    // client-controlled leftmost X-Forwarded-For entry (rate-limit bypass).
    // The registry trusts X-Real-IP instead — ADR-0006 D5.
  };

  /**
   * The one browser origin the public API accepts: the canonical public UI,
   * served at the apex. Today's dashboard calls the registry server-side over
   * private DNS (REGISTRY_URL below) and needs no CORS at all; this admits
   * exactly the origin any future in-browser call would come from, and nothing
   * else -- never "*", never a Railway-generated domain. The compatibility host
   * dashboard.agentnet.io.vn is not admitted: the Cloudflare edge answers every
   * request to it with a 301 to this origin, so no page is ever served from it.
   *
   * Sequencing (docs/CLOUDFLARE_MIGRATION.md §8): this is the FINAL value. The
   * live variable changes to it only after the owner delegates the zone to
   * Cloudflare and the apex serves the dashboard over HTTPS; until then this
   * file is merged no earlier than that live change, so the file never
   * declares a value production does not have.
   */
  const PUBLIC_UI_ORIGIN = "https://agentnet.io.vn";

  /**
   * Payment is private forever: no browser can reach it, so no browser origin
   * is admitted. The private dashboard origin is not one a browser can present;
   * it only satisfies payment's refusal to start without an explicit list.
   */
  const PRIVATE_ONLY_CORS_ORIGIN = "http://prod-dashboard.railway.internal:8080";

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

  /**
   * A2A 1.0 (ADR-0009). Declared DARK: the reviewed code ships with every flag
   * off and production is enabled one flag at a time by the trusted operator
   * (docs/PRODUCTION_RUNBOOK.md "A2A enablement"); a follow-up change flips
   * these lines only after the live value exists, so this file never claims a
   * state production lacks. The cards name the canonical API, never a request
   * Host header. The Society A2A client stays off in production: there is no
   * production Society. A2A_CREDENTIAL_KEY (the federation credential vault) is
   * added -- as a production-scoped shared variable -- together with federation.
   */
  const a2aDark = {
    A2A_PUBLIC_BASE_URL: PUBLIC_API_ORIGIN,
    A2A_SERVER_ENABLED: "false",
    A2A_FEDERATION_ENABLED: "false",
    A2A_SOCIETY_CLIENT_ENABLED: "false",
    SOCIETY_COMPANY_CYCLE_ENABLED: "false",
  };

  /**
   * Verification email through Resend's SMTP relay. Port 2465 is Resend's
   * implicit-TLS alternate: Railway egress blocks 465 and 587, measured from
   * inside production (docs/PRODUCTION_RUNBOOK.md). The sender domain is the
   * verified `mail.agentnet.io.vn`, which is independent of the web DNS.
   */
  const smtp = {
    EMAIL_DELIVERY_PROVIDER: "smtp",
    SMTP_HOST: "smtp.resend.com",
    SMTP_PORT: "2465",
    SMTP_USERNAME: "resend",
    SMTP_PASSWORD: preserve(),
    SMTP_FROM: "AgentNet <noreply@mail.agentnet.io.vn>",
    SMTP_TLS: "true",
    SMTP_STARTTLS: "false",
  };

  // ── registry: the public API; sole owner of the schema ──
  const registry = service("prod-registry", {
    ...app("services/registry"),
    domains: [{ domain: "api.agentnet.io.vn", port: 8000 }],
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
      // A literal, not `${{RAILWAY_PUBLIC_DOMAIN}}`: the link in a verification
      // email must name the canonical API, not whatever domain Railway assigns.
      PUBLIC_BASE_URL: PUBLIC_API_ORIGIN,
      CORS_ALLOWED_ORIGINS: PUBLIC_UI_ORIGIN,
      RATE_LIMIT_PER_MINUTE: "60",
      ORCHESTRATOR_ENABLED: "false",
      PUBLIC_AGENT_REGISTRATION_ENABLED: "false",
      AUTO_SCALER_ENABLED: "false",
      // No operator is bootstrapped into production: the Society operator API
      // is a staging faculty and must have no privileged identity here.
      SOCIETY_OPERATOR_BOOTSTRAP_EMAILS: "",
      ...smtp,
      ...societyOff,
      ...a2aDark,
    },
  });

  // ── payment: PRIVATE (no public domain, ever); wallets/approvals API ──
  const payment = service("prod-payment", {
    ...app("services/payment"),
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
      // Required outside development (payment refuses to start without it).
      CORS_ALLOWED_ORIGINS: PRIVATE_ONLY_CORS_ORIGIN,
    },
  });

  // ── worker: PRIVATE; metrics on 9100 double as the deploy healthcheck ──
  const worker = service("prod-worker", {
    ...app("services/worker"),
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
      REGISTRY_API_URL: "http://${{prod-registry.RAILWAY_PRIVATE_DOMAIN}}:8000",
    },
  });

  // ── dashboard: the public UI; calls the registry privately ──
  // The apex is canonical; dashboard.* stays attached so its certificate and
  // routing survive, and the Cloudflare edge redirects it to the apex.
  const dashboard = service("prod-dashboard", {
    ...app("services/dashboard"),
    domains: [
      { domain: "agentnet.io.vn", port: 8080 },
      { domain: "dashboard.agentnet.io.vn", port: 8080 },
    ],
    healthcheck: "/healthz",
    healthcheckTimeout: 120,
    env: {
      ENVIRONMENT: "production",
      PORT: "8080",
      FLASK_RUN_PORT: "8080",
      BEHIND_PROXY: "true",
      FLASK_SECRET_KEY: ctx.shared.FLASK_SECRET_KEY,
      // The registry is the only backend the dashboard calls (api_client.py).
      REGISTRY_URL: "http://${{prod-registry.RAILWAY_PRIVATE_DOMAIN}}:8000",
    },
  });

  return project("AgentNet", {
    resources: [postgresVolume, db, redisVolume, cache, registry, payment, worker, dashboard],
  });
});
