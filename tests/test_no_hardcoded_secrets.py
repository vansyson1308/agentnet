"""
Secret-hygiene regression test.

INVARIANT: no runtime credential ships as a literal in this repository.
A previous audit found a provider API key and account passwords committed
as *default values* in agent scripts (``agents/*.py``, ``legacy/hermes/*``).
Those were replaced with environment lookups. This test keeps them out.

The scan is deliberately narrow and deterministic (no third-party scanner
required in CI) and complements ggshield / detect-secrets configured in
``.pre-commit-config.yaml``:

1. Provider-style API keys (``sk-...``) anywhere in tracked text files.
2. ``PASSWORD = "<literal>"`` / ``"password": "<literal>"`` assignments in
   Python outside tests, where the literal is not an obvious placeholder.
3. ``os.environ.get("<SECRET_NAME>", "<non-empty default>")`` — a secret
   must never have a non-empty fallback baked into source.
4. ``.gitignore`` must exclude ``.env`` and ``.env.github_token`` on their
   own lines (a previous version glued them into one unusable line).
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

SCAN_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".txt", ".md", ".js", ".jsx"}
SKIP_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__", "werewolf_data", "demo"}

PLACEHOLDER_HINTS = (
    "your_",
    "change_me",
    "changeme",
    "placeholder",
    "example",
    "xxxx",
    "<",
    "${",
    "dev-only",
    "test",
    "demo",
    "secret",  # e.g. "ci-test-secret-key..." in CI config
    "password",  # e.g. "password" used as literal field name/value in tests
)

SECRET_ENV_NAMES = re.compile(
    r"os\.(?:environ\.get|getenv)\(\s*['\"]([A-Z0-9_]*(?:PASSWORD|API_KEY|SECRET|TOKEN)[A-Z0-9_]*)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)"
)
API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")
PASSWORD_LITERAL_RE = re.compile(
    r"""(?:PASSWORD\s*=\s*|["']password["']\s*:\s*)["']([^"']{8,})["']""",
    re.IGNORECASE,
)


def _tracked_files() -> list[pathlib.Path]:
    """Prefer git's view of tracked files so ignored local files never fail the test."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO,
            capture_output=True,
            check=True,
        ).stdout
        names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
        files = [REPO / n for n in names]
    except Exception:  # pragma: no cover — fallback outside a git checkout
        files = [p for p in REPO.rglob("*") if p.is_file()]
    result = []
    for p in files:
        if any(part in SKIP_PARTS for part in p.parts):
            continue
        if p.suffix.lower() in SCAN_SUFFIXES or p.name.startswith(".env"):
            result.append(p)
    return result


def _read(p: pathlib.Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _is_placeholder(value: str) -> bool:
    v = value.lower()
    return any(h in v for h in PLACEHOLDER_HINTS)


def test_no_provider_api_keys_in_tracked_files():
    offenders = []
    for p in _tracked_files():
        if p.name == pathlib.Path(__file__).name:
            continue
        for m in API_KEY_RE.finditer(_read(p)):
            offenders.append(f"{p.relative_to(REPO)}: {m.group(0)[:6]}…")
    assert not offenders, "provider API key literal(s) committed: " + ", ".join(offenders)


def test_no_password_literals_in_python_sources():
    offenders = []
    for p in _tracked_files():
        if p.suffix != ".py" or p.parts[len(REPO.parts)] == "tests":
            continue
        for m in PASSWORD_LITERAL_RE.finditer(_read(p)):
            if not _is_placeholder(m.group(1)):
                offenders.append(f"{p.relative_to(REPO)}")
                break
    assert not offenders, "password literal(s) in source: " + ", ".join(offenders)


def test_secret_env_lookups_have_no_baked_in_default():
    offenders = []
    for p in _tracked_files():
        if p.suffix != ".py" or p.parts[len(REPO.parts)] == "tests":
            continue
        for m in SECRET_ENV_NAMES.finditer(_read(p)):
            if not _is_placeholder(m.group(2)):
                offenders.append(f"{p.relative_to(REPO)}: {m.group(1)}")
    assert not offenders, "secret env lookups with non-empty defaults: " + ", ".join(offenders)


@pytest.mark.parametrize("entry", [".env", ".env.github_token"])
def test_gitignore_excludes_env_files(entry):
    lines = [ln.strip() for ln in (REPO / ".gitignore").read_text().splitlines()]
    assert entry in lines, f"{entry!r} must be on its own line in .gitignore"


def test_sample_agents_require_password_from_env():
    for rel in ("agents/echo_agent.py", "agents/poll_agent.py"):
        src = _read(REPO / rel)
        assert 'os.getenv("AGENT_PASSWORD"' in src, rel
        assert re.search(r'AGENT_PASSWORD\s*=\s*"[^"]+"', src) is None, f"{rel} still hard-codes a password"
