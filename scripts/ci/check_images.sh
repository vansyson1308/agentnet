#!/usr/bin/env bash
# Phase 2.6 §7 — what ships is proven, not inferred: import each service's
# entry module INSIDE its built image (entrypoint bypassed; no database or
# Redis is contacted — engines connect lazily and the services run in dev mode
# for the smoke). Run after `docker compose build`.
set -euo pipefail
cd "$(dirname "$0")/../.."
run() {
    local svc="$1" code="$2"
    echo "== $svc image"
    docker compose run --rm --no-deps --entrypoint python \
        -e ENVIRONMENT=development -e JAEGER_ENABLED=false -e POSTGRES_PASSWORD=smoke-only \
        "$svc" -c "$code"
}
run registry   "import app.main, app.society.worker, app.db_bootstrap; from app.auth import pwd_context; assert pwd_context.verify('x', pwd_context.hash('x')); import websockets, sys; print('registry ok: python', sys.version.split()[0], 'websockets', websockets.__version__)"
run payment    "import app.main; from app.auth import pwd_context; assert pwd_context.verify('x', pwd_context.hash('x')); import sys; print('payment ok: python', sys.version.split()[0])"
run worker     "import app.worker, sys; print('worker ok: python', sys.version.split()[0])"
run simulation "import app.main, sys; print('simulation ok: python', sys.version.split()[0])"
run dashboard  "import app.main, sys; print('dashboard ok: python', sys.version.split()[0])"
