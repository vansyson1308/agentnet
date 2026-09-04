#!/bin/sh
# Registry container entrypoint.
#
# Schema lifecycle (docs/DATABASE_SCHEMA_CONTRACT.md):
# 1. Fresh Postgres *volumes* are bootstrapped by Postgres' own
#    /docker-entrypoint-initdb.d, which runs services/registry/init-db/*.sql
#    (mounted via compose). The bundle currently ends at 17-app-tables.sql
#    and installs the COMPLETE schema.
# 2. An EMPTY database that never ran the bundle (managed Postgres, a
#    freshly created DB, CI) gets the same bundle applied here by
#    `python -m app.db_bootstrap --init-dir "$INIT_DB_DIR"`. INIT_DB_DIR
#    defaults to /app/init-db (the image copies init-db/ there); a local
#    checkout can run `INIT_DB_DIR=<checkout>/services/registry/init-db
#    bash entrypoint.sh true` from services/registry with alembic on PATH.
#    Migration 0001_baseline is a no-op, so without this step
#    `alembic upgrade head` would leave zero tables.
# 3. Either way we then stamp alembic at 0003_spending_cap_fix — the bundle
#    already contains 13-idempotency.sql / 14-spending-cap-fix.sql
#    (= migrations 0002/0003) — and `alembic upgrade head` applies 0004..
#    on top; those migrations are idempotent over the bundle.

set -e

# Wait for Postgres — docker-compose's depends_on with healthcheck already
# does this, but be defensive in case the script is run outside compose.
echo "registry: waiting for postgres..."
python - <<'PY'
import os, sys, time
import psycopg2
host = os.getenv("POSTGRES_HOST", "postgres")
port = int(os.getenv("POSTGRES_PORT", "5432"))
user = os.getenv("POSTGRES_USER", "agentnet")
pw   = os.getenv("POSTGRES_PASSWORD", "")
db   = os.getenv("POSTGRES_DB", "agentnet")
deadline = time.time() + 60
while time.time() < deadline:
    try:
        psycopg2.connect(host=host, port=port, user=user, password=pw, dbname=db, connect_timeout=2).close()
        print("postgres reachable")
        sys.exit(0)
    except Exception as e:
        time.sleep(1)
print("postgres did not become reachable in 60s")
sys.exit(1)
PY

# Decide how to bring the schema under alembic control:
#   0  -> alembic_version exists: just `upgrade head`
#   10 -> schema present (bundle ran) but never stamped: stamp 0003, upgrade
#   20 -> empty database: bootstrap from /app/init-db, stamp 0003, upgrade
#
# NOTE: Python exits 10 or 20 intentionally for the case dispatch below.
# Wrap with set +e / set -e so the shell captures the exit code instead
# of being killed by errexit.
set +e
python - <<'PY'
import os, sys
import psycopg2
host = os.getenv("POSTGRES_HOST", "postgres")
port = int(os.getenv("POSTGRES_PORT", "5432"))
user = os.getenv("POSTGRES_USER", "agentnet")
pw   = os.getenv("POSTGRES_PASSWORD", "")
db   = os.getenv("POSTGRES_DB", "agentnet")
conn = psycopg2.connect(host=host, port=port, user=user, password=pw, dbname=db)
cur  = conn.cursor()
cur.execute("SELECT to_regclass('public.alembic_version')")
exists = cur.fetchone()[0] is not None
cur.execute("SELECT to_regclass('public.transactions')")
schema_present = cur.fetchone()[0] is not None
conn.close()
sys.exit(0 if exists else (10 if schema_present else 20))
PY
stamp_code=$?
set -e
# Fail hard if Python crashed (exception, not intentional exit code)
if [ $stamp_code -ne 0 ] && [ $stamp_code -ne 10 ] && [ $stamp_code -ne 20 ]; then
  echo "registry: alembic stamp check failed (exit $stamp_code)"
  exit 1
fi
INIT_DB_DIR="${INIT_DB_DIR:-/app/init-db}"
export INIT_DB_DIR
case $stamp_code in
  0)
    echo "registry: alembic already stamped — running upgrade"
    ;;
  10)
    echo "registry: schema present from init-db, stamping baseline 0003"
    alembic stamp 0003_spending_cap_fix
    ;;
  20)
    echo "registry: empty DB — applying init-db bundle from ${INIT_DB_DIR}, then stamping baseline 0003"
    # Fails hard (set -e) if any bundle file errors; nothing is stamped then.
    python -m app.db_bootstrap --init-dir "${INIT_DB_DIR}"
    alembic stamp 0003_spending_cap_fix
    ;;
esac

alembic upgrade head

echo "registry: starting uvicorn"
exec "$@"
