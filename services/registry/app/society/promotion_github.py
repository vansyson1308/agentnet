"""GitHub implementation of the PromotionProvider — INERT without configuration.

Secret boundary (Phase 3.1): this module never reads a credential from the
environment itself. It asks a ``GitHubCredentialProvider``
(``society/github_credentials.py``: ``disabled`` by default, ``static`` for
operators/tests, ``app`` for a future Society GitHub App) for a short-lived
credential at call time and never stores it on the provider object, the
settings object, a run/context/event row or a log line. Cognition, context
assembly and the controller import nothing from here
(``tests/society/test_secret_boundary.py``).

``git push`` authentication (gitcredentials(7)): the remote URL contains NO
credential, the token is NOT in argv and NOT in ``.git/config``. A temporary
``GIT_ASKPASS`` helper (no secret embedded; mode 0700; removed in ``finally``)
answers git's username/password prompts from two environment variables that
exist only in the child process. Credential helpers are disabled for the
call (``-c credential.helper=``) so nothing is ever persisted, prompts are
disabled (``GIT_TERMINAL_PROMPT=0``) and the command runs without a shell.

What it does when configured (not exercised against GitHub in this phase —
no App, no token):

* ``publish_branch`` — refuses the base branch and any branch outside the
  autonomous prefix; checks the remote branch via the REST API; pushes the
  candidate worktree's HEAD to ``refs/heads/<branch>`` (NO force, NO
  ``+refspec``); a remote branch with different history is a conflict, never
  overwritten;
* ``open_or_update_pr`` — finds an existing PR by head branch and updates
  it, otherwise creates a DRAFT PR whose body is generated from facts;
* ``get_pr_state`` — merges PR metadata, check-run conclusions and reviews
  into ``PRState``; ``base_moved`` compares the PR's recorded base sha with
  the base branch head;
* ``merge`` — refused unless ``SOCIETY_AUTO_MERGE_ENABLED`` (which config
  validation rejects for this provider in this phase) and even then only
  with the expected head sha (``sha`` parameter of the merge endpoint).

A 401 from the API invalidates the cached credential and retries exactly once
with a fresh one; a second 401 fails closed. Exceptions and logs carry status
codes and scrubbed stderr, never tokens, JWTs, keys or response bodies.

Required future GitHub App permissions (docs/GITHUB_PROMOTION.md):
Contents: read & write (branch push), Pull requests: read & write, Metadata:
read (implicit), Checks: read (CI state). NOT Administration, NOT Issues,
NOT Workflows, NOT Secrets, NOT Actions write.
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence

from .config import SocietySettings
from .engineering import workspace as ws_mod
from .github_credentials import (
    CredentialRefused,
    CredentialTransient,
    CredentialUnavailable,
    GitHubCredentialProvider,
    get_credential_provider,
    redact,
)
from .promotion import BranchRef, PRRef, PRState, ProviderConflict, ProviderRefused, ProviderTransient, ProviderUnavailable

logger = logging.getLogger(__name__)

_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
ASKPASS_USER_ENV = "AGENTNET_GIT_ASKPASS_USER"
ASKPASS_TOKEN_ENV = "AGENTNET_GIT_ASKPASS_TOKEN"
# The helper embeds NO secret: it only echoes the child-process environment.
ASKPASS_SCRIPT = (
    "#!/bin/sh\n"
    "# AgentNet promotion controller: GIT_ASKPASS helper (temporary, no secret embedded).\n"
    'case "$1" in\n'
    '  *sername*) printf \'%s\\n\' "$' + ASKPASS_USER_ENV + '" ;;\n'
    '  *) printf \'%s\\n\' "$' + ASKPASS_TOKEN_ENV + '" ;;\n'
    "esac\n"
)
_FORBIDDEN_PUSH_FLAGS = ("--force", "-f", "--force-with-lease", "--mirror", "--delete", "--prune")

GitRunner = Callable[[Sequence[str], str, Dict[str, str], float], "subprocess.CompletedProcess[str]"]


def _default_git_runner(argv: Sequence[str], cwd: str, env: Dict[str, str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)


def push_base_url(api_url: str) -> str:
    """Derive the git HTTPS base from the API URL: api.github.com -> github.com;
    GHES ``https://host/api/v3`` -> ``https://host``; anything else (a local
    test harness) is used as-is."""
    api = api_url.rstrip("/")
    if api == "https://api.github.com":
        return "https://github.com"
    if api.endswith("/api/v3"):
        return api[: -len("/api/v3")]
    return api


class GitHubPromotionProvider:
    name = "github"

    def __init__(
        self,
        settings: SocietySettings,
        *,
        transport: Optional[Any] = None,
        credentials: Optional[GitHubCredentialProvider] = None,
        git_runner: Optional[GitRunner] = None,
        push_timeout: float = 180.0,
    ):
        if not settings.github_repository or "/" not in settings.github_repository:
            raise ProviderUnavailable("SOCIETY_GITHUB_REPOSITORY (owner/repo) is not configured")
        self.settings = settings
        self.repo = settings.github_repository.strip()
        self.owner = self.repo.split("/")[0]
        self.api = settings.github_api_url.rstrip("/")
        self.base_branch = settings.github_base_branch.strip() or "main"
        self._transport = transport  # test hook: (method, url, json) -> (status, body)
        self._credentials = get_credential_provider(settings, override=credentials)
        self._git = git_runner or _default_git_runner
        self._push_timeout = push_timeout
        self.last_push_argv: List[str] = []  # inspectable; never contains a secret

    def __repr__(self) -> str:
        return f"GitHubPromotionProvider(repo={self.repo!r}, base={self.base_branch!r}, credentials={self._credentials.name!r})"

    # ── credential ──
    def _credential(self):
        try:
            return self._credentials.get()
        except CredentialUnavailable as exc:
            raise ProviderUnavailable(str(exc)) from None
        except CredentialRefused as exc:
            raise ProviderRefused(f"credential refused: {exc}") from None
        except CredentialTransient as exc:
            raise ProviderTransient(f"credential unavailable right now: {exc}") from None

    # ── HTTP ──
    def _send(self, method: str, url: str, token: str, json: Optional[Dict[str, Any]], params: Optional[Dict[str, Any]]):
        if self._transport is not None:
            return self._transport(method, url, json or params)
        import httpx

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            with httpx.Client(timeout=self.settings.model_timeout_seconds) as client:
                resp = client.request(method, url, json=json, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001 — never include the request (Authorization header)
            raise ProviderTransient(f"GitHub transport error {type(exc).__name__}") from None
        finally:
            del headers
        return resp.status_code, (resp.json() if resp.content else {})

    def _request(self, method: str, path: str, json: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.api}{path}"
        status, body = self._send(method, url, self._credential().token, json, params)
        if status == 401:
            # expired/revoked installation token: invalidate, refresh ONCE, retry ONCE
            self._credentials.invalidate()
            status, body = self._send(method, url, self._credential().token, json, params)
        if status in _RETRYABLE:
            raise ProviderTransient(f"GitHub returned {status} for {method} {path}")
        if status in (401, 403):
            raise ProviderRefused(f"GitHub refused {method} {path} ({status})")
        if status == 404:
            return None
        if status >= 400:
            raise ProviderRefused(f"GitHub error {status} for {method} {path}")
        return body

    # ── git push through GIT_ASKPASS (no credential in URL/argv/config) ──
    def _push(self, ws_path: str, branch: str) -> None:
        cred = self._credential()
        url = f"{push_base_url(self.api)}/{self.repo}.git"
        argv = ["git", "-c", "credential.helper=", "push", "--no-verify", url, f"HEAD:refs/heads/{branch}"]
        assert not any(flag in argv for flag in _FORBIDDEN_PUSH_FLAGS) and not any(a.startswith("+") for a in argv)
        self.last_push_argv = list(argv)
        helper_dir = tempfile.mkdtemp(prefix="agentnet-askpass-")
        try:
            os.chmod(helper_dir, 0o700)
            helper = pathlib.Path(helper_dir) / "askpass.sh"
            helper.write_text(ASKPASS_SCRIPT, encoding="utf-8")
            helper.chmod(stat.S_IRWXU)
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", "/tmp"),
                "LC_ALL": "C",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": str(helper),
                "GIT_CONFIG_NOSYSTEM": "1",
                ASKPASS_USER_ENV: cred.username,
                ASKPASS_TOKEN_ENV: cred.token,
            }
            # keep GIT_CONFIG_COUNT/KEY_n/VALUE_n (e.g. safe.directory from compose)
            for k, v in os.environ.items():
                if k.startswith("GIT_CONFIG_") and k != "GIT_CONFIG_GLOBAL":
                    env.setdefault(k, v)
            try:
                proc = self._git(argv, ws_path, env, self._push_timeout)
            except subprocess.TimeoutExpired:
                raise ProviderTransient("git push timed out") from None
            except OSError as exc:
                raise ProviderTransient(f"git push failed to start ({type(exc).__name__})") from None
            finally:
                env.pop(ASKPASS_TOKEN_ENV, None)
        finally:
            shutil.rmtree(helper_dir, ignore_errors=True)
        if proc.returncode != 0:
            err = redact((proc.stderr or "")[:300], cred.token)
            if "rejected" in err or "non-fast-forward" in err or "fetch first" in err:
                raise ProviderConflict(f"push rejected (non-fast-forward): {err}")
            if "Authentication failed" in err or "401" in err or "403" in err:
                self._credentials.invalidate()
                raise ProviderRefused(f"push refused by the remote: {err}")
            raise ProviderTransient(f"git push failed ({proc.returncode}): {err}")

    # ── contract ──
    def publish_branch(self, promotion, candidate, ws: ws_mod.Workspace) -> BranchRef:
        branch = candidate.branch_name or ""
        if not branch or branch in (self.base_branch, "main", "master") or branch.split("/")[-1] in ("main", "master"):
            raise ProviderRefused("refusing to publish to the protected base branch")
        if not branch.startswith(self.settings.branch_prefix.rstrip("/") + "/"):
            raise ProviderRefused(f"branch {branch!r} is outside the autonomous prefix")
        remote = self._request("GET", f"/repos/{self.repo}/branches/{branch}")
        if remote is not None:
            sha = ((remote or {}).get("commit") or {}).get("sha", "")
            if sha == candidate.head_sha:
                return BranchRef(name=branch, sha=sha, created=False)
            raise ProviderConflict(f"remote branch {branch} exists with different history; no force push")
        self._push(str(ws.path), branch)
        return BranchRef(name=branch, sha=candidate.head_sha or "", created=True)

    def open_or_update_pr(self, promotion, candidate, title: str, body: str) -> PRRef:
        branch = promotion.external_branch or candidate.branch_name
        existing = self._request("GET", f"/repos/{self.repo}/pulls", params={"head": f"{self.owner}:{branch}", "state": "all", "per_page": 5}) or []
        for pr in existing:
            if (pr.get("head") or {}).get("ref") == branch:
                self._request("PATCH", f"/repos/{self.repo}/pulls/{pr['number']}", json={"title": title, "body": body})
                return PRRef(number=int(pr["number"]), url=pr.get("html_url", ""), created=False)
        created = self._request("POST", f"/repos/{self.repo}/pulls", json={"title": title, "body": body, "head": branch, "base": self.base_branch, "draft": True})
        if not created:
            raise ProviderTransient("pull request creation returned no body")
        return PRRef(number=int(created["number"]), url=created.get("html_url", ""), created=True)

    def get_pr_state(self, promotion) -> PRState:
        if not promotion.external_pr_number:
            return PRState(ci="unknown", detail="no PR number")
        pr = self._request("GET", f"/repos/{self.repo}/pulls/{promotion.external_pr_number}")
        if pr is None:
            return PRState(ci="unknown", detail="PR not found", closed=True)
        head_sha = ((pr.get("head") or {}).get("sha")) or ""
        base_ref = self._request("GET", f"/repos/{self.repo}/branches/{self.base_branch}") or {}
        base_head = ((base_ref.get("commit") or {}).get("sha")) or ""
        pr_base = ((pr.get("base") or {}).get("sha")) or ""
        checks = self._request("GET", f"/repos/{self.repo}/commits/{head_sha}/check-runs", params={"per_page": 100}) or {}
        runs: List[Dict[str, Any]] = checks.get("check_runs") or []
        ci = "unknown"
        if runs:
            concl = [r.get("conclusion") for r in runs if r.get("status") == "completed"]
            if any(r.get("status") != "completed" for r in runs):
                ci = "pending"
            elif any(c in ("failure", "timed_out", "cancelled", "action_required") for c in concl):
                ci = "failed"
            elif concl and all(c in ("success", "neutral", "skipped") for c in concl):
                ci = "passed"
        reviews = self._request("GET", f"/repos/{self.repo}/pulls/{promotion.external_pr_number}/reviews", params={"per_page": 100}) or []
        approvals = sorted({(r.get("user") or {}).get("login", "?") for r in reviews if r.get("state") == "APPROVED"})
        changes_requested = sum(1 for r in reviews if r.get("state") == "CHANGES_REQUESTED")
        return PRState(
            ci=ci,
            head_sha=head_sha,
            base_moved=bool(pr_base and base_head and pr_base != base_head),
            merged=bool(pr.get("merged")),
            merged_sha=pr.get("merge_commit_sha") or "",
            mergeable=pr.get("mergeable"),
            approvals=approvals,
            unresolved_reviews=changes_requested,
            closed=pr.get("state") == "closed" and not pr.get("merged"),
            detail=str(pr.get("mergeable_state") or ""),
        )

    def merge(self, promotion, expected_head_sha: str) -> str:
        if not self.settings.auto_merge_enabled:
            raise ProviderRefused("auto-merge is disabled; a human merges through GitHub")
        body = self._request("PUT", f"/repos/{self.repo}/pulls/{promotion.external_pr_number}/merge", json={"sha": expected_head_sha, "merge_method": "squash"})
        if not body or not body.get("merged"):
            raise ProviderConflict("merge was not performed (head moved or checks pending)")
        return body.get("sha", "")


__all__ = ["ASKPASS_SCRIPT", "ASKPASS_TOKEN_ENV", "ASKPASS_USER_ENV", "GitHubPromotionProvider", "push_base_url"]
