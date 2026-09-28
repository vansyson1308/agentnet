"""Provider boundary of the Release Controller (ADR-0010 D16).

The ONLY module that reads release credentials, and only inside the release
controller process, at call time:

* ``MAINTENANCE_RELEASE_GITHUB_APP_ID`` / ``..._INSTALLATION_ID`` /
  ``..._PRIVATE_KEY_PEM`` -- a dedicated Maintenance Release GitHub App
  (contents write on release/* branches, pull requests, checks read; no admin,
  no ruleset bypass, no secrets, no workflows);
* ``MAINTENANCE_RAILWAY_TOKEN`` -- a Railway token scoped to the production
  environment (``MAINTENANCE_RAILWAY_AUTH_MODE=project`` sends it as
  ``Project-Access-Token``; ``account`` as a Bearer token).

Nothing here is imported by the kernel, the activities, context building or
any model-facing code (tests/society/maintenance/test_secret_boundary.py). No
credential is ever logged, returned, stored in a row or put in an error.

Railway: the mutations used are discovered from the live schema by
introspection before first use (``serviceInstanceDeployV2`` with
``commitSha``; ``deploymentRollback``); if the schema does not offer them the
provider refuses (fail closed) instead of guessing a stale name.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence

import httpx


class ProviderTransient(Exception):
    """Retry later with backoff (timeouts, 5xx, rate limits)."""


class ProviderRefused(Exception):
    """A definitive answer that makes the release unsafe: fail closed."""


# ── GitHub ─────────────────────────────────────────────────────────────────


@dataclass
class Compare:
    ahead_by: int
    behind_by: int
    files: List[str]
    patch: str  # concatenated per-file patches (may be partial for huge files)
    truncated: bool = False


@dataclass
class PullState:
    number: int
    url: str
    head_sha: str
    merged: bool
    merge_sha: Optional[str]
    mergeable: Optional[bool]
    checks: str  # passed | pending | failed
    merged_by: Optional[str] = None


class ReleaseGitHub(Protocol):
    def compare(self, base: str, head: str) -> Compare: ...  # pragma: no cover
    def tree_sha(self, sha: str) -> str: ...  # pragma: no cover
    def branch_head(self, branch: str) -> str: ...  # pragma: no cover
    def checks(self, sha: str) -> str: ...  # pragma: no cover
    def ensure_branch(self, name: str, sha: str) -> None: ...  # pragma: no cover
    def open_pr(self, head: str, base: str, title: str, body: str) -> PullState: ...  # pragma: no cover
    def pr_state(self, number: int) -> PullState: ...  # pragma: no cover
    def merge_pr(self, number: int, expected_head_sha: str) -> str: ...  # pragma: no cover
    def commit_with_tree(self, branch: str, parent_sha: str, tree_sha: str, message: str) -> str: ...  # pragma: no cover
    def pr_merged_by(self, number: int) -> Optional[str]: ...  # pragma: no cover


# ── Railway ────────────────────────────────────────────────────────────────


@dataclass
class Deployment:
    id: str
    status: str  # BUILDING DEPLOYING SUCCESS FAILED CRASHED REMOVED SLEEPING SKIPPED WAITING QUEUED
    commit_sha: Optional[str]
    created_at: Optional[str] = None
    can_rollback: bool = False


class ReleaseRailway(Protocol):
    def discover(self) -> Dict[str, bool]: ...  # pragma: no cover
    def healthy(self) -> bool: ...  # pragma: no cover
    def deployments(self, service: str, limit: int = 10) -> List[Deployment]: ...  # pragma: no cover
    def current(self, service: str) -> Optional[Deployment]: ...  # pragma: no cover
    def deploy_sha(self, service: str, sha: str) -> str: ...  # pragma: no cover
    def rollback(self, deployment_id: str) -> str: ...  # pragma: no cover
    def get(self, deployment_id: str) -> Deployment: ...  # pragma: no cover


class PublicProbe(Protocol):
    def check(self) -> Dict[str, bool]: ...  # pragma: no cover -- contract item -> healthy


class Preview(Protocol):
    def validate(self, sha: str, services: Sequence[str] = (), baseline: Optional[Dict[str, bool]] = None,
                 required: Sequence[str] = ()) -> Dict[str, Any]: ...  # pragma: no cover -- {"state": passed|pending|failed, ...}


# ── fakes (tests, simulations; never used by a live release) ─────────────────


@dataclass
class FakeGitHub:
    main: List[str] = field(default_factory=lambda: ["g0", "g1"])
    production: str = "g0"
    files_between: Dict[str, List[str]] = field(default_factory=dict)
    patches: Dict[str, str] = field(default_factory=dict)
    check_state: Dict[str, str] = field(default_factory=dict)
    prod_ci: str = "passed"
    faults: List[Exception] = field(default_factory=list)
    prs: Dict[int, PullState] = field(default_factory=dict)
    branches: Dict[str, str] = field(default_factory=dict)
    merges: int = 0
    reconcile_commits: List[Dict[str, str]] = field(default_factory=list)
    conflict: bool = False
    owner_merges: Dict[int, str] = field(default_factory=dict)

    def _fault(self):
        if self.faults:
            raise self.faults.pop(0)

    def compare(self, base, head):
        self._fault()
        if head not in self.main:
            raise ProviderRefused(f"{head} is not on main")
        chain = self.main
        if base in chain:
            ahead = chain.index(head) - chain.index(base)
            behind = max(0, -ahead)
        else:
            ahead, behind = len(chain), 0
        key = f"{base}..{head}"
        files = self.files_between.get(key) or self.files_between.get(head, [])
        return Compare(max(0, ahead), behind, list(files), self.patches.get(key) or self.patches.get(head, ""))

    def tree_sha(self, sha):
        return "tree-" + sha

    def branch_head(self, branch):
        self._fault()
        return self.production if branch == "production" else self.main[-1]

    def checks(self, sha):
        self._fault()
        return self.check_state.get(sha, "passed")

    def ensure_branch(self, name, sha):
        self._fault()
        if name in self.branches and self.branches[name] != sha:
            raise ProviderRefused(f"branch {name} exists at another sha")
        self.branches[name] = sha

    def open_pr(self, head, base, title, body):
        self._fault()
        for pr in self.prs.values():
            if pr.url.endswith(f"/{head}") and not pr.merged:
                return pr
        n = 900 + len(self.prs) + 1
        pr = PullState(n, f"fake://pr/{n}/{head}", self.branches.get(head, head), False, None, not self.conflict, "pending")
        self.prs[n] = pr
        return pr

    def pr_state(self, number):
        self._fault()
        pr = self.prs[number]
        if not pr.merged:
            pr.checks = self.prod_ci
            pr.mergeable = not self.conflict
        return pr

    def merge_pr(self, number, expected_head_sha):
        self._fault()
        pr = self.prs[number]
        if pr.merged:
            return pr.merge_sha
        if pr.head_sha != expected_head_sha:
            raise ProviderRefused("head moved")
        self.merges += 1
        pr.merged, pr.merge_sha = True, "pm-" + expected_head_sha
        self.production = pr.merge_sha
        return pr.merge_sha

    def commit_with_tree(self, branch, parent_sha, tree_sha, message):
        sha = f"rc-{len(self.reconcile_commits) + 1}"
        self.reconcile_commits.append({"branch": branch, "parent": parent_sha, "tree": tree_sha, "sha": sha})
        self.branches[branch] = sha
        return sha

    def pr_merged_by(self, number):
        return self.owner_merges.get(number)


@dataclass
class FakeRailway:
    services: Dict[str, List[Deployment]] = field(default_factory=dict)
    deploy_outcome: str = "SUCCESS"
    rollback_outcome: str = "SUCCESS"
    rollback_error: Optional[Exception] = None
    faults: List[Exception] = field(default_factory=list)
    schema_ok: bool = True
    provider_ok: bool = True
    deploy_calls: int = 0
    rollback_calls: int = 0

    def _fault(self):
        if self.faults:
            raise self.faults.pop(0)

    def seed(self, service, sha, dep_id=None):
        self.services.setdefault(service, []).insert(0, Deployment(dep_id or f"d-{service}-{sha}", "SUCCESS", sha, can_rollback=True))

    def discover(self):
        return {"serviceInstanceDeployV2.commitSha": self.schema_ok, "deploymentRollback": self.schema_ok}

    def healthy(self):
        return self.provider_ok

    def deployments(self, service, limit=10):
        self._fault()
        return list(self.services.get(service, []))[:limit]

    def current(self, service):
        for d in self.services.get(service, []):
            if d.status == "SUCCESS":
                return d
        return None

    def deploy_sha(self, service, sha):
        self._fault()
        self.deploy_calls += 1
        dep = Deployment(f"d-{service}-{sha}-{self.deploy_calls}", self.deploy_outcome, sha, can_rollback=True)
        self.services.setdefault(service, []).insert(0, dep)
        return dep.id

    def rollback(self, deployment_id):
        self._fault()
        self.rollback_calls += 1
        if self.rollback_error:
            raise self.rollback_error
        for svc, deps in self.services.items():
            for d in deps:
                if d.id == deployment_id:
                    new = Deployment(f"rb-{deployment_id}-{self.rollback_calls}", self.rollback_outcome, d.commit_sha, can_rollback=True)
                    deps.insert(0, new)
                    return new.id
        raise ProviderRefused("unknown deployment")

    def get(self, deployment_id):
        for deps in self.services.values():
            for d in deps:
                if d.id == deployment_id:
                    return d
        raise ProviderRefused("unknown deployment")


@dataclass
class FakeProbe:
    """Scripted public observations: a list of {item: healthy} per call; the
    last entry repeats."""

    script: List[Dict[str, bool]] = field(default_factory=lambda: [{"ui_root": True, "api_health": True}])
    calls: int = 0

    def check(self):
        self.calls += 1
        return dict(self.script[min(self.calls - 1, len(self.script) - 1)])


@dataclass
class FakePreview:
    state: str = "passed"

    def validate(self, sha, services=(), baseline=None, required=()):
        return {"state": self.state, "sha": sha, "mode": "fake"}


# ── live providers ─────────────────────────────────────────────────────────


class LiveGitHub:
    """GitHub REST with an installation token of the dedicated Maintenance
    Release App, minted inside this process (JWT -> installation token)."""

    API = "https://api.github.com"

    def __init__(self, repo: str, *, transport: Optional[httpx.BaseTransport] = None, timeout: float = 20.0):
        self.repo = repo
        self._transport = transport
        self._timeout = timeout
        self._token: Optional[str] = None

    def _auth(self) -> str:
        if self._token:
            return self._token
        from types import SimpleNamespace  # noqa: PLC0415

        from ..society.github_credentials import CredentialError, GitHubAppCredentialProvider  # noqa: PLC0415

        app_id = os.getenv("MAINTENANCE_RELEASE_GITHUB_APP_ID", "")
        inst = os.getenv("MAINTENANCE_RELEASE_GITHUB_INSTALLATION_ID", "")
        key_file = os.getenv("MAINTENANCE_RELEASE_GITHUB_PRIVATE_KEY_FILE", "")
        if not (app_id and inst and key_file):
            raise ProviderRefused("the Maintenance Release GitHub App is not configured in this process")
        # The shared App-JWT exchange, fed with the RELEASE App's identity and a
        # mounted key FILE (never the Society App, never an env PEM).
        cfg = SimpleNamespace(
            github_credential_provider="app", github_app_id=app_id, github_installation_id=inst, github_app_private_key_file=key_file,
            github_api_url=self.API, github_repository=self.repo, model_timeout_seconds=int(self._timeout), github_token_refresh_margin_seconds=300,
        )
        try:
            self._token = GitHubAppCredentialProvider(cfg).get().token
        except CredentialError as exc:
            raise ProviderTransient(f"release app token: {type(exc).__name__}") from None
        return self._token

    def _req(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        headers = {"Authorization": f"Bearer {self._auth()}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as c:
                r = c.request(method, self.API + path, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise ProviderTransient(f"github {method} {path.split('?')[0]}: {type(exc).__name__}") from None
        if r.status_code in (429, 500, 502, 503, 504) or (r.status_code == 403 and "rate limit" in r.text.lower()):
            raise ProviderTransient(f"github {r.status_code}")
        if r.status_code == 404:
            raise ProviderRefused(f"github 404 {path.split('?')[0]}")
        if r.status_code >= 400:
            raise ProviderRefused(f"github {r.status_code} {path.split('?')[0]}")
        return r.json() if r.text else {}

    def compare(self, base, head):
        d = self._req("GET", f"/repos/{self.repo}/compare/{base}...{head}")
        files = d.get("files") or []
        patch = "\n".join(f"--- a/{f['filename']}\n+++ b/{f['filename']}\n{f.get('patch', '')}" for f in files)
        return Compare(int(d.get("ahead_by", 0)), int(d.get("behind_by", 0)), [f["filename"] for f in files], patch, truncated=len(files) >= 300 or any("patch" not in f for f in files))

    def tree_sha(self, sha):
        return self._req("GET", f"/repos/{self.repo}/git/commits/{sha}")["tree"]["sha"]

    def branch_head(self, branch):
        return self._req("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")["object"]["sha"]

    def checks(self, sha):
        runs = self._req("GET", f"/repos/{self.repo}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])
        if not runs:
            return "pending"
        if any(r.get("status") != "completed" for r in runs):
            return "pending"
        bad = [r for r in runs if r.get("conclusion") not in ("success", "neutral", "skipped")]
        return "failed" if bad else "passed"

    def ensure_branch(self, name, sha):
        try:
            cur = self.branch_head(name)
        except ProviderRefused:
            self._req("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{name}", "sha": sha})
            return
        if cur != sha:
            raise ProviderRefused(f"branch {name} exists at another sha (no force push)")

    def _pull(self, d) -> PullState:
        return PullState(int(d["number"]), d["html_url"], d["head"]["sha"], bool(d.get("merged")), d.get("merge_commit_sha") if d.get("merged") else None, d.get("mergeable"),
                         self.checks(d["head"]["sha"]), (d.get("merged_by") or {}).get("login"))

    def open_pr(self, head, base, title, body):
        owner = self.repo.split("/")[0]
        existing = self._req("GET", f"/repos/{self.repo}/pulls?state=open&head={owner}:{head}&base={base}")
        if existing:
            return self._pull(existing[0])
        return self._pull(self._req("POST", f"/repos/{self.repo}/pulls", {"head": head, "base": base, "title": title[:250], "body": body[:60000], "draft": False}))

    def pr_state(self, number):
        return self._pull(self._req("GET", f"/repos/{self.repo}/pulls/{number}"))

    def merge_pr(self, number, expected_head_sha):
        d = self._req("PUT", f"/repos/{self.repo}/pulls/{number}/merge", {"sha": expected_head_sha, "merge_method": "merge"})
        return d.get("sha")

    def commit_with_tree(self, branch, parent_sha, tree_sha, message):
        c = self._req("POST", f"/repos/{self.repo}/git/commits", {"message": message, "tree": tree_sha, "parents": [parent_sha]})
        self.ensure_branch(branch, c["sha"])
        return c["sha"]

    def pr_merged_by(self, number):
        return self.pr_state(number).merged_by


class LiveRailway:
    ENDPOINT = "https://backboard.railway.com/graphql/v2"

    def __init__(self, *, project_id: str, environment_id: str, service_ids: Dict[str, str], transport: Optional[httpx.BaseTransport] = None, timeout: float = 20.0,
                 token_env: str = "MAINTENANCE_RAILWAY_TOKEN", auth_mode_env: str = "MAINTENANCE_RAILWAY_AUTH_MODE"):
        self.project_id, self.environment_id, self.service_ids = project_id, environment_id, dict(service_ids)
        self._transport, self._timeout = transport, timeout
        self._token_env, self._auth_mode_env = token_env, auth_mode_env
        self._schema: Optional[Dict[str, bool]] = None

    def _headers(self) -> Dict[str, str]:
        tok = os.getenv(self._token_env, "")
        if not tok:
            raise ProviderRefused("no Railway token for this environment in this process")
        if os.getenv(self._auth_mode_env, "project").strip().lower() == "project":
            return {"Project-Access-Token": tok, "Content-Type": "application/json"}
        return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def _gql(self, query: str, variables: Optional[dict] = None) -> dict:
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as c:
                r = c.post(self.ENDPOINT, headers=self._headers(), json={"query": query, "variables": variables or {}})
        except httpx.HTTPError as exc:
            raise ProviderTransient(f"railway {type(exc).__name__}") from None
        if r.status_code in (429, 500, 502, 503, 504):
            raise ProviderTransient(f"railway {r.status_code}")
        if r.status_code >= 400:
            raise ProviderRefused(f"railway {r.status_code}")
        body = r.json()
        if body.get("errors"):
            msg = "; ".join(str(e.get("message", ""))[:120] for e in body["errors"][:3])
            if "rate" in msg.lower() or "timeout" in msg.lower():
                raise ProviderTransient(f"railway: {msg}")
            raise ProviderRefused(f"railway: {msg}")
        return body.get("data") or {}

    def discover(self) -> Dict[str, bool]:
        """Checked against the live schema, including return shapes: the adapter
        only calls a mutation whose arguments AND result it knows how to read.
        (Live schema 2026-09-28: serviceInstanceDeployV2 -> String! (the new
        deployment id); deploymentRollback -> Boolean!.)"""
        if self._schema is None:
            d = self._gql("query { __schema { mutationType { fields { name args { name } type { name kind ofType { name kind } } } } } }")
            fields = {f["name"]: f for f in d["__schema"]["mutationType"]["fields"]}

            def args(name: str) -> set:
                return {a["name"] for a in (fields.get(name) or {}).get("args") or []}

            def returns(name: str) -> Optional[str]:
                t = (fields.get(name) or {}).get("type") or {}
                return t.get("name") or (t.get("ofType") or {}).get("name")

            self._schema = {
                "serviceInstanceDeployV2.commitSha": {"commitSha", "serviceId", "environmentId"} <= args("serviceInstanceDeployV2")
                and returns("serviceInstanceDeployV2") == "String",
                "deploymentRollback": "id" in args("deploymentRollback") and returns("deploymentRollback") == "Boolean",
            }
        return dict(self._schema)

    def _require(self, key: str) -> None:
        if not self.discover().get(key):
            raise ProviderRefused(f"Railway schema does not offer {key}; refusing to guess an API name")

    def healthy(self) -> bool:
        try:
            self._gql("query { __typename }")
            return True
        except (ProviderTransient, ProviderRefused):
            return False

    def _node(self, n: dict) -> Deployment:
        meta = n.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        return Deployment(n["id"], n.get("status", ""), meta.get("commitHash"), n.get("createdAt"), bool(n.get("canRollback")))

    def _list(self, service_id: str, limit: int) -> List[Deployment]:
        q = "query($input: DeploymentListInput!, $first: Int) { deployments(input: $input, first: $first) { edges { node { id status createdAt meta canRollback } } } }"
        d = self._gql(q, {"input": {"projectId": self.project_id, "serviceId": service_id, "environmentId": self.environment_id}, "first": limit})
        return [self._node(e["node"]) for e in d["deployments"]["edges"]]

    def deployments(self, service, limit=10):
        return self._list(self.service_ids[service], limit)

    def current(self, service):
        for dep in self.deployments(service, 20):
            if dep.status == "SUCCESS":
                return dep
        return None

    def deploy_sha(self, service, sha):
        self._require("serviceInstanceDeployV2.commitSha")
        q = "mutation($serviceId: String!, $environmentId: String!, $commitSha: String!) { serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId, commitSha: $commitSha) }"
        return str(self._gql(q, {"serviceId": self.service_ids[service], "environmentId": self.environment_id, "commitSha": sha})["serviceInstanceDeployV2"])

    #: A rollback whose new deployment was not yet listed when the mutation
    #: returned. It is resolved on every get(); the rollback is never re-issued.
    PENDING = "rollback-pending:"

    def rollback(self, deployment_id):
        """Roll back to ``deployment_id`` and return the NEW deployment's id.

        ``deploymentRollback`` answers only ``true``; the deployment it creates
        is the first one of the same service created after the call. A target
        Railway will not roll back to (``canRollback=false``, e.g. an expired
        image) is refused so the controller redeploys the known-good SHA."""
        self._require("deploymentRollback")
        target = self._gql("query($id: String!) { deployment(id: $id) { id serviceId environmentId canRollback } }", {"id": deployment_id})["deployment"]
        if target.get("environmentId") and target["environmentId"] != self.environment_id:
            raise ProviderRefused("known-good deployment belongs to another environment")
        if not target.get("canRollback"):
            raise ProviderRefused("Railway cannot roll back to this deployment (canRollback=false)")
        started = int(time.time())
        ok = self._gql("mutation($id: String!) { deploymentRollback(id: $id) }", {"id": deployment_id}).get("deploymentRollback")
        if ok is not True:
            raise ProviderRefused("Railway refused the rollback")
        handle = f"{self.PENDING}{target['serviceId']}:{deployment_id}:{started}"
        try:
            found = self._resolve(handle, attempts=3)
        except (ProviderTransient, ProviderRefused):
            found = None  # the rollback was issued: never raise into a retry that would issue it twice
        return found.id if found else handle

    def _resolve(self, handle: str, attempts: int = 1, pause: float = 2.0) -> Optional[Deployment]:
        service_id, target, started = handle[len(self.PENDING):].rsplit(":", 2)
        since = int(started) - 120  # clock skew between this process and Railway
        for i in range(attempts):
            newer = [d for d in self._list(service_id, 10) if d.id != target and _epoch(d.created_at) >= since]
            if newer:
                return min(newer, key=lambda d: _epoch(d.created_at))  # the first deployment after the rollback call
            if i + 1 < attempts:
                time.sleep(pause)
        return None

    def get(self, deployment_id):
        if deployment_id.startswith(self.PENDING):
            return self._resolve(deployment_id) or Deployment(deployment_id, "DEPLOYING", None, None, False)
        d = self._gql("query($id: String!) { deployment(id: $id) { id status createdAt meta canRollback } }", {"id": deployment_id})
        return self._node(d["deployment"])


def _epoch(created_at: Optional[str]) -> float:
    from datetime import datetime  # noqa: PLC0415

    if not created_at:
        return 0.0
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class LivePreview:
    """Release preview by EXACT-SHA parity on an isolated, non-production
    Railway environment (ADR-0010 D12; staging by default: its own database,
    its own staging-safe credentials, it deploys ``main`` through Wait for CI).

    ``passed`` only when, for every service the release changes, the preview
    environment's ACTIVE deployment (newest SUCCESS) is the candidate SHA,
    and then the preview's public surface answers: readiness, the A2A card,
    a security smoke (no public metrics, no wildcard CORS for a foreign
    origin) and the monitored public-surface contract judged like the
    post-deploy check -- no item healthy in the production ``baseline`` may
    fail on the preview, and the repaired incident's own item(s)
    (``required``) must be healthy there. Other defects that are still open
    in production do not block a repair of one of them. ``pending`` while the
    preview is still building the SHA; anything else fails closed. It reads a
    NON-production Railway token (``MAINTENANCE_PREVIEW_RAILWAY_TOKEN``) and
    refuses to treat the production environment as a preview."""

    FOREIGN_ORIGIN = "https://preview-smoke.invalid"

    def __init__(self, railway: "LiveRailway", *, ui_origin: str, api_origin: str, production_environment_id: str, timeout: float = 10.0,
                 transport: Optional[httpx.BaseTransport] = None, contract: Optional[Any] = None):
        if not railway.environment_id or railway.environment_id == production_environment_id:
            raise ValueError("the release preview must be a non-production Railway environment")
        self.railway, self.ui_origin, self.api_origin = railway, ui_origin.rstrip("/"), api_origin.rstrip("/")
        self.timeout, self._transport = timeout, transport
        self._contract = contract or ContractProbe(self.ui_origin, self.api_origin, timeout=timeout)

    def validate(self, sha: str, services: Sequence[str] = (), baseline: Optional[Dict[str, bool]] = None,
                 required: Sequence[str] = ()) -> Dict[str, Any]:
        wanted = [s for s in services if s in self.railway.service_ids] or sorted(self.railway.service_ids)
        missing = [s for s in services if s not in self.railway.service_ids]
        if missing:
            return {"state": "failed", "sha": sha, "reason": f"no preview service mapped for {missing}"}
        active: Dict[str, Optional[str]] = {}
        building = False
        try:
            for svc in wanted:
                deps = self.railway.deployments(svc, 10)
                cur = next((d for d in deps if d.status == "SUCCESS"), None)
                active[svc] = cur.commit_sha if cur else None
                if any(d.commit_sha == sha and d.status in ("BUILDING", "DEPLOYING", "INITIALIZING", "QUEUED", "WAITING") for d in deps):
                    building = True
        except ProviderTransient as exc:
            return {"state": "pending", "sha": sha, "reason": f"preview provider transient: {exc}"[:200]}
        except ProviderRefused as exc:
            return {"state": "failed", "sha": sha, "reason": f"preview provider refused: {exc}"[:200]}
        parity = {svc: active[svc] == sha for svc in wanted}
        if not all(parity.values()):
            # not (yet) running the candidate: wait; the release deadline fails it closed
            return {"state": "pending", "sha": sha, "reason": "preview is not running the candidate SHA yet", "parity": parity, "building": building}
        checks = self._surface_checks(baseline or {}, list(required))
        ok = all(checks.values())
        return {"state": "passed" if ok else "failed", "sha": sha, "parity": parity, "checks": checks,
                "reason": "" if ok else f"preview checks failed: {sorted(k for k, v in checks.items() if not v)}"}

    def _surface_checks(self, baseline: Dict[str, bool], required: List[str]) -> Dict[str, bool]:
        out: Dict[str, bool] = {}
        try:
            with httpx.Client(timeout=self.timeout, transport=self._transport, follow_redirects=False) as c:
                out["readiness"] = c.get(f"{self.api_origin}/readyz").status_code == 200
                card = c.get(f"{self.api_origin}/.well-known/agent-card.json")
                try:
                    out["a2a_card"] = card.status_code == 200 and bool(card.json().get("name"))
                except ValueError:
                    out["a2a_card"] = False
                out["metrics_not_public"] = c.get(f"{self.api_origin}/metrics").status_code in (401, 403, 404)
                pre = c.options(f"{self.api_origin}/readyz", headers={"Origin": self.FOREIGN_ORIGIN, "Access-Control-Request-Method": "GET"})
                acao = pre.headers.get("access-control-allow-origin", "")
                out["cors_foreign_origin_refused"] = acao not in ("*", self.FOREIGN_ORIGIN)
        except httpx.HTTPError:
            out["reachable"] = False
            return out
        contract = self._contract.check()
        # judged like post-deploy verification: an item unknown to the baseline counts as healthy there
        out["public_surface_no_regression"] = bool(contract) and not [k for k, ok in contract.items() if not ok and baseline.get(k, True)]
        out["repaired_item_healthy"] = all(contract.get(r, True) for r in required)
        return out


class ContractProbe:
    """The public-surface contract, checked with deterministic HTTP."""

    def __init__(self, ui_origin: str, api_origin: str, timeout: float = 10.0):
        self.origins = {"ui": ui_origin, "api": api_origin}
        self.timeout = timeout

    def check(self) -> Dict[str, bool]:
        from ..society import surface  # noqa: PLC0415

        report = surface.run_contract(self.origins, only_monitored=True, timeout=self.timeout)
        out: Dict[str, bool] = {}
        for o in report.observations:
            if o.severity in ("major", "critical"):
                out[o.name] = out.get(o.name, True) and o.ok
        return out


#: Which Railway services a repository path deploys to (service-aware release).
SERVICE_PATHS: Sequence[tuple] = (
    ("services/dashboard/", ("dashboard",)),
    ("services/registry/", ("registry",)),
    ("services/payment/", ("payment",)),
    ("services/worker/", ("worker",)),
    ("services/simulation/", ("simulation",)),
    ("sdk/", ()),
    ("docs/", ()),
    ("tests/", ()),
    ("examples/", ()),
)


def services_for(paths: Sequence[str]) -> List[str]:
    out: List[str] = []
    for p in paths:
        hit = None
        for prefix, svcs in SERVICE_PATHS:
            if p.startswith(prefix):
                hit = svcs
                break
        if hit is None:
            if p.endswith(".md"):
                continue
            hit = ("registry", "payment", "worker", "dashboard")  # unknown shared path: every app service
        for s in hit:
            if s not in out:
                out.append(s)
    return out


__all__ = [
    "ProviderTransient",
    "ProviderRefused",
    "Compare",
    "PullState",
    "Deployment",
    "ReleaseGitHub",
    "ReleaseRailway",
    "PublicProbe",
    "Preview",
    "FakeGitHub",
    "FakeRailway",
    "FakeProbe",
    "FakePreview",
    "LiveGitHub",
    "LiveRailway",
    "ContractProbe",
    "services_for",
]
