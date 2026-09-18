"""GitHub implementation of the PromotionProvider — INERT without configuration.

Secret boundary: the credential (a short-lived GitHub App installation token
or, until an App exists, a token supplied by the operator) is read from
``SOCIETY_GITHUB_TOKEN`` INSIDE this module at call time and never stored on
the provider object, the settings object, a run/context/event row or a log
line. Cognition, context assembly and the controller import nothing from
here; ``tests/society/test_secret_boundary.py`` proves the token name is
referenced nowhere else in the society package.

What it does when configured (not exercised in this phase — no App, no token):

* ``publish_branch`` — refuses the base branch; checks the remote branch via
  the REST API; pushes the candidate worktree's HEAD to
  ``refs/heads/<branch>`` with ``git push`` (argv, no shell, NO force);
  a remote branch with different history is a conflict, never overwritten;
* ``open_or_update_pr`` — finds an existing PR by head branch and updates
  it, otherwise creates a DRAFT PR whose body is generated from facts;
* ``get_pr_state`` — merges PR metadata, check-run conclusions and reviews
  into ``PRState``; ``base_moved`` compares the PR's recorded base sha with
  the base branch head;
* ``merge`` — refused unless ``SOCIETY_AUTO_MERGE_ENABLED`` (which config
  validation rejects for this provider in this phase) and even then only
  with the expected head sha (``sha`` parameter of the merge endpoint).

Required future GitHub App permissions (docs/GITHUB_PROMOTION.md):
Contents: read & write (branch push), Pull requests: read & write, Metadata:
read (implicit), Checks: read (CI state). NOT Administration, NOT Issues, and
Workflows only if workflow-file changes are ever intentionally promoted.
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
from typing import Any, Dict, List, Optional

from .config import SocietySettings
from .engineering import workspace as ws_mod
from .promotion import BranchRef, PRRef, PRState, ProviderConflict, ProviderRefused, ProviderTransient, ProviderUnavailable

logger = logging.getLogger(__name__)

TOKEN_ENV = "SOCIETY_GITHUB_TOKEN"
_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def _token() -> str:
    """Read the credential at call time; never cached on an object."""
    tok = os.getenv(TOKEN_ENV, "").strip()
    if not tok:
        raise ProviderUnavailable(f"{TOKEN_ENV} is not set; the GitHub promotion provider is inert")
    return tok


class GitHubPromotionProvider:
    name = "github"

    def __init__(self, settings: SocietySettings, *, transport: Optional[Any] = None):
        if not settings.github_repository or "/" not in settings.github_repository:
            raise ProviderUnavailable("SOCIETY_GITHUB_REPOSITORY (owner/repo) is not configured")
        self.settings = settings
        self.repo = settings.github_repository.strip()
        self.owner = self.repo.split("/")[0]
        self.api = settings.github_api_url.rstrip("/")
        self.base_branch = settings.github_base_branch.strip() or "main"
        self._transport = transport  # test hook: (method, url, json) -> (status, body)

    # ── HTTP ──
    def _request(self, method: str, path: str, json: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Any:
        token = _token()
        url = f"{self.api}{path}"
        if self._transport is not None:
            status, body = self._transport(method, url, json or params)
        else:
            import httpx

            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            try:
                with httpx.Client(timeout=self.settings.model_timeout_seconds) as client:
                    resp = client.request(method, url, json=json, params=params, headers=headers)
            except Exception as exc:  # noqa: BLE001 — never include the request (Authorization header)
                raise ProviderTransient(f"GitHub transport error {type(exc).__name__}") from None
            status, body = resp.status_code, (resp.json() if resp.content else {})
        if status in _RETRYABLE:
            raise ProviderTransient(f"GitHub returned {status} for {method} {path}")
        if status in (401, 403):
            raise ProviderRefused(f"GitHub refused {method} {path} ({status})")
        if status == 404:
            return None
        if status >= 400:
            raise ProviderRefused(f"GitHub error {status} for {method} {path}")
        return body

    # ── contract ──
    def publish_branch(self, promotion, candidate, ws: ws_mod.Workspace) -> BranchRef:
        branch = candidate.branch_name or ""
        if not branch or branch in (self.base_branch, "main", "master"):
            raise ProviderRefused("refusing to publish to the protected base branch")
        if not branch.startswith(self.settings.branch_prefix.rstrip("/") + "/"):
            raise ProviderRefused(f"branch {branch!r} is outside the autonomous prefix")
        remote = self._request("GET", f"/repos/{self.repo}/branches/{branch}")
        if remote is not None:
            sha = ((remote or {}).get("commit") or {}).get("sha", "")
            if sha == candidate.head_sha:
                return BranchRef(name=branch, sha=sha, created=False)
            raise ProviderConflict(f"remote branch {branch} exists with different history; no force push")
        token = _token()
        push_url = f"https://x-access-token:{token}@github.com/{self.repo}.git"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"), "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        try:
            proc = subprocess.run(["git", "push", "--no-verify", push_url, f"HEAD:refs/heads/{branch}"], cwd=str(ws.path), env=env, capture_output=True, text=True, timeout=180, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderTransient(f"git push failed to start/finish: {type(exc).__name__}") from None
        if proc.returncode != 0:
            err = (proc.stderr or "")[:300].replace(token, "***")
            if "rejected" in err or "non-fast-forward" in err:
                raise ProviderConflict(f"push rejected (non-fast-forward): {err}")
            raise ProviderTransient(f"git push failed ({proc.returncode}): {err}")
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


__all__ = ["GitHubPromotionProvider", "TOKEN_ENV"]
