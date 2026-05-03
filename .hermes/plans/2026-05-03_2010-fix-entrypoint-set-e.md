# Fix entrypoint.sh `set -e` killing intentional non-zero exit codes

> **For Hermes:** Execute directly — single-file fix, no delegation needed.

**Goal:** Fix `agentnet-staging-registry` infinite restart loop caused by `set -e` killing the entrypoint script when the alembic-stamp-check Python script intentionally returns exit code 10 or 20.

**Architecture:** The entrypoint uses a Python heredoc to check DB state and returns exit codes 10 (schema present, needs stamp) or 20 (empty DB). A `case` statement handles these. But `set -e` kills the script before `case` runs. Fix: capture the exit code before `set -e` can kill the script.

**Root cause:** `services/registry/entrypoint.sh:11` has `set -e`. Lines 38-54 run a Python script that `sys.exit(10)` or `sys.exit(20)`. These are **intentional** — line 55's `case $? in` is meant to dispatch on them. But `set -e` terminates the shell when the Python process exits non-zero, so `case` never runs. Container restarts → infinite loop.

---

## Files to change

- **Modify:** `services/registry/entrypoint.sh` — fix `set -e` + heredoc pattern
- **Also modify:** `services/payment/entrypoint.sh` if it exists with same pattern (check first)
- **Also modify:** `services/worker/entrypoint.sh` if it exists with same pattern (check first)

---

### Task 1: Verify affected files

```bash
grep -rl 'set -e' services/*/entrypoint.sh 2>/dev/null
grep -rl 'sys.exit' services/*/entrypoint.sh 2>/dev/null
```

### Task 2: Fix `services/registry/entrypoint.sh`

The fix: wrap the second Python heredoc so its exit code is captured without `set -e` killing the script. Two approaches:

**Approach A (minimal):** Add `|| true` to the Python invocation so `set -e` doesn't kill:
```bash
python - <<'PY'
... sys.exit(N) ...
PY
exit_code=$?
```
But `set -e` kills before `exit_code=$?` runs.

**Approach B (correct):** Use `set +e` / `set -e` around the Python script, or capture in a subshell:
```bash
exit_code=0
python - <<'PY' || exit_code=$?
... sys.exit(N) ...
PY
```
Wait — `set -e` won't kill a command that's part of `||`. Let me test:

Actually the simplest fix that works: just capture the exit code with `||`:

```bash
python - <<'PY' || exit_code=$?
... code ...
PY
```

But this doesn't assign exit_code when exit is 0. Better: use a function or `set +e` block.

**Best fix (tested pattern):**
```bash
# Save exit code without set -e interference
set +e
python - <<'PY'
... code that may exit 10 or 20 ...
PY
exit_code=$?
set -e
case $exit_code in
```

Change in `services/registry/entrypoint.sh`:
- After line 36 (after the first Python heredoc that exits 0 or 1)
- Before the second Python heredoc (line 38): add `set +e`
- After the second Python heredoc (after line 54 `PY`): capture exit code, then `set -e`

### Task 3: Apply same fix to other entrypoint scripts if they have the same pattern

Check: `services/payment/entrypoint.sh`, `services/worker/entrypoint.sh`

### Task 4: Rebuild and restart staging

```bash
docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d --build registry-staging
```

### Task 5: Verify

```bash
# Wait 30s then check
sleep 30
docker compose -f docker-compose.yml -f docker-compose.staging.yml ps | grep staging
# All 4 staging containers should be healthy

docker logs --tail=20 agentnet-staging-registry
# Must see: "registry: starting uvicorn"

curl -fsS https://staging.agentnet.io.vn/healthz
# Must return 200

curl -fsS https://staging.agentnet.io.vn/.well-known/agent-card.json | jq .url
# Must return "https://staging.agentnet.io.vn"
```
