"""Phase 3.1 — GitHub credential boundary and argv-safe git authentication.

No real GitHub, no real App: a fake token endpoint, a generated RSA key, a
recording git runner and a local git smart-HTTP server with Basic auth.
"""

from __future__ import annotations

import base64
import json
import pathlib
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from services.registry.app.society import github_credentials as gc
from services.registry.app.society.config import SocietyConfigError, SocietySettings, reset_settings_cache
from services.registry.app.society.promotion import ProviderConflict, ProviderRefused, ProviderTransient, ProviderUnavailable
from services.registry.app.society.promotion_github import ASKPASS_SCRIPT, ASKPASS_TOKEN_ENV, ASKPASS_USER_ENV, GitHubPromotionProvider, push_base_url
from .git_http_harness import GitHTTPServer

TOKEN = "installation-token-SENTINEL-7f3a"   # any shape; never assume length/prefix
T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


# ── helpers ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def rsa_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    return key, pem


def _verify_jwt(token: str, public_key) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    h, p, s = token.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    public_key.verify(base64.urlsafe_b64decode(pad(s)), f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
    header = json.loads(base64.urlsafe_b64decode(pad(h)))
    assert header == {"alg": "RS256", "typ": "JWT"}
    return json.loads(base64.urlsafe_b64decode(pad(p)))


class FakeTokenEndpoint:
    """Records every call; scripted per-call responses."""

    def __init__(self, responses=None, *, delay: float = 0.0):
        self.responses = list(responses or [])
        self.calls = []
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout):
        with self.lock:
            self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
            nxt = self.responses.pop(0) if self.responses else (201, {"token": TOKEN, "expires_at": (T0 + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        if self.delay:
            time.sleep(self.delay)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def app_settings(monkeypatch, tmp_path, pem: bytes, *, key_in_env: bool = False, repo: str = "owner/repo") -> SocietySettings:
    monkeypatch.setenv("SOCIETY_GITHUB_CREDENTIAL_PROVIDER", "app")
    monkeypatch.setenv("SOCIETY_GITHUB_APP_ID", "12345")
    monkeypatch.setenv("SOCIETY_GITHUB_INSTALLATION_ID", "67890")
    monkeypatch.setenv("SOCIETY_GITHUB_REPOSITORY", repo)
    if key_in_env:
        monkeypatch.delenv("SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE", raising=False)
        monkeypatch.setenv("SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM", pem.decode())
    else:
        keyfile = tmp_path / "app.pem"
        keyfile.write_bytes(pem)
        keyfile.chmod(0o600)
        monkeypatch.setenv("SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE", str(keyfile))
        monkeypatch.delenv("SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM", raising=False)
    reset_settings_cache()
    return SocietySettings()


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


# ── configuration ──────────────────────────────────────────────────────


def test_default_is_disabled_and_inert(monkeypatch):
    for name in ("SOCIETY_GITHUB_CREDENTIAL_PROVIDER", "SOCIETY_GITHUB_TOKEN", "SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM"):
        monkeypatch.delenv(name, raising=False)
    reset_settings_cache()
    s = SocietySettings()
    provider = gc.get_credential_provider(s)
    assert provider.name == "disabled"
    with pytest.raises(gc.CredentialUnavailable):
        provider.get()
    reset_settings_cache()


def test_unknown_credential_provider_fails_closed_to_disabled(monkeypatch):
    monkeypatch.setenv("SOCIETY_GITHUB_CREDENTIAL_PROVIDER", "magic")
    reset_settings_cache()
    assert SocietySettings().github_credential_provider == "disabled"
    reset_settings_cache()


def test_app_provider_requires_identifiers_and_a_key_path_not_key_material(monkeypatch, tmp_path, rsa_key):
    _, pem = rsa_key
    monkeypatch.setenv("SOCIETY_GITHUB_CREDENTIAL_PROVIDER", "app")
    monkeypatch.delenv("SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM", raising=False)
    reset_settings_cache()
    with pytest.raises(SocietyConfigError, match="SOCIETY_GITHUB_APP_ID"):
        SocietySettings()
    monkeypatch.setenv("SOCIETY_GITHUB_APP_ID", "1")
    monkeypatch.setenv("SOCIETY_GITHUB_INSTALLATION_ID", "2")
    reset_settings_cache()
    with pytest.raises(SocietyConfigError, match="PRIVATE_KEY_FILE"):
        SocietySettings()
    monkeypatch.setenv("SOCIETY_GITHUB_APP_PRIVATE_KEY_FILE", pem.decode())  # key material where a PATH belongs
    reset_settings_cache()
    with pytest.raises(SocietyConfigError, match="file path"):
        SocietySettings()
    reset_settings_cache()


def test_static_provider_reads_env_at_call_time_and_caches_nothing(monkeypatch):
    monkeypatch.delenv("SOCIETY_GITHUB_TOKEN", raising=False)
    p = gc.StaticTokenCredentialProvider()
    with pytest.raises(gc.CredentialUnavailable):
        p.get()
    monkeypatch.setenv("SOCIETY_GITHUB_TOKEN", TOKEN)
    cred = p.get()
    assert cred.token == TOKEN and cred.username == "x-access-token" and cred.expires_at is None
    assert TOKEN not in repr(cred) and TOKEN not in str(cred) and TOKEN not in repr(p)
    monkeypatch.setenv("SOCIETY_GITHUB_TOKEN", "rotated")
    assert p.get().token == "rotated"


# ── App token lifecycle ────────────────────────────────────────────────


def test_app_provider_mints_a_valid_jwt_and_downscoped_token(monkeypatch, tmp_path, rsa_key):
    key, pem = rsa_key
    s = app_settings(monkeypatch, tmp_path, pem)
    endpoint = FakeTokenEndpoint()
    clock = Clock()
    p = gc.GitHubAppCredentialProvider(s, transport=endpoint, clock=clock)
    cred = p.get()
    assert cred.token == TOKEN and cred.source == "app" and cred.expires_at == T0 + timedelta(hours=1)
    call = endpoint.calls[0]
    assert call["method"] == "POST" and call["url"] == "https://api.github.com/app/installations/67890/access_tokens"
    assert call["headers"]["Accept"] == "application/vnd.github+json" and call["headers"]["X-GitHub-Api-Version"]
    jwt = call["headers"]["Authorization"].removeprefix("Bearer ")
    claims = _verify_jwt(jwt, key.public_key())
    assert claims["iss"] == "12345"
    assert claims["iat"] == int(T0.timestamp()) - 60, "iat 60 s in the past (clock drift)"
    assert claims["exp"] - claims["iat"] <= 10 * 60 and claims["exp"] > int(T0.timestamp())
    assert call["body"] == {"permissions": gc.MINIMUM_PERMISSIONS, "repositories": ["repo"]}
    assert set(gc.MINIMUM_PERMISSIONS) == {"contents", "pull_requests", "checks", "metadata"}
    assert p.stats == {"minted": 1, "reused": 0, "invalidations": 0, "cached": 1}
    assert TOKEN not in repr(p) and "PRIVATE" not in repr(p) and pem.decode()[:30] not in repr(p)
    reset_settings_cache()


def test_private_key_may_come_from_env_only_as_fallback(monkeypatch, tmp_path, rsa_key):
    key, pem = rsa_key
    s = app_settings(monkeypatch, tmp_path, pem, key_in_env=True)
    endpoint = FakeTokenEndpoint()
    p = gc.GitHubAppCredentialProvider(s, transport=endpoint, clock=Clock())
    assert p.get().token == TOKEN
    _verify_jwt(endpoint.calls[0]["headers"]["Authorization"].removeprefix("Bearer "), key.public_key())
    reset_settings_cache()


def test_unreadable_or_non_pem_key_fails_closed_without_leaking(monkeypatch, tmp_path, rsa_key):
    _, pem = rsa_key
    s = app_settings(monkeypatch, tmp_path, pem)
    pathlib.Path(s.github_app_private_key_file).unlink()
    p = gc.GitHubAppCredentialProvider(s, transport=FakeTokenEndpoint(), clock=Clock())
    with pytest.raises(gc.CredentialUnavailable) as exc:
        p.get()
    assert "not readable" in str(exc.value) and str(tmp_path) not in str(exc.value)
    pathlib.Path(s.github_app_private_key_file).write_text("garbage")
    with pytest.raises(gc.CredentialUnavailable):
        p.get()
    reset_settings_cache()


def test_cached_token_is_reused_then_refreshed_before_expiry_and_after(monkeypatch, tmp_path, rsa_key):
    _, pem = rsa_key
    s = app_settings(monkeypatch, tmp_path, pem)
    endpoint = FakeTokenEndpoint()
    clock = Clock()
    p = gc.GitHubAppCredentialProvider(s, transport=endpoint, clock=clock, refresh_margin_seconds=300)
    first = p.get()
    for _ in range(5):
        assert p.get() is first
    assert p.stats["minted"] == 1 and p.stats["reused"] == 5
    clock.advance(minutes=54)              # 6 min left: still fresh (margin 5 min)
    assert p.get() is first
    clock.advance(minutes=1, seconds=1)    # < 5 min left: refresh BEFORE expiry
    second = p.get()
    assert second is not first and p.stats["minted"] == 2
    assert second.expires_at == T0 + timedelta(hours=1), "fake endpoint answers with the same expiry; the point is the refresh"
    clock.advance(hours=2)                 # expired: refresh again
    p.get()
    assert p.stats["minted"] == 3
    reset_settings_cache()


def test_missing_expiry_is_treated_as_short_lived(monkeypatch, tmp_path, rsa_key):
    _, pem = rsa_key
    s = app_settings(monkeypatch, tmp_path, pem)
    endpoint = FakeTokenEndpoint([(201, {"token": TOKEN}), (201, {"token": TOKEN, "expires_at": "not-a-date"})])
    clock = Clock()
    p = gc.GitHubAppCredentialProvider(s, transport=endpoint, clock=clock, refresh_margin_seconds=60)
    cred = p.get()
    assert cred.expires_at == T0 + timedelta(minutes=5)
    clock.advance(minutes=4, seconds=30)
    p.get()
    assert p.stats["minted"] == 2
    reset_settings_cache()


@pytest.mark.parametrize("status,exc", [(401, gc.CredentialRefused), (403, gc.CredentialRefused), (404, gc.CredentialRefused), (422, gc.CredentialRefused), (429, gc.CredentialTransient), (500, gc.CredentialTransient), (503, gc.CredentialTransient)])
def test_token_endpoint_errors_fail_closed_without_caching(monkeypatch, tmp_path, rsa_key, status, exc):
    _, pem = rsa_key
    s = app_settings(monkeypatch, tmp_path, pem)
    endpoint = FakeTokenEndpoint([(status, {"message": "nope", "token": "should-never-be-used"})])
    p = gc.GitHubAppCredentialProvider(s, transport=endpoint, clock=Clock())
    with pytest.raises(exc) as info:
        p.get()
    assert "should-never-be-used" not in str(info.value) and "nope" not in str(info.value)
    assert p.stats["cached"] == 0
    assert p.get().token == TOKEN, "the next call mints normally"
    reset_settings_cache()


def test_transport_timeout_is_transient_and_never_echoes_the_jwt(monkeypatch, tmp_path, rsa_key):
    _, pem = rsa_key
    s = app_settings(monkeypatch, tmp_path, pem)
    endpoint = FakeTokenEndpoint([TimeoutError("boom secret-looking-text")])
    p = gc.GitHubAppCredentialProvider(s, transport=endpoint, clock=Clock())
    with pytest.raises(gc.CredentialTransient) as info:
        p.get()
    assert "TimeoutError" in str(info.value) and "secret-looking-text" not in str(info.value)
    reset_settings_cache()


def test_invalidate_forces_a_fresh_mint(monkeypatch, tmp_path, rsa_key):
    _, pem = rsa_key
    s = app_settings(monkeypatch, tmp_path, pem)
    p = gc.GitHubAppCredentialProvider(s, transport=FakeTokenEndpoint(), clock=Clock())
    p.get()
    p.invalidate()
    p.get()
    assert p.stats["minted"] == 2 and p.stats["invalidations"] == 1
    reset_settings_cache()


def test_concurrent_callers_do_not_mint_a_token_storm(monkeypatch, tmp_path, rsa_key):
    _, pem = rsa_key
    s = app_settings(monkeypatch, tmp_path, pem)
    endpoint = FakeTokenEndpoint(delay=0.2)
    p = gc.GitHubAppCredentialProvider(s, transport=endpoint, clock=Clock())
    results, errors = [], []

    def worker():
        try:
            results.append(p.get().token)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors and len(results) == 12 and set(results) == {TOKEN}
    assert len(endpoint.calls) == 1 and p.stats["minted"] == 1 and p.stats["reused"] == 11
    reset_settings_cache()


# ── provider: API 401 handling through the credential boundary ─────────


class FakeCreds:
    name = "fake"

    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.gets = 0
        self.invalidations = 0

    def get(self):
        self.gets += 1
        if not self.tokens:
            raise gc.CredentialUnavailable("exhausted")
        return gc.Credential(token=self.tokens[0], expires_at=None, source="fake")

    def invalidate(self):
        self.invalidations += 1
        if self.tokens:
            self.tokens.pop(0)


def gh_settings(monkeypatch, api_url="https://api.github.com"):
    monkeypatch.setenv("SOCIETY_GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("SOCIETY_GITHUB_API_URL", api_url)
    monkeypatch.setenv("SOCIETY_PROMOTION_PROVIDER", "github")
    monkeypatch.delenv("SOCIETY_GITHUB_CREDENTIAL_PROVIDER", raising=False)
    reset_settings_cache()
    return SocietySettings()


def test_api_401_invalidates_and_retries_exactly_once(monkeypatch):
    s = gh_settings(monkeypatch)
    answers = [(401, {"message": "Bad credentials"}), (200, {"number": 7, "head": {"sha": "abc"}, "base": {"sha": "b"}, "state": "open"})]
    calls = []

    def transport(method, url, payload):
        calls.append((method, url))
        if answers:
            return answers.pop(0)
        if url.endswith("/reviews"):
            return (200, [])
        if "/check-runs" in url:
            return (200, {"check_runs": []})
        return (200, {"commit": {"sha": "b"}})

    creds = FakeCreds(["old", "new"])
    p = GitHubPromotionProvider(s, transport=transport, credentials=creds)
    state = p.get_pr_state(type("P", (), {"external_pr_number": 7})())
    assert state.head_sha == "abc"
    assert creds.invalidations == 1 and creds.gets >= 2
    assert calls[0] == calls[1], "the same request is retried once with a fresh credential"

    answers = [(401, {}), (401, {})]
    creds = FakeCreds(["a", "b", "c"])
    p = GitHubPromotionProvider(s, transport=transport, credentials=creds)
    with pytest.raises(ProviderRefused):
        p.get_pr_state(type("P", (), {"external_pr_number": 7})())
    assert creds.invalidations == 1, "repeated 401 fails closed after ONE refresh"
    reset_settings_cache()


@pytest.mark.parametrize("status,exc", [(403, ProviderRefused), (429, ProviderTransient), (502, ProviderTransient), (422, ProviderRefused)])
def test_api_errors_map_without_retry_storm(monkeypatch, status, exc):
    s = gh_settings(monkeypatch)
    calls = []
    p = GitHubPromotionProvider(s, transport=lambda m, u, j: (calls.append(u), (status, {"message": "x"}))[1], credentials=FakeCreds(["t"]))
    with pytest.raises(exc):
        p.get_pr_state(type("P", (), {"external_pr_number": 7})())
    assert len(calls) == 1
    reset_settings_cache()


def test_credential_errors_map_to_provider_errors(monkeypatch):
    s = gh_settings(monkeypatch)
    for err, expected in ((gc.CredentialUnavailable("x"), ProviderUnavailable), (gc.CredentialRefused("x"), ProviderRefused), (gc.CredentialTransient("x"), ProviderTransient)):
        class Boom:
            name = "boom"

            def get(self, _e=err):
                raise _e

            def invalidate(self):
                return None

        p = GitHubPromotionProvider(s, transport=lambda m, u, j: (200, {}), credentials=Boom())
        with pytest.raises(expected):
            p.get_pr_state(type("P", (), {"external_pr_number": 7})())
    reset_settings_cache()


# ── git push: argv/URL/config safety with a recording runner ───────────


class Rec:
    def __init__(self, returncode=0, stderr="", raise_exc=None):
        self.calls = []
        self.returncode, self.stderr, self.raise_exc = returncode, stderr, raise_exc
        self.helper_content_seen = None
        self.helper_path_seen = None

    def __call__(self, argv, cwd, env, timeout):
        helper = pathlib.Path(env["GIT_ASKPASS"])
        self.helper_path_seen = helper
        self.helper_content_seen = helper.read_text()
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "timeout": timeout, "helper_mode": oct(helper.stat().st_mode & 0o777)})
        if self.raise_exc:
            raise self.raise_exc
        return subprocess.CompletedProcess(list(argv), self.returncode, stdout="", stderr=self.stderr)


def _cand(branch="agentnet-auto/abc", sha="deadbeef"):
    return type("C", (), {"branch_name": branch, "head_sha": sha})()


def _ws(tmp_path):
    return type("W", (), {"path": tmp_path})()


def test_push_keeps_the_token_out_of_argv_url_and_config(monkeypatch, tmp_path):
    s = gh_settings(monkeypatch)
    rec = Rec()
    p = GitHubPromotionProvider(s, transport=lambda m, u, j: (404, {}), credentials=FakeCreds([TOKEN]), git_runner=rec)
    ref = p.publish_branch(None, _cand(), _ws(tmp_path))
    assert ref.created and ref.name == "agentnet-auto/abc"
    call = rec.calls[0]
    argv = call["argv"]
    assert TOKEN not in " ".join(argv) and "x-access-token" not in " ".join(argv)
    assert argv[:3] == ["git", "-c", "credential.helper="] and "push" in argv
    assert "https://github.com/owner/repo.git" in argv and "HEAD:refs/heads/agentnet-auto/abc" in argv
    assert not any(a in ("--force", "-f", "--force-with-lease", "--mirror", "--delete") or a.startswith("+") for a in argv)
    env = call["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env[ASKPASS_USER_ENV] == "x-access-token" and env[ASKPASS_TOKEN_ENV] == TOKEN, "the ONLY channel is the child environment"
    assert TOKEN not in rec.helper_content_seen and rec.helper_content_seen == ASKPASS_SCRIPT
    assert call["helper_mode"] == "0o700"
    assert not rec.helper_path_seen.exists() and not rec.helper_path_seen.parent.exists(), "helper removed in finally"
    assert TOKEN not in json.dumps(p.last_push_argv) and TOKEN not in repr(p)
    reset_settings_cache()


def test_push_refuses_main_master_and_foreign_prefix_before_any_git_call(monkeypatch, tmp_path):
    s = gh_settings(monkeypatch)
    rec = Rec()
    p = GitHubPromotionProvider(s, transport=lambda m, u, j: (404, {}), credentials=FakeCreds([TOKEN]), git_runner=rec)
    for bad in ("main", "master", "refs/heads/main", "feature/x", "agentnet-auto", "", "someone/main"):
        with pytest.raises(ProviderRefused):
            p.publish_branch(None, _cand(branch=bad), _ws(tmp_path))
    assert rec.calls == []
    reset_settings_cache()


def test_push_is_idempotent_and_never_forces_over_different_history(monkeypatch, tmp_path):
    s = gh_settings(monkeypatch)
    rec = Rec()
    same = lambda m, u, j: (200, {"commit": {"sha": "deadbeef"}})  # noqa: E731
    p = GitHubPromotionProvider(s, transport=same, credentials=FakeCreds([TOKEN]), git_runner=rec)
    ref = p.publish_branch(None, _cand(), _ws(tmp_path))
    assert not ref.created and rec.calls == []
    other = lambda m, u, j: (200, {"commit": {"sha": "0ther"}})  # noqa: E731
    p = GitHubPromotionProvider(s, transport=other, credentials=FakeCreds([TOKEN]), git_runner=rec)
    with pytest.raises(ProviderConflict):
        p.publish_branch(None, _cand(), _ws(tmp_path))
    assert rec.calls == []
    reset_settings_cache()


def test_push_failures_are_classified_scrubbed_and_cleaned_up(monkeypatch, tmp_path):
    s = gh_settings(monkeypatch)
    notfound = lambda m, u, j: (404, {})  # noqa: E731
    rec = Rec(returncode=1, stderr=f"! [rejected] non-fast-forward for https://x-access-token:{TOKEN}@github.com")
    p = GitHubPromotionProvider(s, transport=notfound, credentials=FakeCreds([TOKEN]), git_runner=rec)
    with pytest.raises(ProviderConflict) as info:
        p.publish_branch(None, _cand(), _ws(tmp_path))
    assert TOKEN not in str(info.value) and not rec.helper_path_seen.parent.exists()

    creds = FakeCreds([TOKEN, "second"])
    rec = Rec(returncode=128, stderr="fatal: Authentication failed for 'https://github.com/owner/repo.git/'")
    p = GitHubPromotionProvider(s, transport=notfound, credentials=creds, git_runner=rec)
    with pytest.raises(ProviderRefused):
        p.publish_branch(None, _cand(), _ws(tmp_path))
    assert creds.invalidations == 1

    rec = Rec(raise_exc=subprocess.TimeoutExpired(cmd="git", timeout=1))
    p = GitHubPromotionProvider(s, transport=notfound, credentials=FakeCreds([TOKEN]), git_runner=rec)
    with pytest.raises(ProviderTransient):
        p.publish_branch(None, _cand(), _ws(tmp_path))
    assert not rec.helper_path_seen.parent.exists(), "helper removed even when git raised"

    rec = Rec(returncode=1, stderr="error: RPC failed; HTTP 502")
    p = GitHubPromotionProvider(s, transport=notfound, credentials=FakeCreds([TOKEN]), git_runner=rec)
    with pytest.raises(ProviderTransient):
        p.publish_branch(None, _cand(), _ws(tmp_path))
    reset_settings_cache()


def test_push_base_url_derivation():
    assert push_base_url("https://api.github.com") == "https://github.com"
    assert push_base_url("https://ghe.example/api/v3/") == "https://ghe.example"
    assert push_base_url("http://127.0.0.1:9") == "http://127.0.0.1:9"


# ── real git push over HTTP through GIT_ASKPASS (local harness) ────────


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def local_git(tmp_path):
    root = tmp_path / "srv"
    bare = root / "owner" / "repo.git"
    bare.mkdir(parents=True)
    _git(["init", "-q", "--bare", "-b", "main", str(bare)], tmp_path)
    _git(["config", "http.receivepack", "true"], bare)
    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "-q", "-b", "main"], work)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base"], work)
    _git(["checkout", "-q", "-b", "agentnet-auto/cand1"], work)
    (work / "fix.txt").write_text("fix\n")
    _git(["add", "-A"], work)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "candidate"], work)
    return root, bare, work


@pytest.mark.timeout(120)
def test_real_git_push_authenticates_through_askpass_only(monkeypatch, tmp_path, local_git):
    root, bare, work = local_git
    head = _git(["rev-parse", "HEAD"], work)
    with GitHTTPServer(root, username="x-access-token", token=TOKEN) as server:
        s = gh_settings(monkeypatch, api_url=server.base_url)
        monkeypatch.setenv("SOCIETY_GITHUB_TOKEN", TOKEN)
        creds = gc.StaticTokenCredentialProvider()
        seen = {}

        def transport(method, url, payload):  # fake REST API: branch absent on first publish
            seen.setdefault("urls", []).append(url)
            return (404, {})

        p = GitHubPromotionProvider(s, transport=transport, credentials=creds)
        ws = type("W", (), {"path": work})()
        ref = p.publish_branch(None, _cand(branch="agentnet-auto/cand1", sha=head), ws)
        assert ref.created and ref.sha == head
        assert _git(["rev-parse", "refs/heads/agentnet-auto/cand1"], bare) == head, "the branch arrived on the remote"
        assert "none" in server.auth_attempts and "ok" in server.auth_attempts and "bad" not in server.auth_attempts
        assert all(TOKEN not in path for path in server.paths), "token never in a URL"
        assert TOKEN not in " ".join(p.last_push_argv)
        cfg = _git(["config", "--list", "--show-origin"], work)
        assert TOKEN not in cfg and "askpass" not in cfg.lower() and "credential.helper" not in cfg
        assert TOKEN not in (work / ".git" / "config").read_text()
        assert not (pathlib.Path.home() / ".git-credentials").exists() or TOKEN not in (pathlib.Path.home() / ".git-credentials").read_text()
        assert not list(pathlib.Path(tempfile_dir()).glob("agentnet-askpass-*")), "no helper directory left behind"

        # a diverged remote branch is never overwritten (API stale -> git refuses non-fast-forward)
        other = tmp_path / "other"
        _git(["clone", "-q", str(bare), str(other)], tmp_path)
        _git(["checkout", "-q", "-b", "agentnet-auto/cand1", "origin/agentnet-auto/cand1~1"], other)  # branch from the base commit
        _git(["-c", "user.name=o", "-c", "user.email=o@o", "commit", "-q", "--allow-empty", "-m", "diverged"], other)
        _git(["push", "-q", "--force", "origin", "agentnet-auto/cand1"], other)  # test setup only
        diverged = _git(["rev-parse", "refs/heads/agentnet-auto/cand1"], bare)
        assert diverged != head
        with pytest.raises(ProviderConflict):
            p.publish_branch(None, _cand(branch="agentnet-auto/cand1", sha=head), ws)
        assert _git(["rev-parse", "refs/heads/agentnet-auto/cand1"], bare) == diverged, "no force push"

        # wrong credential -> refused, not silently retried forever
        monkeypatch.setenv("SOCIETY_GITHUB_TOKEN", "wrong")
        _git(["checkout", "-q", "-b", "agentnet-auto/cand2"], work)
        with pytest.raises(ProviderRefused):
            p.publish_branch(None, _cand(branch="agentnet-auto/cand2", sha=head), ws)
        assert "bad" in server.auth_attempts
    reset_settings_cache()


def tempfile_dir():
    import tempfile

    return tempfile.gettempdir()


def test_promotion_controller_uses_the_github_provider_only_through_the_factory(monkeypatch):
    """The Society worker constructs the provider from settings; the model
    never receives a provider or credential object (structural)."""
    from services.registry.app.society import promotion as pm

    s = gh_settings(monkeypatch)
    prov = pm.get_promotion_provider(s)
    assert isinstance(prov, GitHubPromotionProvider) and prov._credentials.name == "disabled"
    with pytest.raises(ProviderUnavailable):
        prov.get_pr_state(type("P", (), {"external_pr_number": 1})())
    src = pathlib.Path(__file__).resolve().parent.parent.parent / "services/registry/app/society"
    for name in ("cognition.py", "context.py", "executor.py", "policy.py"):
        text = (src / name).read_text()
        assert "promotion_github" not in text and "github_credentials" not in text, name
    reset_settings_cache()

