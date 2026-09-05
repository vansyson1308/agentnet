#!/usr/bin/env bash
# Phase 2.6 §7 — the union CI interpreter is never the sole compatibility proof.
#
# Every service's requirements.txt is installed ALONE into a fresh virtualenv
# (exactly what its Dockerfile does), `pip check`ed, and import-smoked; the
# resulting freezes are then compared: a runtime library shared by several
# services must resolve to ONE version everywhere (an image can otherwise ship
# a version that no test ever ran against — that is how websockets 12 vs 16
# went unnoticed).
#
#   bash scripts/ci/check_service_envs.sh          # PYTHON=python3.10 to match the images
set -euo pipefail
cd "$(dirname "$0")/../.."
PY="${PYTHON:-python3}"
WORK="${SERVICE_ENV_ROOT:-.service-envs}"
SERVICES=(registry payment worker simulation dashboard)

# Import smoke only: dev mode, nothing is contacted (engines connect lazily).
export ENVIRONMENT=development JAEGER_ENABLED=false
export POSTGRES_HOST=db.invalid POSTGRES_PASSWORD=smoke-only REDIS_HOST=redis.invalid REDIS_PASSWORD=""

smoke() {
    case "$1" in
        registry)   echo "import app.main, app.society.worker, app.db_bootstrap; from app.auth import pwd_context; assert pwd_context.verify('x', pwd_context.hash('x'))" ;;
        payment)    echo "import app.main; from app.auth import pwd_context; assert pwd_context.verify('x', pwd_context.hash('x'))" ;;
        worker)     echo "import app.worker" ;;
        simulation) echo "import app.main" ;;
        dashboard)  echo "import app.main" ;;
    esac
}

mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"   # absolute: the smoke runs from inside services/<svc>
for svc in "${SERVICES[@]}"; do
    venv="$WORK/$svc"
    echo "== $svc ($("$PY" --version 2>&1))"
    rm -rf "$venv"
    "$PY" -m venv "$venv"
    "$venv/bin/pip" install -q --upgrade pip
    "$venv/bin/pip" install -q -r "services/$svc/requirements.txt"
    if [[ "$svc" == "registry" ]]; then
        "$venv/bin/pip" install -q alembic==1.13.1   # services/registry/Dockerfile installs it the same way
    fi
    "$venv/bin/pip" check
    ( cd "services/$svc" && "$venv/bin/python" -c "$(smoke "$svc")" )
    "$venv/bin/pip" freeze > "$WORK/$svc.freeze"
    echo "   pip check ok, import smoke ok"
done

"$PY" - "$WORK" "${SERVICES[@]}" <<'PYEOF'
import re, sys, pathlib
work, services = pathlib.Path(sys.argv[1]), sys.argv[2:]
SHARED = {
    "fastapi", "starlette", "pydantic", "pydantic-core", "pydantic-settings", "sqlalchemy",
    "psycopg2-binary", "redis", "httpx", "httpcore", "h11", "anyio", "python-jose", "passlib",
    "bcrypt", "websockets", "uvicorn", "opentelemetry-api", "opentelemetry-sdk",
    "opentelemetry-semantic-conventions", "opentelemetry-instrumentation-fastapi",
    "opentelemetry-instrumentation-sqlalchemy", "opentelemetry-exporter-otlp-proto-http",
    "prometheus-client", "structlog", "python-dotenv", "python-ulid", "alembic", "cryptography",
}
versions = {}
for svc in services:
    for line in (work / f"{svc}.freeze").read_text().splitlines():
        m = re.match(r"([A-Za-z0-9_.\-]+)==(\S+)", line)
        if m:
            versions.setdefault(m.group(1).lower().replace("_", "-"), {})[svc] = m.group(2)
bad = []
print("\nshared runtime libraries across isolated service environments:")
for pkg in sorted(SHARED):
    got = versions.get(pkg, {})
    if len(got) < 2:
        continue
    distinct = sorted(set(got.values()))
    flag = "  DIVERGENT" if len(distinct) > 1 else ""
    print(f"  {pkg:45s} " + "  ".join(f"{s}={v}" for s, v in sorted(got.items())) + flag)
    if len(distinct) > 1:
        bad.append(pkg)
if bad:
    print(f"\nFAIL: shared runtime pins diverge between services: {', '.join(bad)}", file=sys.stderr)
    sys.exit(1)
print("\nOK: every shared runtime library resolves to one version in every service environment")
PYEOF
