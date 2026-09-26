"""Phase 4 — Railway deployment adaptation, proven without Railway.

* ``SKIP_DB_BOOTSTRAP=true`` makes the registry entrypoint exec its command
  without touching the database (society worker / runtime container), while
  the default path keeps bootstrapping (``tests/test_db_parity.py``).
* ``start-society-railway.sh`` clones the public repository onto the volume
  on first start, reuses and refreshes it afterwards, aligns the trusted
  checkout to ``RAILWAY_GIT_COMMIT_SHA``, refuses credentials in the URL and
  unknown commits, resets a dirty trusted checkout, prunes only stale
  worktree metadata and never deletes candidate worktrees — idempotently.
* ``.railway/railway.ts`` (Infrastructure as Code) is staging-only, carries
  no secret value, keeps databases/workers private, attaches the single
  volume to the society worker only, ends with the Society switched OFF and
  never names the custom domains.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = REPO / "services" / "registry"
SCRIPT = REGISTRY / "start-society-railway.sh"
IAC = REPO / ".railway" / "railway.ts"


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _bootstrap(env_extra: dict, *, expect_rc: int = 0) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SOCIETY_", "RAILWAY_"))}
    env.update({"SOCIETY_BOOTSTRAP_ONLY": "1", "HOME": env_extra.pop("HOME", os.environ.get("HOME", "/tmp"))})
    env.update(env_extra)
    proc = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == expect_rc, (proc.returncode, proc.stdout, proc.stderr)
    return proc


@pytest.fixture
def origin(tmp_path):
    """A local stand-in for the public GitHub repository: two commits on main."""
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    (src / "README.md").write_text("one\n")
    _git(["add", "-A"], src)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one"], src)
    first = _git(["rev-parse", "HEAD"], src)
    (src / "README.md").write_text("two\n")
    _git(["add", "-A"], src)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "two"], src)
    second = _git(["rev-parse", "HEAD"], src)
    bare = tmp_path / "origin.git"
    _git(["clone", "-q", "--bare", str(src), str(bare)], tmp_path)
    return bare, first, second, src


# ── entrypoint: SKIP_DB_BOOTSTRAP ──────────────────────────────────────


def test_skip_db_bootstrap_execs_without_touching_the_database(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("POSTGRES_")}
    env.update({"SKIP_DB_BOOTSTRAP": "true", "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "1", "POSTGRES_PASSWORD": "unused"})
    proc = subprocess.run(["bash", "entrypoint.sh", "sh", "-c", "echo started-$$"], cwd=REGISTRY, env=env, capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    assert "SKIP_DB_BOOTSTRAP=true" in proc.stdout and "started-" in proc.stdout
    assert "waiting for postgres" not in proc.stdout and "alembic" not in proc.stdout.lower()


def test_default_entrypoint_path_still_bootstraps():
    text = (REGISTRY / "entrypoint.sh").read_text()
    assert 'SKIP_DB_BOOTSTRAP:-false' in text and "alembic upgrade head" in text and "python -m app.db_bootstrap" in text
    assert text.index("SKIP_DB_BOOTSTRAP") < text.index("waiting for postgres"), "the skip decision happens before any DB access"


def test_registry_image_ships_the_society_bootstrap_script():
    docker = (REGISTRY / "Dockerfile").read_text()
    assert "COPY start-society-railway.sh /app/start-society-railway.sh" in docker
    assert "chmod +x /app/entrypoint.sh /app/start-society-railway.sh" in docker
    assert SCRIPT.exists() and os.access(SCRIPT, os.X_OK)


# ── society runtime bootstrap script ───────────────────────────────────


def test_first_start_clones_then_reuses_and_aligns_to_the_deployment_sha(tmp_path, origin):
    bare, first, second, _ = origin
    vol = tmp_path / "volume"
    env = {"SOCIETY_REPO_URL": str(bare), "SOCIETY_REPO_ROOT": str(vol / "repo"), "SOCIETY_WORKSPACE_ROOT": str(vol / "worktrees"), "RAILWAY_GIT_COMMIT_SHA": first, "HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir()
    out = _bootstrap(dict(env)).stdout
    assert "cloning" in out and f"trusted base checkout at {first}" in out
    assert (vol / "repo" / ".git").is_dir() and (vol / "worktrees").is_dir()
    assert _git(["rev-parse", "HEAD"], vol / "repo") == first
    assert _git(["remote", "get-url", "origin"], vol / "repo") == str(bare)
    # marker on the volume survives a second start; the checkout is reused, not re-cloned
    marker = vol / "worktrees" / "marker.txt"
    marker.write_text("persist\n")
    env["RAILWAY_GIT_COMMIT_SHA"] = second
    out2 = _bootstrap(dict(env)).stdout
    assert "reusing persistent checkout" in out2 and "cloning" not in out2
    assert _git(["rev-parse", "HEAD"], vol / "repo") == second
    assert marker.read_text() == "persist\n"
    # no target SHA: aligns to origin/main
    env.pop("RAILWAY_GIT_COMMIT_SHA")
    out3 = _bootstrap(dict(env)).stdout
    assert "aligning to origin/main" in out3 and _git(["rev-parse", "HEAD"], vol / "repo") == second
    assert "password" not in (out + out2 + out3).lower()


def test_dirty_trusted_checkout_is_reset_and_candidate_worktrees_are_kept(tmp_path, origin):
    bare, first, second, _ = origin
    vol = tmp_path / "volume"
    (tmp_path / "home").mkdir()
    env = {"SOCIETY_REPO_URL": str(bare), "SOCIETY_REPO_ROOT": str(vol / "repo"), "SOCIETY_WORKSPACE_ROOT": str(vol / "worktrees"), "RAILWAY_GIT_COMMIT_SHA": second, "HOME": str(tmp_path / "home")}
    _bootstrap(dict(env))
    repo = vol / "repo"
    # a candidate worktree, as the Builder creates it, plus a stale worktree entry
    wt = vol / "worktrees" / "cand-1"
    _git(["worktree", "add", "-q", "-B", "agentnet-auto/cand-1", str(wt), second], repo)
    (wt / "fix.txt").write_text("candidate work\n")
    stale = vol / "worktrees" / "stale"
    _git(["worktree", "add", "-q", "-B", "agentnet-auto/stale", str(stale), second], repo)
    import shutil

    shutil.rmtree(stale)
    # dirty the TRUSTED checkout (must never happen; must be healed)
    (repo / "README.md").write_text("tampered\n")
    (repo / "junk.txt").write_text("junk\n")
    out = _bootstrap(dict(env)).stdout
    assert "dirty" in out and "resetting" in out
    assert (repo / "README.md").read_text() == "two\n" and not (repo / "junk.txt").exists()
    assert (wt / "fix.txt").read_text() == "candidate work\n", "candidate worktree untouched"
    listing = _git(["worktree", "list", "--porcelain"], repo)
    assert str(wt) in listing and str(stale) not in listing, "only stale metadata pruned"
    assert "agentnet-auto/cand-1" in _git(["branch", "--list", "agentnet-auto/*"], repo)


def test_unknown_commit_and_credentialed_urls_are_refused(tmp_path, origin):
    bare, first, second, _ = origin
    vol = tmp_path / "volume"
    (tmp_path / "home").mkdir()
    base = {"SOCIETY_REPO_ROOT": str(vol / "repo"), "SOCIETY_WORKSPACE_ROOT": str(vol / "worktrees"), "HOME": str(tmp_path / "home")}
    proc = _bootstrap({**base, "SOCIETY_REPO_URL": "https://SENTINEL@github.com/vansyson1308/agentnet.git"}, expect_rc=2)
    assert "embedded credential" in proc.stdout and "SENTINEL" not in proc.stdout
    _bootstrap({**base, "SOCIETY_REPO_URL": "git://example/repo.git"}, expect_rc=2)
    proc = _bootstrap({**base, "SOCIETY_REPO_URL": str(bare), "RAILWAY_GIT_COMMIT_SHA": "0" * 40}, expect_rc=3)
    assert "not reachable from origin" in proc.stdout
    assert not (vol / "repo" / "junk").exists()


def test_bootstrap_refuses_option_injection_query_urls_and_shallow_roots(tmp_path, origin):
    """Operator-set values reach git argv and an rm -rf: a ref or commit id that
    starts with '-' must never become a git option, a URL with a query or
    fragment is refused before it can be logged, and the repo root must be a
    real mount point (never '/')."""
    bare, first, second, _ = origin
    vol = tmp_path / "volume"
    (tmp_path / "home").mkdir()
    base = {"SOCIETY_REPO_ROOT": str(vol / "repo"), "SOCIETY_WORKSPACE_ROOT": str(vol / "worktrees"), "HOME": str(tmp_path / "home"), "SOCIETY_REPO_URL": str(bare)}
    proc = _bootstrap({**base, "SOCIETY_REPO_REF": "--upload-pack=/bin/true"}, expect_rc=2)
    assert "SOCIETY_REPO_REF" in proc.stdout
    proc = _bootstrap({**base, "RAILWAY_GIT_COMMIT_SHA": "--upload-pack=/bin/true"}, expect_rc=2)
    assert "RAILWAY_GIT_COMMIT_SHA" in proc.stdout
    _bootstrap({**base, "RAILWAY_GIT_COMMIT_SHA": "abc"}, expect_rc=2)  # too short to be a commit id
    proc = _bootstrap({**base, "SOCIETY_REPO_URL": f"{bare}?ref=SENTINEL"}, expect_rc=2)
    assert "SENTINEL" not in proc.stdout and "SENTINEL" not in proc.stderr
    _bootstrap({**base, "SOCIETY_REPO_URL": f"{bare}#SENTINEL"}, expect_rc=2)
    for root in ("/", "/repo", "relative/repo"):  # "" falls back to the documented default path
        _bootstrap({**base, "SOCIETY_REPO_ROOT": root}, expect_rc=2)
    assert not (vol / "repo").exists(), "every refusal happens before anything is cloned or removed"
    # the valid shapes still work
    _bootstrap({**base, "RAILWAY_GIT_COMMIT_SHA": first})
    assert _git(["rev-parse", "HEAD"], vol / "repo") == first


def test_bootstrap_script_never_pushes_or_forces():
    text = SCRIPT.read_text()
    assert "git push" not in text and "--force" not in text and "-f " not in text.replace("clean -q -fd", "")
    assert "GIT_TERMINAL_PROMPT=0" in text and "exec python -m app.society.worker" in text
    assert "worktree prune" in text and "worktree remove" not in text


# ── Infrastructure as Code ─────────────────────────────────────────────


def test_iac_is_staging_only_secret_free_and_keeps_the_boundaries():
    text = IAC.read_text(encoding="utf-8")
    assert 'ctx.environment !== "staging"' in text and "throw new Error" in text, "refuses non-staging environments"
    for svc in ("registry", "payment", "worker", "dashboard", "society-worker", "postgres", "redis"):
        assert f'"{svc}"' in text, svc
    assert "simulation" not in text and "jaeger" not in text.lower().replace("jaeger_enabled", "")
    # secrets only as shared-variable references, never literals
    for secret in ("JWT_SECRET_KEY", "FLASK_SECRET_KEY", "INTERNAL_WORKER_TOKEN"):
        assert re.search(rf"{secret}: ctx\.shared\.{secret}", text), secret
        assert not re.search(rf'{secret}: "', text), f"{secret} must not be a literal"
    for forbidden in ("SOCIETY_MODEL_API_KEY", "LLM_API_KEY", "SOCIETY_GITHUB_TOKEN", "PRIVATE_KEY", "DEEPSEEK", "agentnet.io.vn", "139.180.143.222", "-----BEGIN"):
        assert forbidden not in text, forbidden
    # managed credentials only through references
    for ref in ("db.env.PGHOST", "db.env.PGPASSWORD", "cache.env.REDISHOST", "cache.env.REDISPASSWORD"):
        assert ref in text, ref
    assert "volumeMounts" in text and text.count("volumeMounts") == 1, "exactly one service mounts a volume"
    assert '"/workspace": societyWorkspace' in text
    assert 'SOCIETY_REPO_ROOT: "/workspace/repo"' in text and 'SOCIETY_WORKSPACE_ROOT: "/workspace/worktrees"' in text
    for off in ('SOCIETY_RUNTIME_ENABLED: "false"', 'SOCIETY_AUTONOMOUS_CODE_ENABLED: "false"', 'SOCIETY_STAGING_DEPLOY_ENABLED: "false"', 'SOCIETY_PROMOTION_PROVIDER: "disabled"', 'SOCIETY_GITHUB_CREDENTIAL_PROVIDER: "disabled"', 'SOCIETY_AUTO_MERGE_ENABLED: "false"', 'SOCIETY_DEPLOYMENT_PROVIDER: "disabled"', 'SOCIETY_MODEL_PROVIDER: "scripted"'):
        assert off in text, off
    assert "domains:" not in text, "Railway-generated domains only; no custom domain"
    assert 'SKIP_DB_BOOTSTRAP=false /app/entrypoint.sh true' in text, "the registry pre-deploy step owns migrations"
    assert text.count('SKIP_DB_BOOTSTRAP: "true"') == 2, "registry runtime + society worker never migrate at start"
    assert 'start: "sh /app/start-society-railway.sh"' in text
    assert 'ENVIRONMENT: "staging"' in text and '"production"' not in text


def test_iac_variable_names_match_the_configuration_contract():
    text = IAC.read_text(encoding="utf-8")
    names = set(re.findall(r"^\s*([A-Z][A-Z0-9_]+):", text, re.M))
    env_example = (REPO / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", env_example, re.M))
    railway_only = {"PORT", "FLASK_RUN_PORT"}  # injected by the platform / Flask CLI only
    unknown = sorted(n for n in names - documented - railway_only)
    assert not unknown, f"variables in railway.ts that .env.example does not document: {unknown}"


# ── Dashboard reaches the registry through the documented variable ─────


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"REGISTRY_URL": "http://registry.railway.internal:8000"}, "http://registry.railway.internal:8000"),
        ({"REGISTRY_URL": "http://registry:8000", "API_BASE_URL": "http://legacy:1"}, "http://registry:8000"),
        ({"API_BASE_URL": "http://legacy:1"}, "http://legacy:1"),
        ({}, "http://localhost:8000"),
    ],
)
def test_dashboard_client_honours_registry_url(env, expected):
    """Every compose file and the Railway topology pass REGISTRY_URL to the
    dashboard; before Phase 4 the client silently read API_BASE_URL only, so
    the dashboard called localhost inside its own container."""
    clean = {k: v for k, v in os.environ.items() if k not in {"REGISTRY_URL", "API_BASE_URL"}}
    proc = subprocess.run(
        [sys.executable, "-c", "from app.api_client import ApiClient; print(ApiClient().base_url)"],
        cwd=REPO / "services/dashboard", env={**clean, **env}, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


def test_dashboard_readiness_never_echoes_the_registry_probe_error():
    """The dashboard is public on the platform; its /readyz must not name the
    private registry URL or any exception text (same rule as the registry's
    own readiness endpoint)."""
    text = (REPO / "services/dashboard/app/main.py").read_text(encoding="utf-8")
    assert '"error": str(e)' not in text
    clean = {k: v for k, v in os.environ.items() if k not in {"REGISTRY_URL", "API_BASE_URL", "ENVIRONMENT"}}
    code = (
        "from app.main import app; c = app.test_client(); r = c.get('/readyz'); "
        "print(r.status_code); print(r.get_data(as_text=True))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO / "services/dashboard",
        env={**clean, "REGISTRY_URL": "http://127.0.0.1:1/private-registry-host", "ENVIRONMENT": "development"},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    status, body = proc.stdout.strip().split("\n", 1)
    assert status == "503"
    assert "private-registry-host" not in body and "127.0.0.1" not in body and "error" not in body


def test_dashboard_pages_render_with_an_unreachable_registry():
    """With the registry down the public pages still render (no 500) and the
    API client uses the registry's /v1 routes.

    This test used to also REQUIRE inert ``href="#"`` anchors: Phase 4 papered
    over templates linking to removed pages with a BuildError fallback. That
    masking is the defect the public-surface contract now detects
    (docs/PUBLIC_SURFACE_CONTRACT.md); pinning it here would reject a correct
    repair. Whether links resolve is judged by
    services/dashboard/tests/test_public_surface.py, not by this test."""
    text = (REPO / "services/dashboard/app/api_client.py").read_text(encoding="utf-8")
    assert '"/v1/agents/public/"' in text and 'f"/v1/agents/{agent_id}"' in text
    clean = {k: v for k, v in os.environ.items() if k not in {"REGISTRY_URL", "API_BASE_URL", "ENVIRONMENT"}}
    code = (
        "from app.main import app; c = app.test_client(); "
        "r1 = c.get('/landing'); r2 = c.get('/'); r3 = c.get('/metaverse'); "
        "print(r1.status_code, r2.status_code, r2.headers.get('Location', ''), r3.status_code, "
        "'Internal Server Error' in r3.get_data(as_text=True))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO / "services/dashboard",
        env={**clean, "REGISTRY_URL": "http://127.0.0.1:1", "ENVIRONMENT": "development"},
        capture_output=True, text=True, timeout=90,
    )
    assert proc.returncode == 0, proc.stderr
    landing, index, location, metaverse, has_500_text = proc.stdout.split()
    assert landing == "200" and index == "302" and "/metaverse" in location
    assert metaverse == "200" and has_500_text == "False"


def test_dashboard_metaverse_renders_the_registry_public_listing():
    """The registry's public listing is a bare JSON array (``List[AgentPublic]``).
    On staging the dashboard answered 200 but logged ``'list' object has no
    attribute 'get'`` and rendered the error flash instead of the fleet; the
    client now accepts both the array and a ``{"agents": [...]}`` envelope."""
    clean = {k: v for k, v in os.environ.items() if k not in {"REGISTRY_URL", "API_BASE_URL", "ENVIRONMENT"}}
    code = (
        "import json, threading\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "AGENT = {'id': 'a1', 'name': 'Society_Scout', 'description': 'scout', 'capabilities': ['recon'],\n"
        "         'success_rate': 0.5, 'total_tasks_completed': 1, 'total_tasks_failed': 0, 'total_tasks_timeout': 0,\n"
        "         'reputation_tier': 'bronze'}\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps([AGENT] if self.path.startswith('/v1/agents/public/') else {}).encode()\n"
        "        self.send_response(200); self.send_header('content-type', 'application/json'); self.end_headers(); self.wfile.write(body)\n"
        "    def log_message(self, *a): pass\n"
        "srv = HTTPServer(('127.0.0.1', 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
        "import os; os.environ['REGISTRY_URL'] = 'http://127.0.0.1:%d' % srv.server_port\n"
        "from app.main import app\n"
        "from app.api_client import api_client\n"
        "print(api_client.fetch_agents()[0]['name'])\n"
        "r = app.test_client().get('/metaverse'); body = r.get_data(as_text=True)\n"
        "print(r.status_code, 'Society_Scout' in body, 'An error occurred while loading the command center' in body)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO / "services/dashboard",
        env={**clean, "ENVIRONMENT": "development"},
        capture_output=True, text=True, timeout=90,
    )
    assert proc.returncode == 0, proc.stderr
    first_name, status, has_agent, has_error_flash = proc.stdout.split()
    assert first_name == "Society_Scout"
    assert status == "200" and has_agent == "True" and has_error_flash == "False"
