#!/bin/sh
# Society worker runtime bootstrap for managed platforms with a persistent
# volume (Railway). Runs at CONTAINER START (Railway volumes are mounted only at
# runtime — never at build or pre-deploy time), then execs the worker.
#
#   /workspace/repo       trusted base checkout of the PUBLIC repository
#   /workspace/worktrees  isolated candidate worktrees (agentnet-auto/<id>)
#
# Contract (docs/RAILWAY_STAGING.md, ADR-0006):
#   * first start: clone the public repository over HTTPS (no credential);
#     later starts: reuse the persistent checkout and fetch origin/<ref>;
#   * the remote URL never carries a credential (refused if it does);
#   * the trusted checkout is aligned to RAILWAY_GIT_COMMIT_SHA (the commit
#     this deployment was built from) when set and reachable, else to
#     origin/<ref>; the society's Builder branches from HEAD, so deployed
#     code and trusted source refer to the same commit;
#   * nothing is ever pushed or force-pushed; candidate worktrees are never
#     deleted — only demonstrably stale worktree metadata is pruned;
#   * a dirty trusted checkout is reset (candidates never live here);
#   * only the SHA is logged, never a credential.
#
# SOCIETY_BOOTSTRAP_ONLY=1 performs the bootstrap and exits 0 (tests).
# SOCIETY_GITHUB_PREFLIGHT=1 runs the App credential preflight before the worker.
set -eu

REPO_ROOT="${SOCIETY_REPO_ROOT:-/workspace/repo}"
WORKSPACE_ROOT="${SOCIETY_WORKSPACE_ROOT:-/workspace/worktrees}"
REPO_URL="${SOCIETY_REPO_URL:-https://github.com/vansyson1308/agentnet.git}"
REPO_REF="${SOCIETY_REPO_REF:-main}"
TARGET_SHA="${RAILWAY_GIT_COMMIT_SHA:-}"

log() { printf 'society-bootstrap: %s\n' "$*"; }

# Input shape checks (operator-set values, but they reach git argv and rm -rf):
case "$REPO_URL" in
  *@*|*\?*|*\#*) log "refusing SOCIETY_REPO_URL with an embedded credential, query or fragment"; exit 2 ;;
  https://*|file://*|/*) ;;
  *) log "refusing SOCIETY_REPO_URL: only https:// (or a local path in tests) is allowed"; exit 2 ;;
esac
case "$REPO_ROOT" in
  /?*/?*) ;;
  *) log "refusing SOCIETY_REPO_ROOT: must be an absolute path at least two levels deep"; exit 2 ;;
esac
if ! git check-ref-format --branch "$REPO_REF" >/dev/null 2>&1; then
  log "refusing SOCIETY_REPO_REF: not a valid branch name"; exit 2
fi
case "$TARGET_SHA" in
  "") ;;
  *[!0-9a-fA-F]*|?|??|???|????|?????|??????) log "refusing RAILWAY_GIT_COMMIT_SHA: not a hexadecimal commit id"; exit 2 ;;
esac

export GIT_TERMINAL_PROMPT=0
mkdir -p "$(dirname "$REPO_ROOT")" "$WORKSPACE_ROOT"
# The volume is mounted as root while the image may run as another user.
git config --global --add safe.directory "$REPO_ROOT" >/dev/null 2>&1 || true

if [ ! -d "$REPO_ROOT/.git" ]; then
  if [ -e "$REPO_ROOT" ] && [ -n "$(ls -A "$REPO_ROOT" 2>/dev/null)" ]; then
    log "$REPO_ROOT exists without .git (interrupted clone?) — removing it"
    rm -rf "$REPO_ROOT"
  fi
  log "cloning $REPO_URL (ref $REPO_REF) into $REPO_ROOT"
  git clone -q --branch "$REPO_REF" "$REPO_URL" "$REPO_ROOT"
else
  log "reusing persistent checkout at $REPO_ROOT"
  git -C "$REPO_ROOT" remote set-url origin "$REPO_URL"
  git -C "$REPO_ROOT" fetch -q origin "$REPO_REF"
fi

if [ -n "$TARGET_SHA" ]; then
  if ! git -C "$REPO_ROOT" cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null; then
    git -C "$REPO_ROOT" fetch -q origin "$TARGET_SHA" 2>/dev/null || true
  fi
  if ! git -C "$REPO_ROOT" cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null; then
    log "deployment commit $TARGET_SHA is not reachable from origin — refusing to start on an unknown base"
    exit 3
  fi
  target="$TARGET_SHA"
else
  target="$(git -C "$REPO_ROOT" rev-parse "origin/$REPO_REF")"
  log "RAILWAY_GIT_COMMIT_SHA not set; aligning to origin/$REPO_REF"
fi

if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  log "trusted checkout was dirty — resetting (candidates never live here)"
  git -C "$REPO_ROOT" reset -q --hard
  git -C "$REPO_ROOT" clean -q -fd
fi
git -C "$REPO_ROOT" checkout -q --detach "$target"
git -C "$REPO_ROOT" worktree prune
worktrees="$(find "$WORKSPACE_ROOT" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
log "trusted base checkout at $(git -C "$REPO_ROOT" rev-parse HEAD); workspace root $WORKSPACE_ROOT holds $worktrees candidate worktree dir(s)"

export SOCIETY_REPO_ROOT="$REPO_ROOT"
export SOCIETY_WORKSPACE_ROOT="$WORKSPACE_ROOT"
if [ "${SOCIETY_BOOTSTRAP_ONLY:-}" = "1" ]; then
  exit 0
fi

# Optional one-shot GitHub App credential preflight (SOCIETY_GITHUB_PREFLIGHT=1).
# It runs HERE because this is the only process the App private key is given to,
# and it is structural only: it mints a token, checks the installation scope and
# that Actions secrets stay refused, then scans its own report for the token
# before printing. Never key material, never the token.
#
# A blocked preflight does NOT stop the worker: cognition is independent of
# promotion, so the runtime keeps observing while promotion stays inert.
if [ "${SOCIETY_GITHUB_PREFLIGHT:-}" = "1" ]; then
  log "running GitHub App credential preflight"
  python -m app.society.github_preflight || log "github preflight reported a blocker — promotion stays inert, continuing to the worker"
fi

exec python -m app.society.worker
