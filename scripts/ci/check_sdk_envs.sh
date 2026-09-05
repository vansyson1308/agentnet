#!/usr/bin/env bash
# Phase 2.6 §6.6 — the SDK's optional WebSocket client supports websockets 12
# (classic asyncio client) through the current release (new asyncio client,
# default since 14.0). Run the SDK tests at both ends in fresh virtualenvs so
# the union environment's single version is never the only proof.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY="${PYTHON:-python3}"
WORK="${SDK_ENV_ROOT:-.sdk-envs}"
mkdir -p "$WORK"
for spec in "websockets==12.0" "websockets>=17,<18"; do
    name="$(echo "$spec" | tr -c 'a-z0-9\n' '_')"
    venv="$WORK/$name"
    echo "== SDK with $spec"
    rm -rf "$venv"
    "$PY" -m venv "$venv"
    "$venv/bin/pip" install -q --upgrade pip
    # sdk/python/setup.py runtime deps + the test tooling pytest.ini expects
    # (pytest-timeout for `timeout =`, pydantic/sqlalchemy so the warning
    # filters' categories resolve).
    "$venv/bin/pip" install -q "httpx==0.28.1" "pydantic==2.13.5" "sqlalchemy==2.0.23" \
        "pytest==9.1.1" "pytest-asyncio==1.4.0" "pytest-timeout==2.4.0" "$spec"
    "$venv/bin/python" -c "import websockets; print('   websockets', websockets.__version__)"
    "$venv/bin/python" -m pytest -p no:cacheprovider tests/test_sdk.py -q
done
