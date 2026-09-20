#!/usr/bin/env python3
"""AgentNet — the trusted production release gate (Phase 7 §17-21).

This is an OPERATOR tool. It is deliberately not a Society module:

  * nothing under ``services/registry/app/society`` imports it, and a test
    (``tests/test_production_release_gate.py``) asserts that;
  * no model intent reaches it -- there is no executor and no intent type;
  * it lives outside the application image's runtime path.

The Society may autonomously improve ``main``. It may never autonomously grant
itself production authority. Production release is a constitutional boundary,
and this file is where that boundary is actually checked.

Default behaviour is a READ-ONLY PREFLIGHT. It prints what it would do and
exits non-zero if any gate fails. Mutation requires ``--execute``, matching the
project's audit-first philosophy.

    python deploy/production/release.py --target <MAIN_SHA>             # preflight
    python deploy/production/release.py --target <MAIN_SHA> --execute   # release

What it proves before allowing a release
----------------------------------------
1. TARGET           the SHA exists, is reachable from ``main``, and main CI
                    succeeded on it. "Latest" is never implied.
2. STAGING EVIDENCE for every production service, the target's runtime subtree
                    is one staging has actually run. A Railway watch-path SKIP
                    is legitimate, so an unchanged subtree inherits the
                    evidence of the SHA staging did deploy -- but a subtree that
                    CHANGED and was never deployed is not releasable.
3. SENSITIVE DIFF   migrations, DB bootstrap, auth, payment/economics, Society
                    policy, the credential boundary, CI, deployment foundation
                    and this release machinery are owner-reviewed release
                    events. The gate fails closed on them (ADR-0008 D7).
4. TREE EQUALITY    the release commit's tree must equal the approved target's
                    tree. The wrapper commit SHA differs; the source must not.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

REPO_DEFAULT = "vansyson1308/agentnet"
PRODUCTION_BRANCH = "production"
MAIN_BRANCH = "main"

#: Runtime subtrees that define what a production service actually runs. A
#: change inside one of these is a change to that service, and needs staging
#: evidence; a change outside them (docs, tests) cannot alter the image.
SERVICE_PATHS: Dict[str, str] = {
    "registry": "services/registry",
    "payment": "services/payment",
    "worker": "services/worker",
    "dashboard": "services/dashboard",
}

#: Paths whose change makes a release an explicit, owner-reviewed event.
#: Deliberately broad: the cost of a false positive is one human read; the cost
#: of a false negative is an unreviewed migration or auth change in production.
SENSITIVE_PATTERNS: Dict[str, Sequence[str]] = {
    "migrations": ("services/*/migrations/*", "services/*/migrations/**"),
    "db_bootstrap": ("init-db/*", "init-db/**", "**/entrypoint.sh", "scripts/ci/*db*"),
    "auth": ("**/auth.py", "**/api/routes/auth.py", "**/security.py", "**/operator_auth.py"),
    "payment_economics": (
        "services/payment/**", "**/task_service.py", "**/wallet*.py", "**/escrow*.py",
        "**/transactions.py", "**/wallets.py",
    ),
    "society_policy": (
        "**/society/policy.py", "**/society/risk.py", "**/society/fitness.py",
        "**/society/promotion.py", "**/society/promotion_github.py", "**/society/config.py",
        "**/society/approvals.py", "**/society/intents.py",
    ),
    "credential_boundary": (
        "**/github_credentials.py", "**/email_delivery.py", "**/secrets*.py",
    ),
    "release_machinery": ("deploy/production/**", ".railway/**"),
    "ci": (".github/workflows/**",),
    "deployment_foundation": ("**/Dockerfile", "**/Dockerfile.*", "docker-compose*.yml"),
}


class ReleaseBlocked(Exception):
    """A gate refused the release. The message names which one and why."""


# ── git / CI facts (injectable so the gate is testable offline) ──────────
def _git(*args: str, cwd: Optional[str] = None) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=120
    )
    if out.returncode != 0:
        raise ReleaseBlocked(f"git {' '.join(args)} failed: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


@dataclass
class GitFacts:
    """Everything the gate needs to know about the repository."""

    cwd: Optional[str] = None

    def commit_exists(self, sha: str) -> bool:
        try:
            return _git("cat-file", "-e", f"{sha}^{{commit}}", cwd=self.cwd) == ""
        except ReleaseBlocked:
            return False

    def is_reachable_from(self, sha: str, ref: str) -> bool:
        try:
            _git("merge-base", "--is-ancestor", sha, ref, cwd=self.cwd)
            return True
        except ReleaseBlocked:
            return False

    def tree_of(self, sha: str) -> str:
        return _git("rev-parse", f"{sha}^{{tree}}", cwd=self.cwd)

    def subtree_of(self, sha: str, path: str) -> str:
        """The tree object for one directory. Equal shas mean byte-identical
        contents, which is exactly the 'did this service change?' question."""
        line = _git("rev-parse", f"{sha}:{path}", cwd=self.cwd)
        return line

    def changed_paths(self, base: str, head: str) -> List[str]:
        raw = _git("diff", "--name-only", f"{base}...{head}", cwd=self.cwd)
        return [p for p in raw.splitlines() if p.strip()]


#: (sha) -> "success" | "failure" | ... ; injected so tests need no network.
CIStatusFn = Callable[[str], str]
#: (service_name) -> the main SHA staging is actually running for it.
StagingShaFn = Callable[[str], Optional[str]]


@dataclass
class GateResult:
    ok: bool
    target: str
    checks: List[Dict[str, object]] = field(default_factory=list)
    blocking: List[str] = field(default_factory=list)
    sensitive: Dict[str, List[str]] = field(default_factory=dict)

    def record(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            self.blocking.append(name)
            self.ok = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "target": self.target,
            "checks": self.checks,
            "blocking": self.blocking,
            "sensitive": self.sensitive,
        }


def classify_sensitive(paths: Sequence[str]) -> Dict[str, List[str]]:
    """Group changed paths by the sensitive category they fall into."""
    hits: Dict[str, List[str]] = {}
    for path in paths:
        for category, patterns in SENSITIVE_PATTERNS.items():
            if any(fnmatch.fnmatch(path, pat) for pat in patterns):
                hits.setdefault(category, []).append(path)
                break
    return hits


def staging_evidence_for(
    git: GitFacts, service: str, target: str, staging_sha: Optional[str]
) -> tuple[bool, str]:
    """Has staging actually run this service's code at the target?

    A Railway deployment can legitimately be SKIPPED when the change misses the
    service's watch paths -- a docs-only merge deploys nothing. So equality of
    deployed SHAs is the wrong question. The right one is whether the service's
    runtime subtree at the target is the same tree staging last ran.
    """
    path = SERVICE_PATHS[service]
    if staging_sha is None:
        return False, f"{service}: staging has no recorded deployment"
    if staging_sha == target:
        return True, f"{service}: staging deployed the target itself"
    try:
        a = git.subtree_of(target, path)
        b = git.subtree_of(staging_sha, path)
    except ReleaseBlocked as exc:
        return False, f"{service}: cannot compare subtrees ({exc})"
    if a == b:
        return True, (
            f"{service}: {path} unchanged since staging-deployed {staging_sha[:12]} "
            f"(tree {a[:12]})"
        )
    return False, (
        f"{service}: {path} CHANGED at the target ({a[:12]}) versus staging-deployed "
        f"{staging_sha[:12]} ({b[:12]}) and was never deployed to staging"
    )


def preflight(
    *,
    target: str,
    git: GitFacts,
    main_ci: CIStatusFn,
    staging_sha: StagingShaFn,
    current_release: Optional[str] = None,
    allow_sensitive: bool = False,
    merge_freeze_reasons: Sequence[str] = (),
) -> GateResult:
    """Run every gate read-only and return the verdict. Never mutates."""
    result = GateResult(ok=True, target=target)

    # 1. target ------------------------------------------------------------
    if not target or len(target) < 7:
        result.record("target_shape", False, "a full main SHA is required; 'latest' is not a target")
        return result
    result.record("target_shape", True, target)

    exists = git.commit_exists(target)
    result.record("target_exists", exists, "commit found" if exists else "no such commit")
    if not exists:
        return result

    reachable = git.is_reachable_from(target, f"origin/{MAIN_BRANCH}")
    result.record(
        "target_on_main", reachable,
        "reachable from origin/main" if reachable else
        "NOT reachable from origin/main -- an arbitrary SHA is never releasable",
    )

    conclusion = (main_ci(target) or "").lower()
    result.record(
        "target_ci_green", conclusion == "success",
        f"main CI conclusion={conclusion or 'unknown'}",
    )

    if merge_freeze_reasons:
        result.record(
            "no_autonomous_merge_freeze", False,
            "an autonomous-merge freeze is active: " + ", ".join(merge_freeze_reasons),
        )
    else:
        result.record("no_autonomous_merge_freeze", True, "no freeze recorded")

    # 2. staging evidence, per service ------------------------------------
    for service in sorted(SERVICE_PATHS):
        ok, detail = staging_evidence_for(git, service, target, staging_sha(service))
        result.record(f"staging_evidence:{service}", ok, detail)

    # 3. sensitive diff ----------------------------------------------------
    base = current_release
    if base and git.commit_exists(base):
        changed = git.changed_paths(base, target)
        result.sensitive = classify_sensitive(changed)
        if result.sensitive and not allow_sensitive:
            categories = ", ".join(sorted(result.sensitive))
            result.record(
                "no_sensitive_changes", False,
                f"this release touches owner-reviewed areas ({categories}); "
                "re-run with --allow-sensitive after an explicit owner review",
            )
        else:
            note = "none" if not result.sensitive else f"acknowledged: {sorted(result.sensitive)}"
            result.record("no_sensitive_changes", True, note)
    else:
        # First bootstrap: there is no previous release to diff against.
        result.record(
            "no_sensitive_changes", True,
            "initial bootstrap: no current production release to diff against",
        )
    return result


def verify_tree_equality(git: GitFacts, target: str, release_commit: str) -> None:
    """The wrapper commit may differ; the source tree must not."""
    want, got = git.tree_of(target), git.tree_of(release_commit)
    if want != got:
        raise ReleaseBlocked(
            f"release tree {got[:12]} != approved target tree {want[:12]}: "
            "the production branch would not contain the approved source"
        )


# ── live fact providers (used by the CLI, injected in tests) ────────────
def github_main_ci(repo: str = REPO_DEFAULT) -> CIStatusFn:
    """Conclusion of the CI workflow run for a SHA on main, via the REST API.

    Uses GITHUB_TOKEN if present. Read-only.
    """

    def fetch(sha: str) -> str:
        import urllib.request

        url = f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&per_page=20"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        token = os.getenv("GITHUB_TOKEN", "")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - never echo the request
            raise ReleaseBlocked(f"cannot read main CI status ({type(exc).__name__})") from None
        runs = [r for r in body.get("workflow_runs", []) if r.get("head_branch") == MAIN_BRANCH]
        if not runs:
            return "unknown"
        runs.sort(key=lambda r: r.get("run_number", 0), reverse=True)
        return str(runs[0].get("conclusion") or "pending")

    return fetch


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AgentNet trusted production release gate")
    parser.add_argument("--target", required=True, help="the approved main SHA to release")
    parser.add_argument("--repo", default=REPO_DEFAULT)
    parser.add_argument(
        "--staging-sha", action="append", default=[], metavar="SERVICE=SHA",
        help="the main SHA staging is running for a service (repeatable)",
    )
    parser.add_argument("--current-release", default=None, help="the SHA production runs today")
    parser.add_argument(
        "--allow-sensitive", action="store_true",
        help="acknowledge an owner-reviewed sensitive release (never the default)",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="create the release branch. Without this the tool only reports.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable verdict")
    args = parser.parse_args(argv)

    staging: Dict[str, str] = {}
    for item in args.staging_sha:
        if "=" not in item:
            print(f"--staging-sha expects SERVICE=SHA, got {item!r}", file=sys.stderr)
            return 2
        name, _, sha = item.partition("=")
        staging[name.strip()] = sha.strip()

    git = GitFacts()
    try:
        verdict = preflight(
            target=args.target,
            git=git,
            main_ci=github_main_ci(args.repo),
            staging_sha=lambda svc: staging.get(svc),
            current_release=args.current_release,
            allow_sensitive=args.allow_sensitive,
        )
    except ReleaseBlocked as exc:
        print(f"RELEASE BLOCKED: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(verdict.as_dict(), indent=2))
    else:
        print(f"\nAgentNet production release preflight — target {args.target}\n")
        for check in verdict.checks:
            print(f"  [{'PASS' if check['ok'] else 'FAIL'}] {check['check']}: {check['detail']}")
        if verdict.sensitive:
            print("\n  sensitive areas touched:")
            for category, paths in sorted(verdict.sensitive.items()):
                print(f"    - {category}: {len(paths)} file(s)")
        print()

    if not verdict.ok:
        print("RELEASE BLOCKED: " + ", ".join(verdict.blocking), file=sys.stderr)
        return 1

    if not args.execute:
        print("PREFLIGHT OK — read-only. Re-run with --execute to create the release branch.")
        return 0

    short = args.target[:12]
    branch = f"release/prod-{short}"
    print(f"Creating {branch} at {short} …")
    _git("branch", "-f", branch, args.target)
    verify_tree_equality(git, args.target, branch)
    print(f"OK — {branch} tree equals the approved target tree.")
    print(f"Next (trusted operator): push {branch}, open a PR into '{PRODUCTION_BRANCH}', "
          "let required CI run, and merge it yourself. The Society has no part in this.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
