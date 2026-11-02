"""Rework an OPEN bot PR in place (G1y items 2 and 4, 2026-11-01).

Two deterministic ways a candidate that already has a pull request gets a new head,
and ONE way that head reaches the same PR. No model decides any of it, nothing here
merges, approves or touches a wallet, and the RED law is unchanged: the new head is
classified again by the trusted ``risk.py`` of the running revision.

* ``relocate_tests`` (item 2) -- a READY candidate with an open PR whose only RED path
  is its own NEW test in ``tests/society/`` while its product change belongs to one
  placement area (``spec_gate.PLACEMENT``: templates/static, sdk, docs): the controller
  moves the test (``git mv``, byte-identical, proven green at the new path first),
  writes a NEW plan revision that renames the test path (``spec["revisions"]``; the
  previous paths are kept there -- files_allowed is renamed, never widened), clears the
  QA/Security verdicts and asks QA for the new head. Nothing is moved for a change to
  society code, a nested test, a non-test file or an existing test.

* ``return_for_ci`` (item 4) -- CI is red on an UP-TO-DATE head: the controller merges
  the trusted base into the candidate workspace, re-runs the tests of the change's area
  there and keeps only the failures that are NEW with the change (a test that also fails
  on the base is the environment, not the candidate). Reproduced -> a new plan revision
  adds those tests to ``acceptance_tests`` (more criteria, files_allowed unchanged), the
  candidate goes back to the Builder as ``qa_failed`` with the failing tests, at most
  ``MAX_CI_REWORKS`` times; not reproduced -> nothing changes (the PR stays red for the
  owner). The Builder rebuilds, or declines with a reason (executor; the controller then
  closes its own PR).

* ``step`` (both) -- once the candidate is READY again (QA and Security PASS for the new
  head, ``promotion.validate`` re-run from the trusted base), the change between the old
  and the new head is replayed onto the PR branch as ONE fast-forward commit through the
  provider (``replay_files``): never a force push, and only over files whose version on
  the PR branch is one the rework knows (the PR's previous version, or the base version
  the new head was built on). Anything else -- a human edit, a newer main -- is a
  conflict: the PR is held for the owner, never overwritten. CI then runs again and a new
  fitness experiment judges the new head.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import pathlib
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from ..models import CodeCandidate, CodeCandidateStatus, CodePromotion, PromotionStatus
from . import candidate_health
from .config import SocietySettings
from .engineering import workspace as ws_mod
from .events import EventType, emit_event, utcnow
from .risk import RiskTier
from .risk import assess as assess_risk

logger = logging.getLogger("agentnet.society.rework")

PENDING = "rework_pending"
DONE = "reworks"
SKIPPED = "rework_skipped"
MAX_CI_REWORKS = 1
MAX_REPLAY_ERRORS = 3
TERMINAL = ("rejected", "failed", "abandoned")
OPEN_PR_STATES = (PromotionStatus.PR_OPEN, PromotionStatus.CI_PENDING, PromotionStatus.CI_PASSED, PromotionStatus.CI_FAILED,
                  PromotionStatus.AWAITING_APPROVAL, PromotionStatus.MERGE_ELIGIBLE)
_TEST_FILE = re.compile(r"(?:^|/)test_[^/]*\.py$")
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+)(?: - (.*))?$")


def _ev(v: Any) -> str:
    return str(getattr(v, "value", v))


# ── git facts (content-addressed, so a local blob sha IS the GitHub blob sha) ──


def git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()  # noqa: S324 -- git's object id, not security


def _blob(ws: ws_mod.Workspace, commit: str, path: str) -> Optional[str]:
    if not commit:
        return None
    try:
        return ws_mod._git(["rev-parse", "--verify", "-q", f"{commit}:{path}"], cwd=ws.path).strip() or None
    except ws_mod.WorkspaceError:
        return None


def _content(ws: ws_mod.Workspace, commit: str, path: str) -> Optional[bytes]:
    proc = subprocess.run(["git", "cat-file", "blob", f"{commit}:{path}"], cwd=str(ws.path), capture_output=True, timeout=60, check=False)
    return proc.stdout if proc.returncode == 0 else None


def _keep(ws: ws_mod.Workspace, cand_id: Any, sha: str) -> None:
    """Keep the PR's previous head reachable (the replay reads it after a reset/merge)."""
    ws_mod._git(["update-ref", f"refs/agentnet/rework/{cand_id}/{sha[:12]}", sha], cwd=ws.path)


def replay_set(ws: ws_mod.Workspace, *, from_local: str, to_local: str, base: str, paths: List[str]) -> Tuple[Dict[str, Optional[bytes]], Dict[str, List[Optional[str]]]]:
    """(changes, expected): every path of the old or new change gets its content at the new head
    (None = deleted); the PR branch may hold only the old head's or the new base's version of it."""
    changes: Dict[str, Optional[bytes]] = {}
    expected: Dict[str, List[Optional[str]]] = {}
    for path in sorted(set(paths)):
        new_blob = _blob(ws, to_local, path)
        old_blob = _blob(ws, from_local, path)
        if new_blob == old_blob:
            continue  # unchanged by the rework
        changes[path] = _content(ws, to_local, path) if new_blob else None
        expected[path] = sorted({old_blob, _blob(ws, base, path)}, key=lambda x: x or "")
    return changes, expected


# ── plan revisions (immutable history: the previous paths stay readable) ──


def revise_spec(spec: Dict[str, Any], *, kind: str, moves: Optional[Dict[str, str]] = None, add_tests: Optional[List[str]] = None, **detail: Any) -> Dict[str, Any]:
    moves = dict(moves or {})

    def mv(p: str) -> str:
        head, sep, rest = str(p).partition("::")
        return moves.get(head, head) + sep + rest

    old = dict(spec or {})
    new = copy.deepcopy(old)
    rev = int(old.get("plan_revision") or 1) + 1
    new["files_allowed"] = [mv(p) for p in old.get("files_allowed") or []]
    tests = [mv(t) if isinstance(t, str) else t for t in old.get("acceptance_tests") or []]
    new["acceptance_tests"] = tests + [t for t in (add_tests or []) if t not in tests]
    if old.get("test_first"):
        new["test_first"] = [{**f, "path": mv(f["path"])} for f in old["test_first"]]
    new["plan_revision"] = rev
    record = {"revision": rev, "kind": kind, "at": utcnow().isoformat(), "by": "promotion-controller",
              "previous": {"files_allowed": list(old.get("files_allowed") or []), "acceptance_tests": list(old.get("acceptance_tests") or []),
                           "test_first": [f.get("path") for f in old.get("test_first") or []]}}
    if moves:
        record["moves"] = moves
    if add_tests:
        record["added_acceptance_tests"] = list(add_tests)
    new["revisions"] = list(old.get("revisions") or []) + [{**record, **detail}]
    return new


# ── item 2: a test next to what it judges ──


def relocation_plan(settings: SocietySettings, cand: CodeCandidate, ws: ws_mod.Workspace) -> Tuple[Optional[Dict[str, Any]], str]:
    from . import spec_gate  # noqa: PLC0415

    changed = [str(p) for p in cand.changed_files or []]
    society = [p for p in changed if p.startswith("tests/society/")]
    if not society:
        return None, ""
    tests = [p for p in society if _TEST_FILE.search(p)]
    if len(tests) != len(society):
        return None, "a non-test file under tests/society/ changes"
    product = [p for p in changed if not p.startswith("tests/")]
    if any(p.startswith(spec_gate.SOCIETY_CODE) for p in product):
        return None, "the change touches society code: its test stays in tests/society/"
    root = spec_gate.test_root_for(changed)
    if not product or not root:
        return None, "the product files have no single placement area"
    moves: Dict[str, str] = {}
    repo = pathlib.Path(settings.repo_root)
    for path in tests:
        rest = path[len("tests/society/"):]
        if "/" in rest:
            return None, f"{path} is nested; only a top-level test is moved"
        if _blob(ws, cand.base_sha or "", path):
            return None, f"{path} exists on the base: an existing test is never moved"
        target = root + rest
        if _blob(ws, cand.head_sha or "", target) or (repo / target).exists():
            return None, f"{target} already exists"
        moves[path] = target
    after = [moves.get(p, p) for p in changed]
    risk = assess_risk(after, ws_mod.diff_text(ws, max_chars=400_000), spec_kind=str((cand.spec or {}).get("kind") or ""))
    if risk.tier in (RiskTier.RED, RiskTier.NEVER):
        return None, f"still {risk.tier.value} after the move: " + "; ".join(risk.reasons[:3])
    return {"moves": moves, "root": root, "tier_after_predicted": risk.tier.value}, ""


def _pytest(ws: ws_mod.Workspace, path: pathlib.Path, targets: List[str], timeout: int) -> Tuple[int, str]:
    from .engineering.qa import scrubbed_env  # noqa: PLC0415

    argv = [sys.executable, "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider", *targets]
    env = {**scrubbed_env(ws), "PYTHONPATH": str(path)}
    try:
        proc = subprocess.run(argv, cwd=str(path), env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except OSError as exc:
        return -1, f"could not start pytest: {exc}"
    return proc.returncode, proc.stdout + "\n" + proc.stderr


def _open_rows(db: Session):
    return (db.query(CodePromotion, CodeCandidate).join(CodeCandidate, CodeCandidate.id == CodePromotion.candidate_id)
            .filter(CodePromotion.status.in_(OPEN_PR_STATES), CodePromotion.external_pr_number.isnot(None))
            .order_by(CodePromotion.created_at).all())


def _skip(promo: CodePromotion, head: str, kind: str, reason: str) -> None:
    ev = dict(promo.evidence or {})
    skipped = dict(ev.get(SKIPPED) or {})
    skipped[kind] = {"head": head, "reason": reason[:300], "at": utcnow().isoformat()}
    promo.evidence = {**ev, SKIPPED: skipped}


def _skipped(promo: CodePromotion, head: str, kind: str) -> bool:
    return ((promo.evidence or {}).get(SKIPPED) or {}).get(kind, {}).get("head") == head


def _handoff(db: Session, cand: CodeCandidate, ws: ws_mod.Workspace, head: str) -> None:
    cand.head_sha = head
    cand.base_sha = ws.base_sha
    cand.changed_files = ws_mod.changed_files(ws)
    cand.diff_stat = ws_mod.diff_stat(ws)
    cand.diff_hash, cand.diff_lines = ws_mod.diff_identity(ws)


def relocate_tests(db: Session, settings: SocietySettings, *, limit: int = 5) -> int:
    """Move the tests/society test of each eligible open PR (module doc). Commits per candidate."""
    moved = 0
    for promo, cand in _open_rows(db):
        if moved >= limit:
            break
        ev = promo.evidence or {}
        if _ev(cand.status) != CodeCandidateStatus.READY.value or ev.get(PENDING) or _skipped(promo, cand.head_sha or "", "test_relocation"):
            continue
        if any(r.get("kind") == "test_relocation" for r in ev.get(DONE) or []):
            continue
        if not any(str(p).startswith("tests/society/") for p in cand.changed_files or []):
            continue  # nothing to move: no git work on every loop
        try:
            ws = ws_mod.ensure_workspace(settings, cand.id)
            if ws_mod.head_sha(ws) != cand.head_sha:
                continue  # the workspace is not the head QA judged; promotion.validate deals with it
            plan, why = relocation_plan(settings, cand, ws)
            if plan is None:
                if why:
                    _skip(promo, cand.head_sha or "", "test_relocation", why)
                    db.commit()
                continue
            for old, new in plan["moves"].items():
                ws_mod.contained_path(ws.path, new).parent.mkdir(parents=True, exist_ok=True)
                ws_mod._git(["mv", old, new], cwd=ws.path)
            rc, out = _pytest(ws, ws.path, sorted(plan["moves"].values()), settings.qa_test_timeout_seconds)
            if rc != 0:
                ws_mod._git(["reset", "-q", "--hard", cand.head_sha], cwd=ws.path)
                _skip(promo, cand.head_sha or "", "test_relocation", f"the moved test does not pass at its new path (exit {rc}): {out.strip()[-200:]}")
                db.commit()
                continue
            from_head = cand.head_sha
            _keep(ws, cand.id, from_head)
            head = ws_mod.commit_all(ws, "relocate: move the candidate's own test next to what it judges ("
                                     + ", ".join(f"{o} -> {n}" for o, n in plan["moves"].items()) + ")")
        except ws_mod.WorkspaceError as exc:
            logger.warning("rework: relocation of candidate %s skipped: %s", str(cand.id)[:8], exc)
            continue
        tier_before = _ev(promo.risk_tier)
        paths_before = list(cand.changed_files or [])
        cand.spec = revise_spec(cand.spec or {}, kind="test_relocation", moves=plan["moves"], from_head=from_head, tier_before=tier_before)
        _handoff(db, cand, ws, head)
        cand.requires_security_review = True  # the tier this head is judged at is AMBER at least: Security sees it again
        cand.qa_report = candidate_health.reset_qa_report(cand.qa_report, new_head=head)
        sec = dict(cand.security_report or {})
        cand.security_report = {"previous": {k: sec.get(k) for k in ("verdict", "head_sha") if sec.get(k)}}
        cand.status = CodeCandidateStatus.BUILT
        promo.evidence = {**(promo.evidence or {}), PENDING: {"kind": "test_relocation", "from_local": from_head, "to_local": head, "paths_before": paths_before,
                                                              "moves": plan["moves"], "tier_before": tier_before, "tier_after_predicted": plan["tier_after_predicted"],
                                                              "requested_at": utcnow().isoformat()}}
        db.flush()
        candidate_health.emit_qa_request(db, cand, reason="test_relocation")
        db.commit()
        moved += 1
        logger.info("rework: PR #%s candidate %s test moved %s (tier %s -> %s predicted); QA/Security asked for %s",
                    promo.external_pr_number, str(cand.id)[:8], plan["moves"], tier_before, plan["tier_after_predicted"], head[:10])
    return moved


# ── item 4: CI red on an up-to-date head goes back to the Builder ──


def _failures(out: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for line in out.splitlines():
        m = _FAILED_LINE.match(line.strip())
        if m:
            found.setdefault(m.group(1), (m.group(2) or "").strip()[:200])
    return found


def _on_base(settings: SocietySettings, ws: ws_mod.Workspace, base: str, ids: List[str]) -> Set[str]:
    """Which of these tests also fail on the trusted base alone (environment, not the change)."""
    tmp = ws.path.parent / f"{ws.path.name}.ci-base"
    try:
        ws_mod._git(["worktree", "prune"], cwd=ws.repo_root)
        ws_mod._git(["worktree", "add", "--detach", "--force", str(tmp), base], cwd=ws.repo_root, timeout=120)
        rc, out = _pytest(ws, tmp, ids, settings.qa_test_timeout_seconds)
        if rc in (0, 5):
            return set()
        fails = set(_failures(out))
        return fails or set(ids)  # could not tell which: none of them is attributed to the change
    except ws_mod.WorkspaceError:
        return set(ids)
    finally:
        try:
            ws_mod._git(["worktree", "remove", "--force", str(tmp)], cwd=ws.repo_root)
        except ws_mod.WorkspaceError:
            ws_mod._git(["worktree", "prune"], cwd=ws.repo_root)


def return_for_ci(db: Session, settings: SocietySettings, promo: CodePromotion, cand: CodeCandidate) -> bool:
    """CI failed on an up-to-date head the controller validated (the caller checks both). True when
    the candidate went back to its Builder; otherwise the reason is recorded once per PR head."""
    from . import spec_gate  # noqa: PLC0415
    from .executor import MAX_QA_ATTEMPTS  # noqa: PLC0415

    ev = promo.evidence or {}
    remote = promo.candidate_sha or ""
    if ev.get(PENDING) or _ev(cand.status) != CodeCandidateStatus.READY.value or _skipped(promo, remote, "ci_fix"):
        return False
    if sum(1 for r in ev.get(DONE) or [] if r.get("kind") == "ci_fix") >= MAX_CI_REWORKS:
        _skip(promo, remote, "ci_fix", f"CI is red again after {MAX_CI_REWORKS} rework(s): the owner decides")
        return False
    changed = [str(p) for p in cand.changed_files or []]
    product = [p for p in changed if not p.startswith("tests/")]
    roots = sorted({spec_gate._area(p) for p in product})
    if not product or "" in roots or any(p.startswith(spec_gate.SOCIETY_CODE) for p in product):
        _skip(promo, remote, "ci_fix", "the change has no placement test area to reproduce CI in")
        return False
    try:
        ws = ws_mod.ensure_workspace(settings, cand.id)
        from_head = cand.head_sha or ""
        if ws_mod.head_sha(ws) != from_head:
            return False
        main = ws_mod.main_branch_head(settings)
        _keep(ws, cand.id, from_head)
        if main and main != cand.base_sha:
            try:
                ws_mod._git(["merge", "--no-edit", "-q", main], cwd=ws.path, timeout=120)
            except ws_mod.WorkspaceError as exc:
                try:
                    ws_mod._git(["merge", "--abort"], cwd=ws.path)
                except ws_mod.WorkspaceError:
                    ws_mod._git(["reset", "-q", "--hard", from_head], cwd=ws.path)
                _skip(promo, remote, "ci_fix", f"the candidate does not merge cleanly with main: {str(exc)[:150]}")
                return False
        targets = [r for r in roots if (ws.path / r).is_dir()] + [p for p in changed if _TEST_FILE.search(p) and (ws.path / p).is_file()]
        rc, out = _pytest(ws, ws.path, list(dict.fromkeys(targets)), settings.qa_test_timeout_seconds)
        failing = _failures(out) if rc not in (0, 5) else {}
        if failing:
            env = _on_base(settings, ws, main or cand.base_sha or "", sorted(failing))
            failing = {k: v for k, v in failing.items() if k not in env}
        if not failing:
            ws_mod._git(["reset", "-q", "--hard", from_head], cwd=ws.path)
            _skip(promo, remote, "ci_fix", f"no failure of {', '.join(targets)} reproduces with the change on main (exit {rc})")
            return False
        ws = ws_mod.ensure_workspace(settings, cand.id)  # base is now main: the next build starts there
        head = ws_mod.head_sha(ws)
    except ws_mod.WorkspaceError as exc:
        logger.warning("rework: CI reproduction for candidate %s failed: %s", str(cand.id)[:8], exc)
        return False
    ids = sorted(failing)[:10]
    paths_before = list(cand.changed_files or [])
    base_before = cand.base_sha
    cand.spec = revise_spec(cand.spec or {}, kind="ci_fix", add_tests=ids, from_head=from_head, pr_head=remote, base_before=base_before, base_after=ws.base_sha)
    _handoff(db, cand, ws, head)
    qa = dict(cand.qa_report or {})
    failures = [f"ci_failed: {i}" + (f" - {failing[i]}" if failing[i] else "") for i in ids]
    cand.qa_report = {"verdict": "fail", "head_sha": head, "attempts": min(int(qa.get("attempts") or 0), MAX_QA_ATTEMPTS - 1), "ci_rework": True,
                      "summary": f"FAIL: required CI is red on PR #{promo.external_pr_number}; reproduced with the change on main: " + "; ".join(ids[:3]),
                      "failures": failures, "previous": {k: qa.get(k) for k in ("verdict", "head_sha", "attempts") if qa.get(k) is not None}}
    cand.status = CodeCandidateStatus.QA_FAILED
    cand.error = None
    promo.evidence = {**(promo.evidence or {}), PENDING: {"kind": "ci_fix", "from_local": from_head, "handoff_head": head, "paths_before": paths_before,
                                                          "failing": ids, "tier_before": _ev(promo.risk_tier), "requested_at": utcnow().isoformat()}}
    db.flush()
    # A fresh story for the Builder: the PR's own story may be at its loop-breaker budget.
    emit_event(db, event_type=EventType.CODE_CANDIDATE_QA_FAILED,
               payload={"candidate_id": str(cand.id), "title": cand.title, "attempts": cand.qa_report["attempts"], "qa_summary": cand.qa_report["summary"],
                        "failures": failures[:5], "final": False, "ci_rework": True, "pr_number": promo.external_pr_number,
                        "note": "fix the product files so these tests pass, or DECLINE_CODE_CANDIDATE with a reason"},
               actor_type="system", subject_type="code_candidate", subject_id=cand.id, idempotency_key=f"ci-rework:{cand.id}:{head}"[:160], notify=True)
    logger.info("rework: PR #%s candidate %s CI red, reproduced (%s); back to the Builder", promo.external_pr_number, str(cand.id)[:8], ", ".join(ids[:3]))
    return True


# ── both: the new head reaches the SAME PR ──


def _finish(db: Session, provider: Any, promo: CodePromotion, cand: CodeCandidate, pending: Dict[str, Any], parent: str, new_head: str) -> str:
    from . import promotion as P  # noqa: PLC0415

    ev = dict(promo.evidence or {})
    ev.pop(PENDING, None)
    record = {"kind": pending["kind"], "from_remote": parent, "to_remote": new_head, "from_local": pending.get("from_local"), "to_local": cand.head_sha,
              "tier_before": pending.get("tier_before"), "tier_after": _ev(promo.risk_tier), "at": utcnow().isoformat()}
    for k in ("moves", "failing"):
        if pending.get(k):
            record[k] = pending[k]
    promo.evidence = {**ev, DONE: list(ev.get(DONE) or []) + [record]}
    promo.candidate_sha = new_head
    promo.ci_state = "pending"
    promo.merge_state = "reworked"
    try:  # the PR body states the tier: it must not keep the old one
        provider.open_or_update_pr(promo, cand, f"[agentnet-auto] {cand.title}"[:200], P.pr_body_from_facts(db, promo, cand))
    except (P.ProviderUnavailable, P.ProviderRefused, P.ProviderConflict) as exc:
        promo.evidence = {**promo.evidence, "rework_body_error": str(exc)[:200]}
    P._transition(db, promo, cand, PromotionStatus.CI_PENDING,
                  reason=f"rework ({pending['kind']}) replayed onto the same PR: tier {record['tier_before']} -> {record['tier_after']}; required checks must re-run",
                  head_sha=new_head[:12])
    P._release(db, promo)
    logger.info("rework: PR #%s %s replayed %s -> %s, tier %s -> %s", promo.external_pr_number, pending["kind"], parent[:10], new_head[:10], record["tier_before"], record["tier_after"])
    return PromotionStatus.CI_PENDING.value


def _hold(db: Session, promo: CodePromotion, **note: Any) -> str:
    from . import promotion as P  # noqa: PLC0415

    if note:
        pending = dict((promo.evidence or {}).get(PENDING) or {})
        promo.evidence = {**(promo.evidence or {}), PENDING: {**pending, **note}}
    P._release(db, promo)
    return _ev(promo.status)


def owns_step(promo: CodePromotion, state: Any) -> bool:
    """Does a pending rework decide this poll? Not when the PR head moved for another reason
    (a reconcile landing, a human push): the controller's ordinary rules handle that first."""
    pending = (promo.evidence or {}).get(PENDING)
    if not pending:
        return False
    moved = bool(state.head_sha) and state.head_sha != promo.candidate_sha
    return not moved or bool(pending.get("replay"))


def step(db: Session, settings: SocietySettings, provider: Any, promo: CodePromotion, cand: CodeCandidate, state: Any) -> str:
    """One poll of a PR with a pending rework (merged/closed were handled by the caller)."""
    from . import promotion as P  # noqa: PLC0415

    pending = dict((promo.evidence or {}).get(PENDING) or {})
    cstatus = _ev(cand.status)
    if cstatus in TERMINAL:  # declined by its Builder (or rejected): the controller closes its OWN bot PR
        reason = f"rework_ended: candidate {cstatus} during a {pending.get('kind')} rework: {(cand.error or '')[:200]}"
        try:
            provider.close_pr(promo, f"Closed by the AgentNet promotion controller: {reason}")
        except (P.ProviderUnavailable, P.ProviderRefused, P.ProviderConflict) as exc:
            logger.warning("rework: could not close PR #%s: %s", promo.external_pr_number, type(exc).__name__)
        P._transition(db, promo, cand, PromotionStatus.REJECTED, reason=reason)
        P._release(db, promo)
        return PromotionStatus.REJECTED.value
    inflight = pending.get("replay")
    if inflight and state.head_sha and state.head_sha != promo.candidate_sha:
        # the replay call did not answer, but a head appeared: adopt it only if it is the
        # controller's own single-parent commit on the PR head it replayed onto
        try:
            parents = [str(x) for x in provider.commit_parents(state.head_sha) or []]
        except (P.ProviderUnavailable, P.ProviderRefused, P.ProviderConflict, P.ProviderTransient):
            return _hold(db, promo)
        if parents == [inflight["parent"]]:
            return _finish(db, provider, promo, cand, pending, inflight["parent"], state.head_sha)
        P._transition(db, promo, cand, PromotionStatus.SUPERSEDED, reason="PR head moved away from the rework replay")
        P._release(db, promo)
        return PromotionStatus.SUPERSEDED.value
    if pending.get("blocked") or cstatus != CodeCandidateStatus.READY.value:
        return _hold(db, promo)  # QA/Security/Builder still at work, or the owner decides
    try:
        checks = P.validate(db, settings, promo, cand)  # trusted re-validation of the NEW head (tier, QA, Security)
    except (LookupError, ValueError) as exc:
        return _hold(db, promo, blocked=f"validation of the reworked head failed: {str(exc)[:200]}")
    try:
        ws = ws_mod.ensure_workspace(settings, cand.id)
        paths = list(pending.get("paths_before") or []) + list(cand.changed_files or [])
        changes, expected = replay_set(ws, from_local=pending["from_local"], to_local=cand.head_sha or "", base=cand.base_sha or "", paths=paths)
    except (ws_mod.WorkspaceError, KeyError) as exc:
        return _hold(db, promo, blocked=f"replay set unavailable: {str(exc)[:200]}")
    if not changes:
        return _hold(db, promo, blocked="the reworked head changes nothing on the PR")
    parent = promo.candidate_sha or ""
    promo.eligibility = checks
    promo.evidence = {**(promo.evidence or {}), PENDING: {**pending, "replay": {"parent": parent, "at": utcnow().isoformat()}}}
    db.commit()  # the intent is durable before the network call (an unanswered call is adopted by its parent proof)
    message = f"agentnet rework ({pending['kind']}): candidate {cand.id} head {str(cand.head_sha)[:12]}"
    try:
        new_head = provider.replay_files(promo, parent, changes, expected, message)
    except P.ProviderConflict as exc:
        pending.pop("replay", None)
        promo.evidence = {**(promo.evidence or {}), PENDING: {**pending, "blocked": f"conflict: {str(exc)[:200]}"}}
        return _hold(db, promo)
    except P.ProviderRefused as exc:
        pending.pop("replay", None)
        errors = int(pending.get("replay_errors") or 0) + 1
        note = {"replay_errors": errors, "replay_error": str(exc)[:200]}
        if errors >= MAX_REPLAY_ERRORS:
            note["blocked"] = f"replay refused {errors} times: {str(exc)[:150]}"
        promo.evidence = {**(promo.evidence or {}), PENDING: {**pending, **note}}
        return _hold(db, promo)
    return _finish(db, provider, promo, cand, pending, parent, new_head)


def records(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """The validator's structural view: what is pending, what was replayed (tier before/after)."""
    pending = dict((evidence or {}).get(PENDING) or {})
    return {
        "pending": {k: pending.get(k) for k in ("kind", "tier_before", "tier_after_predicted", "moves", "failing", "blocked", "replay_errors") if pending.get(k) is not None} or None,
        "done": [{k: r.get(k) for k in ("kind", "tier_before", "tier_after", "moves", "failing", "to_remote")} for r in (evidence or {}).get(DONE) or []],
        "skipped": (evidence or {}).get(SKIPPED) or {},
    }


__all__ = ["DONE", "MAX_CI_REWORKS", "PENDING", "git_blob_sha", "owns_step", "records", "relocate_tests", "relocation_plan", "replay_set",
           "return_for_ci", "revise_spec", "step"]
