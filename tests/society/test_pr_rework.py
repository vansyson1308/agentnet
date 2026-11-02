"""Rework an OPEN bot PR in place (G1y items 2 and 4, 2026-11-01).

Staging 2026-11-01: 9 bot PRs were RED only because the candidate's own new test
sat in tests/society/** while the product change (a template, the SDK README,
an example README) was AMBER; #134 was red in CI because an existing test on main
forbids a phrase the candidate added. Item 2: the controller moves such a test
next to what it judges through a NEW plan revision, QA/Security judge the new head
again, and the SAME PR gets one fast-forward commit (never a force push); tier
before/after is recorded. Item 4: CI red on an up-to-date head is reproduced
against main and handed back to the Builder, who fixes it or declines (the
controller closes its own PR).
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid

from services.registry.app.models import ChangeExperiment, CodeCandidate, SocietyEvent
from services.registry.app.society import promotion as pm
from services.registry.app.society import rework
from services.registry.app.society.engineering import workspace as ws_mod
from services.registry.app.society.risk import JUDGE_PATHS, OWNER_MERGE_PATTERNS, judges_not_owner_only
from services.registry.app.society.seed import seed_society

from .conftest import _git
from .test_promotion import _request, drive

PAGE = "services/dashboard/app/templates/page.html"
SOC_TEST = "tests/society/test_page_cta.py"
DASH_TEST = "tests/dashboard/test_page_cta.py"
GUARD_TEST = "tests/dashboard/test_page_no_credits.py"
CTA_TEST = (
    "import pathlib\n\n"
    "ROOT = pathlib.Path(__file__).resolve().parents[2]\n\n\n"
    "def test_page_has_the_cta():\n"
    "    assert 'List your agent' in (ROOT / 'services/dashboard/app/templates/page.html').read_text()\n"
)
GUARD = (
    "import pathlib\n\n"
    "ROOT = pathlib.Path(__file__).resolve().parents[2]\n\n\n"
    "def test_page_never_says_credits_or_usdc():\n"
    "    assert 'credits or USDC' not in (ROOT / 'services/dashboard/app/templates/page.html').read_text()\n"
)


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _base(repo, files):
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "base"], repo)


def _blob(ws, sha, path):
    return subprocess.run(["git", "rev-parse", f"{sha}:{path}"], cwd=str(ws.path), capture_output=True, text=True, check=True).stdout.strip()


def _candidate(db, settings, report, files, *, test_first=None, kind="code"):
    cid = uuid.uuid4()
    ws = ws_mod.ensure_workspace(settings, cid)
    for rel, text in files.items():
        p = ws.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    head = ws_mod.commit_all(ws, "candidate")
    diff_hash, diff_lines = ws_mod.diff_identity(ws)
    tests = [f for f in files if f.startswith("tests/")]
    spec = {"kind": kind, "files_allowed": [f for f in files if f not in (test_first or {})], "acceptance_tests": tests, "must_compile": True}
    if test_first:
        spec["test_first"] = [{"path": p, "content": c, "sha256": hashlib.sha256(c.encode()).hexdigest()} for p, c in test_first.items()]
    cand = CodeCandidate(
        id=cid, correlation_id=uuid.uuid4(), title="page: list your agent CTA", spec=spec, status="ready", branch_name=ws.branch,
        workspace_path=str(ws.path), base_sha=ws.base_sha, head_sha=head, changed_files=ws_mod.changed_files(ws), diff_hash=diff_hash,
        diff_lines=diff_lines, requested_by_agent_id=report.agents["architect"], builder_agent_id=report.agents["builder"],
        qa_agent_id=report.agents["qa"], security_agent_id=report.agents["security"],
        qa_report={"verdict": "pass", "head_sha": head, "attempts": 2, "summary": "ok"},
        security_report={"verdict": "pass", "head_sha": head, "findings": [], "static_findings": []}, requires_security_review=True,
    )
    db.add(cand)
    db.commit()
    return cand, ws


def _open_pr(db, db_factory, settings, report, cand, ws):
    """Request + drive to an open PR; the fake PR branch holds the candidate's files."""
    promo, _ = _request(db, settings, cand, report)
    provider = pm.FakePromotionProvider()
    drive(db_factory, settings, provider)
    db.refresh(promo)
    assert promo.external_pr_number, promo.failure_reason
    provider.files[promo.candidate_sha] = {p: _blob(ws, cand.head_sha, p) for p in cand.changed_files}
    return promo, provider


def _pass_qa_and_security(db, cand):
    db.refresh(cand)
    cand.status = "ready"
    cand.qa_report = {**(cand.qa_report or {}), "verdict": "pass", "head_sha": cand.head_sha}
    cand.security_report = {"verdict": "pass", "head_sha": cand.head_sha, "findings": [], "static_findings": []}
    db.commit()


# ── item 2: the test moves, the SAME PR is updated, the tier drops ──


def test_a_red_only_by_its_test_pr_is_relocated_requalified_and_updated_in_place(db, db_factory, society_settings, temp_repo, monkeypatch):
    _base(temp_repo, {PAGE: "<h1>AgentNet</h1>\n"})
    report = seed_society(db)
    cand, ws = _candidate(db, society_settings, report, {PAGE: "<h1>AgentNet</h1>\n<a>List your agent</a>\n", SOC_TEST: CTA_TEST}, test_first={SOC_TEST: CTA_TEST})
    promo, provider = _open_pr(db, db_factory, society_settings, report, cand, ws)
    assert promo.risk_tier == "red", "before: the change is RED only because of tests/society/"
    pr, old_remote, old_head = promo.external_pr_number, promo.candidate_sha, cand.head_sha
    old_spec = dict(cand.spec)

    assert rework.relocate_tests(db, society_settings) == 1
    db.refresh(cand)
    db.refresh(promo)
    assert DASH_TEST in cand.changed_files and SOC_TEST not in cand.changed_files and cand.head_sha != old_head
    assert ws_mod.head_sha(ws) == cand.head_sha
    # a NEW plan revision: renamed, never widened; the previous paths stay readable; test_first byte-identical
    assert cand.spec["plan_revision"] == 2 and cand.spec["revisions"][-1]["moves"] == {SOC_TEST: DASH_TEST}
    assert cand.spec["revisions"][-1]["previous"]["files_allowed"] == old_spec["files_allowed"]
    assert len(cand.spec["files_allowed"]) == len(old_spec["files_allowed"])
    assert cand.spec["acceptance_tests"] == [DASH_TEST]
    assert [f["path"] for f in cand.spec["test_first"]] == [DASH_TEST] and cand.spec["test_first"][0]["sha256"] == old_spec["test_first"][0]["sha256"]
    # verdicts belong to the head they judged: QA and Security are asked again
    assert _ev(cand.status) == "built" and "verdict" not in cand.qa_report and cand.qa_report["awaiting_head"] == cand.head_sha
    assert "verdict" not in cand.security_report and cand.requires_security_review is True
    assert db.query(SocietyEvent).filter(SocietyEvent.subject_id == cand.id, SocietyEvent.payload["reason"].astext == "test_relocation").count() == 1
    assert promo.evidence[rework.PENDING]["tier_before"] == "red"
    # idempotent: a second sweep moves nothing
    assert rework.relocate_tests(db, society_settings) == 0

    drive(db_factory, society_settings, provider)  # held while QA/Security work
    assert not [c for c in provider.calls if c[0] == "replay_files"]
    db.refresh(promo)
    assert promo.candidate_sha == old_remote and _ev(promo.status) != "merged"

    _pass_qa_and_security(db, cand)
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    db.refresh(cand)
    replays = [c for c in provider.calls if c[0] == "replay_files"]
    assert len(replays) == 1 and replays[0][2] == old_remote and replays[0][3] == [DASH_TEST, SOC_TEST]
    assert promo.external_pr_number == pr and provider.pr_create_count == 1 and provider.publish_count == 1, "the SAME PR, no new branch"
    new_remote = provider.prs[cand.branch_name]["head_sha"]
    assert promo.candidate_sha == new_remote and provider.parents[new_remote] == [old_remote], "one fast-forward commit, never a force push"
    assert DASH_TEST in provider.files[new_remote] and SOC_TEST not in provider.files[new_remote]
    assert provider.files[new_remote][DASH_TEST] == provider.files[old_remote][SOC_TEST], "the test moved byte-identical"
    assert promo.risk_tier == "amber" and promo.evidence[rework.DONE][-1]["tier_before"] == "red" and promo.evidence[rework.DONE][-1]["tier_after"] == "amber"
    assert rework.PENDING not in promo.evidence
    assert SOC_TEST not in promo.eligibility["changed_files"]
    # the fitness verdict belongs to the new head
    exp = db.query(ChangeExperiment).filter(ChangeExperiment.promotion_id == promo.id).order_by(ChangeExperiment.created_at.desc()).first()
    assert exp is not None and exp.candidate_sha == cand.head_sha
    view = rework.records(promo.evidence)
    assert view["done"][0]["tier_before"] == "red" and view["done"][0]["tier_after"] == "amber"


def test_a_test_is_not_moved_for_society_code_nor_when_it_fails_at_its_new_path(db, db_factory, society_settings, temp_repo):
    _base(temp_repo, {PAGE: "<h1>AgentNet</h1>\n", "services/registry/app/society/x.py": "X = 1\n"})
    report = seed_society(db)
    soc, soc_ws = _candidate(db, society_settings, report, {"services/registry/app/society/x.py": "X = 2\n", "tests/society/test_x.py": "def test_x():\n    assert True\n"})
    p1, _ = _open_pr(db, db_factory, society_settings, report, soc, soc_ws)
    # a test that only works from tests/society/ is not moved (proven at the new path first)
    fragile = "import pathlib\n\n\ndef test_where():\n    assert pathlib.Path(__file__).parent.name == 'society'\n"
    frag, frag_ws = _candidate(db, society_settings, report, {PAGE: "<a>List your agent</a>\n", SOC_TEST: fragile})
    p2, _ = _open_pr(db, db_factory, society_settings, report, frag, frag_ws)
    heads = (soc.head_sha, frag.head_sha)

    assert rework.relocate_tests(db, society_settings) == 0
    for c, p, ws in ((soc, p1, soc_ws), (frag, p2, frag_ws)):
        db.refresh(c)
        db.refresh(p)
        assert _ev(c.status) == "ready" and ws_mod.head_sha(ws) == c.head_sha and c.head_sha in heads
        assert rework.PENDING not in (p.evidence or {})
    assert "society code" in p1.evidence[rework.SKIPPED]["test_relocation"]["reason"]
    assert "does not pass at its new path" in p2.evidence[rework.SKIPPED]["test_relocation"]["reason"]
    assert not (frag_ws.path / DASH_TEST).exists(), "the move was undone"


def test_a_file_someone_else_changed_on_the_pr_branch_is_never_overwritten(db, db_factory, society_settings, temp_repo):
    _base(temp_repo, {PAGE: "<h1>AgentNet</h1>\n"})
    report = seed_society(db)
    cand, ws = _candidate(db, society_settings, report, {PAGE: "<a>List your agent</a>\n", SOC_TEST: CTA_TEST})
    promo, provider = _open_pr(db, db_factory, society_settings, report, cand, ws)
    provider.files[promo.candidate_sha][SOC_TEST] = "f" * 40  # a human edited the test on the PR branch
    old_remote = promo.candidate_sha
    assert rework.relocate_tests(db, society_settings) == 1
    _pass_qa_and_security(db, cand)
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert provider.prs[cand.branch_name]["head_sha"] == old_remote and promo.candidate_sha == old_remote
    assert "conflict" in promo.evidence[rework.PENDING]["blocked"]
    assert _ev(promo.status) not in ("superseded", "rejected", "merged"), "held for the owner, not destroyed"
    drive(db_factory, society_settings, provider)
    assert len([c for c in provider.calls if c[0] == "replay_files"]) == 1, "a blocked rework is not retried every poll"


def test_an_unanswered_replay_is_adopted_only_by_its_parent_proof(db, db_factory, society_settings, temp_repo):
    _base(temp_repo, {PAGE: "<h1>AgentNet</h1>\n"})
    report = seed_society(db)
    cand, ws = _candidate(db, society_settings, report, {PAGE: "<a>List your agent</a>\n", SOC_TEST: CTA_TEST})
    promo, provider = _open_pr(db, db_factory, society_settings, report, cand, ws)
    old_remote = promo.candidate_sha
    rework.relocate_tests(db, society_settings)
    _pass_qa_and_security(db, cand)
    real = provider.replay_files

    def lost_answer(*a, **k):
        real(*a, **k)
        raise pm.ProviderTransient("timeout after the ref moved")

    provider.replay_files = lost_answer
    drive(db_factory, society_settings, provider, rounds=1)
    db.refresh(promo)
    assert promo.evidence[rework.PENDING]["replay"]["parent"] == old_remote
    provider.replay_files = real
    promo.lease_expires_at = None  # the transient backoff has passed
    db.commit()
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert promo.candidate_sha == provider.prs[cand.branch_name]["head_sha"] != old_remote
    assert _ev(promo.status) not in ("superseded", "rejected") and promo.evidence[rework.DONE][-1]["kind"] == "test_relocation"
    assert len([c for c in provider.calls if c[0] == "replay_files"]) == 1, "adopted, not replayed twice"


# ── item 4: CI red on an up-to-date head goes back to the Builder ──


def _ci_red(db, db_factory, society_settings, temp_repo):
    _base(temp_repo, {PAGE: "<h1>AgentNet</h1>\n", GUARD_TEST: GUARD})
    report = seed_society(db)
    cand, ws = _candidate(db, society_settings, report, {PAGE: "<h1>AgentNet</h1>\n<a>List your agent</a> pay in credits or USDC\n", DASH_TEST: CTA_TEST})
    promo, provider = _open_pr(db, db_factory, society_settings, report, cand, ws)
    provider.set_ci(cand.branch_name, "failed")
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    db.refresh(cand)
    return report, cand, ws, promo, provider


def test_ci_red_on_an_up_to_date_head_is_reproduced_and_handed_back_to_the_builder(db, db_factory, society_settings, temp_repo):
    report, cand, ws, promo, provider = _ci_red(db, db_factory, society_settings, temp_repo)
    failing = f"{GUARD_TEST}::test_page_never_says_credits_or_usdc"
    assert _ev(promo.status) == "ci_failed"
    assert _ev(cand.status) == "qa_failed" and cand.qa_report["ci_rework"] is True
    assert any(f.startswith(f"ci_failed: {failing}") for f in cand.qa_report["failures"])
    assert cand.qa_report["attempts"] == 1, "exactly one more QA attempt for the fix"
    assert failing in cand.spec["acceptance_tests"] and cand.spec["revisions"][-1]["kind"] == "ci_fix"
    assert cand.spec["files_allowed"] == [PAGE, DASH_TEST], "more criteria, never a wider scope"
    ev = db.query(SocietyEvent).filter(SocietyEvent.event_type == "code_candidate.qa_failed", SocietyEvent.subject_id == cand.id).one()
    assert ev.payload["ci_rework"] is True and ev.correlation_id != cand.correlation_id, "a fresh story for the Builder"
    assert promo.evidence[rework.PENDING]["kind"] == "ci_fix"

    # the Builder fixes the product file (the guard test is not its to edit)
    (ws.path / PAGE).write_text("<h1>AgentNet</h1>\n<a>List your agent</a>\n")
    head = ws_mod.commit_all(ws, "fix")
    cand.head_sha, cand.changed_files = head, ws_mod.changed_files(ws)
    db.commit()
    _pass_qa_and_security(db, cand)
    provider.set_ci(cand.branch_name, "passed")
    old_remote = promo.candidate_sha
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    replays = [c for c in provider.calls if c[0] == "replay_files"]
    assert len(replays) == 1 and replays[0][3] == [PAGE], "only the file the fix changed"
    assert provider.parents[promo.candidate_sha] == [old_remote] and promo.external_pr_number == 101
    assert promo.evidence[rework.DONE][-1]["kind"] == "ci_fix" and _ev(promo.status) in ("ci_pending", "ci_passed", "awaiting_approval")


def test_a_builder_decline_during_a_ci_rework_closes_its_own_pr(db, db_factory, society_settings, temp_repo):
    _, cand, ws, promo, provider = _ci_red(db, db_factory, society_settings, temp_repo)
    cand.status, cand.error = "rejected", "declined by Society_Builder (acceptance_unsatisfiable): the guard forbids the phrase the ticket asks for"
    db.commit()
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    assert _ev(promo.status) == "rejected" and promo.failure_reason.startswith("rework_ended")
    assert provider.prs[cand.branch_name]["closed"] is True
    assert ("close_pr", promo.external_pr_number) == provider.calls[[c[0] for c in provider.calls].index("close_pr")][:2]


def test_ci_red_that_does_not_reproduce_is_left_to_the_owner_once(db, db_factory, society_settings, temp_repo):
    _base(temp_repo, {PAGE: "<h1>AgentNet</h1>\n", GUARD_TEST: GUARD})
    report = seed_society(db)
    cand, ws = _candidate(db, society_settings, report, {PAGE: "<h1>AgentNet</h1>\n<a>List your agent</a>\n", DASH_TEST: CTA_TEST})
    promo, provider = _open_pr(db, db_factory, society_settings, report, cand, ws)
    provider.set_ci(cand.branch_name, "failed")
    drive(db_factory, society_settings, provider)
    db.refresh(promo)
    db.refresh(cand)
    assert _ev(cand.status) == "ready" and ws_mod.head_sha(ws) == cand.head_sha
    assert "no failure" in promo.evidence[rework.SKIPPED]["ci_fix"]["reason"]
    assert promo.evidence[rework.SKIPPED]["ci_fix"]["head"] == promo.candidate_sha, "checked once per PR head"


def test_the_rework_controller_is_a_judge_only_the_owner_merges():
    assert "services/registry/app/society/rework.py" in JUDGE_PATHS
    assert "services/registry/app/society/rework.py" in OWNER_MERGE_PATTERNS
    assert judges_not_owner_only() == []


def test_the_github_replay_is_one_fast_forward_commit_after_a_version_check(monkeypatch):
    from services.registry.app.society.promotion_github import GitHubPromotionProvider

    from .test_github_credentials_and_push import FakeCreds, gh_settings

    s = gh_settings(monkeypatch)
    calls = []
    remote = {"tests/society/test_x.py": "aaa"}

    def transport(method, url, payload):
        calls.append((method, url.split("/repos/owner/repo")[1], payload))
        if method == "GET" and "/contents/" in url:
            path = url.split("/contents/")[1]
            return (200, {"type": "file", "sha": remote[path]}) if path in remote else (404, {})
        if url.endswith("/git/commits/parent1"):
            return (200, {"tree": {"sha": "tree1"}})
        if url.endswith("/git/blobs"):
            return (201, {"sha": "blob2"})
        if url.endswith("/git/trees"):
            return (201, {"sha": "tree2"})
        if url.endswith("/git/commits"):
            return (201, {"sha": "commit2"})
        return (200, {})

    p = GitHubPromotionProvider(s, transport=transport, credentials=FakeCreds(["t"]))
    promo = type("P", (), {"external_branch": f"{s.branch_prefix.rstrip('/')}/abc", "external_pr_number": 7})()
    changes = {"tests/society/test_x.py": None, "tests/dashboard/test_x.py": b"def test_x():\n    pass\n"}
    expected = {"tests/society/test_x.py": ["aaa"], "tests/dashboard/test_x.py": [None]}
    assert p.replay_files(promo, "parent1", changes, expected, "rework") == "commit2"
    writes = [(m, u, j) for m, u, j in calls if m != "GET"]
    assert [w[1] for w in writes] == ["/git/blobs", "/git/trees", "/git/commits", f"/git/refs/heads/{promo.external_branch}"]
    assert writes[2][2]["parents"] == ["parent1"] and writes[3][2] == {"sha": "commit2", "force": False}
    assert {e["path"]: e["sha"] for e in writes[1][2]["tree"]} == {"tests/dashboard/test_x.py": "blob2", "tests/society/test_x.py": None}

    calls.clear()
    remote["tests/society/test_x.py"] = "zzz"  # edited by someone else
    import pytest

    with pytest.raises(pm.ProviderConflict):
        p.replay_files(promo, "parent1", changes, expected, "rework")
    assert all(m == "GET" for m, _, _ in calls), "nothing written on a conflict"
    with pytest.raises(pm.ProviderRefused):
        p.replay_files(type("P", (), {"external_branch": "main", "external_pr_number": 7})(), "parent1", changes, expected, "rework")


def test_the_builder_may_decline_only_a_ci_rework_among_promoted_candidates(db, SessionLocal, code_settings, grants_with_no_cooldown):
    from services.registry.app.models import AgentCapabilityGrant, CodePromotion

    from .test_builder_decline import _decline, _intent_of, _live_deadlock, _run_as, _seed

    report = _seed(db, grants_with_no_cooldown)
    db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents["builder"]).update({"max_runs_per_hour": 100})
    db.commit()
    _, plain = _live_deadlock(db, report)
    db.add(CodePromotion(candidate_id=plain.id, correlation_id=uuid.uuid4(), risk_tier="amber", provider="fake", status="ci_failed", external_pr_number=7))
    _, reworked = _live_deadlock(db, report)
    db.add(CodePromotion(candidate_id=reworked.id, correlation_id=uuid.uuid4(), risk_tier="amber", provider="fake", status="ci_failed", external_pr_number=8,
                         evidence={rework.PENDING: {"kind": "ci_fix", "failing": ["tests/dashboard/test_x.py::test_y"]}}))
    db.commit()
    refused = _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [_decline(plain.id, reason="acceptance_unsatisfiable", blocking=())]))
    assert _ev(refused.execution_status) == "failed" and "promotion" in refused.error
    ok = _intent_of(db, _run_as(db, SessionLocal, code_settings, "builder", [_decline(reworked.id, reason="acceptance_unsatisfiable", blocking=())]))
    assert _ev(ok.execution_status) == "executed", ok.error
    assert _ev(db.get(CodeCandidate, reworked.id).status) == "rejected"
