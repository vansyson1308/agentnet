#!/usr/bin/env bash
# Re-runnable migration applier for already-deployed databases.
#
# init-db/*.sql files run automatically only on a *fresh* postgres volume.
# For an existing prod DB, apply newly added migrations with:
#
#   DATABASE_URL=postgresql://user:pass@host:5432/db ./apply-pending.sh
#
# The script pipes each *.sql in lexical order through psql. Files use
# `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` so re-runs are no-ops on
# objects that already exist.

set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
    # The official Postgres image executes every *.sh file in this directory
    # during first-volume initialization. The numbered SQL files have already
    # been applied in that path, so this operator helper must be a no-op there.
    if [[ "${BASH_SOURCE[0]}" == /docker-entrypoint-initdb.d/* ]]; then
        echo "apply-pending.sh: init hook detected; numbered SQL files already applied"
        exit 0
    fi
    echo "error: DATABASE_URL not set" >&2
    exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
shopt -s nullglob
files=("${DIR}"/*.sql)
shopt -u nullglob

for f in "${files[@]}"; do
    name="$(basename "$f")"
    echo "▸ applying ${name}…"
    psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 -f "$f"
done

echo "✓ all migrations applied"
