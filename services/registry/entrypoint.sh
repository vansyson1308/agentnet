#!/bin/sh
# Registry container entrypoint.
#
# 1. Fresh DBs are bootstrapped by Postgres' /docker-entrypoint-initdb.d
#    (it runs services/registry/init-db/*.sql, mounted via compose). After
#    bootstrap the SQL bundle has installed schemas through 14-spending-cap-fix.sql.
# 2. We then stamp alembic at the matching baseline so future migrations can
#    pick up incrementally.
# 3. `alembic upgrade head` applies any newer migrations on top.

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

# Stamp alembic baseline if no version recorded yet (first boot after the
# SQL bundle ran). After that, only `upgrade head` is needed.
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
case $stamp_code in
  0)
    echo "registry: alembic already stamped — running upgrade"
    ;;
  10)
    echo "registry: schema present from init-db, stamping baseline 0003"
    alembic stamp 0003_spending_cap_fix
    ;;
  20)
    echo "registry: empty DB, alembic upgrade head will create schema (be sure init-db ran)"
    ;;
esac

alembic upgrade head

echo "registry: starting uvicorn"
exec "$@"
