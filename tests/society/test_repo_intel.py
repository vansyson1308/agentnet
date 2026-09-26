"""Repository intelligence: read-only, path-safe, bounded, deduplicated, audited."""

from __future__ import annotations

import asyncio
import os
import pathlib
import uuid

import pytest

from services.registry.app.models import (
    Agent,
    AgentIntent,
    AgentRun,
    CodeCandidate,
    ImprovementProposal,
    ProposalSource,
    ProposalStatus,
    SocietyEvent,
)
from services.registry.app.society import repo_intel as ri
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker


def _ev(v):
    return v.value if hasattr(v, "value") else v


# ── pure path / bounds tests ───────────────────────────────────────────


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", ".git/config", ".env", ".env.local", "services/../../x", "~/x", "a\\b", "config/secrets/prod.json", "id_rsa", "keys/server.pem", "", "a/./b", "x\0y"])
def test_reads_refuse_unsafe_or_secret_paths(code_repo, bad):
    with pytest.raises(ri.RepoReadError):
        ri.read_file(code_repo, bad)


def test_symlink_escape_is_refused(code_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (code_repo / "link.txt").symlink_to(outside)
    with pytest.raises(ri.RepoReadError):
        ri.read_file(code_repo, "link.txt")
    (code_repo / "linkdir").symlink_to(tmp_path)
    with pytest.raises(ri.RepoReadError):
        ri.read_file(code_repo, "linkdir/outside.txt")
    # the tree listing never follows symlinks either
    names = {e["path"] for e in ri.list_tree(code_repo, "", depth=1).data["entries"]}
    assert "link.txt" not in names and "linkdir" not in names


def test_binary_and_git_internals_are_refused(code_repo):
    (code_repo / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(ri.RepoReadError, match="binary"):
        ri.read_file(code_repo, "blob.bin")
    assert not any(e["path"].startswith(".git") for e in ri.list_tree(code_repo, "", depth=3).data["entries"])
    assert all(".git/" not in h["path"] for h in ri.search(code_repo, "ref:").data["hits"])


def test_outputs_are_bounded(code_repo):
    big = code_repo / "big.txt"
    big.write_text("x" * 100_000)
    r = ri.read_file(code_repo, "big.txt", max_bytes=10_000_000)  # caller cannot raise the cap
    assert r.truncated and r.bytes_returned <= ri.DEFAULT_MAX_BYTES
    (code_repo / "many.py").write_text("\n".join(f"needle_{i} = {i}" for i in range(500)))
    s = ri.search(code_repo, "needle_", max_results=10_000)
    assert s.truncated and s.data["count"] <= ri.DEFAULT_MAX_RESULTS
    rr = ri.read_range(code_repo, "many.py", 1, 100_000)
    assert rr.data["end"] - rr.data["start"] + 1 <= ri.DEFAULT_MAX_LINES
    t = ri.list_tree(code_repo, "", depth=99, max_entries=10_000)
    assert t.data["count"] <= ri.DEFAULT_MAX_ENTRIES


def test_search_is_deterministic_and_untrusted(code_repo):
    a = ri.search(code_repo, "def parse_bool", glob="*.py")
    b = ri.search(code_repo, "def parse_bool", glob="*.py")
    assert a.data == b.data and a.data["count"] == 1 and a.data["hits"][0]["path"] == "app/textutil.py"
    d = a.to_dict()
    assert d["_untrusted"] is True and d["source"].startswith("repo:")
    with pytest.raises(ri.RepoReadError):
        ri.search(code_repo, "(", regex=True)


# ── runtime integration: intents, audit rows, wake turns, bounds ───────


def _seed(db, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    return report


def _run(db, SessionLocal, settings, model, payload, *, roles=("architect",), max_cycles=8):
    correlation = uuid.uuid4()
    ev = emit_event(db, event_type="t.read", payload=payload, correlation_id=correlation, idempotency_key=f"t-read-{uuid.uuid4()}")
    db.commit()
    routing = {"t.read": list(roles), EventType.REPO_READ_RESULT: list(roles)}
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w-read", telemetry_enabled=False)
    worker.routing = routing
    stats = asyncio.run(worker.run_until_idle(max_cycles=max_cycles))
    return ev, stats


def test_read_intents_are_audited_and_wake_one_turn(db, SessionLocal, code_settings, grants_with_no_cooldown):
    _seed(db, grants_with_no_cooldown)
    script = {
        "architect": [
            {"decision_summary": "look", "intents": [{"type": "SEARCH_REPO", "payload": {"pattern": "parse_bool", "glob": "*.py"}}, {"type": "READ_REPO_RANGE", "payload": {"path": "app/textutil.py", "start": 1, "end": 5}}], "sleep_for_seconds": 1},
            {"decision_summary": "done", "intents": [], "sleep_for_seconds": 1},
        ]
    }
    model = FakeModel(script)
    ev, stats = _run(db, SessionLocal, code_settings, model, {"x": 1})
    reads = db.query(AgentIntent).filter(AgentIntent.intent_type.in_(["SEARCH_REPO", "READ_REPO_RANGE"])).all()
    assert len(reads) == 2 and all(_ev(r.execution_status) == "executed" for r in reads)
    search = next(r for r in reads if r.intent_type == "SEARCH_REPO")
    assert search.result["result"]["_untrusted"] is True
    assert search.result["result"]["data"]["hits"][0]["path"] == "app/textutil.py"
    wakes = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.REPO_READ_RESULT).all()
    assert len(wakes) == 1, "two reads in one run produce ONE targeted wake (one engineering turn)"
    assert wakes[0].subject_type == "agent"
    # the second turn saw the read results in its context
    second = model.calls[1]
    ops = [r["data"]["op"] for r in second.repo_reads]
    assert ops == ["search", "read_range"]
    assert second.engineering["turns_used"] == 1
    assert len(model.calls) == 2


def test_identical_search_is_suppressed_without_a_new_turn(db, SessionLocal, code_settings, grants_with_no_cooldown):
    _seed(db, grants_with_no_cooldown)
    same = {"type": "SEARCH_REPO", "payload": {"pattern": "parse_bool", "glob": "*.py"}}
    script = {"architect": [{"decision_summary": "a", "intents": [same], "sleep_for_seconds": 1}, {"decision_summary": "b", "intents": [dict(same)], "sleep_for_seconds": 1}, {"decision_summary": "c", "intents": [], "sleep_for_seconds": 1}]}
    model = FakeModel(script)
    _run(db, SessionLocal, code_settings, model, {"x": 1})
    wakes = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.REPO_READ_RESULT).count()
    assert wakes == 1, "the repeated identical search must not wake a third turn"
    reads = db.query(AgentIntent).filter(AgentIntent.intent_type == "SEARCH_REPO").order_by(AgentIntent.created_at).all()
    assert len(reads) == 2 and reads[1].result["result"]["duplicate"] is True
    assert len(model.calls) == 2


def test_per_run_and_per_correlation_read_caps_fail_closed(db, SessionLocal, code_settings, grants_with_no_cooldown, monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    monkeypatch.setenv("SOCIETY_MAX_REPO_READS_PER_RUN", "2")
    monkeypatch.setenv("SOCIETY_MAX_REPO_READS_PER_CORRELATION", "3")
    monkeypatch.setenv("SOCIETY_MAX_ENGINEERING_TURNS", "2")
    reset_settings_cache()
    settings = SocietySettings()
    _seed(db, grants_with_no_cooldown)
    read = {"type": "READ_REPO_RANGE", "payload": {"path": "app/textutil.py", "start": 1, "end": 3}}
    script = {"architect": [
        {"decision_summary": "3 reads", "intents": [dict(read), {"type": "READ_REPO_RANGE", "payload": {"path": "app/textutil.py", "start": 4, "end": 6}}, {"type": "READ_REPO_RANGE", "payload": {"path": "app/textutil.py", "start": 7, "end": 9}}], "sleep_for_seconds": 1},
        {"decision_summary": "more", "intents": [{"type": "READ_REPO_RANGE", "payload": {"path": "app/textutil.py", "start": 10, "end": 12}}, {"type": "READ_REPO_RANGE", "payload": {"path": "app/textutil.py", "start": 13, "end": 15}}], "sleep_for_seconds": 1},
        {"decision_summary": "again", "intents": [{"type": "READ_REPO_RANGE", "payload": {"path": "app/textutil.py", "start": 16, "end": 18}}], "sleep_for_seconds": 1},
        {"decision_summary": "never", "intents": [], "sleep_for_seconds": 1},
    ]}
    model = FakeModel(script)
    _run(db, SessionLocal, settings, model, {"x": 1}, max_cycles=12)
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == "READ_REPO_RANGE").order_by(AgentIntent.created_at).all()
    statuses = [_ev(r.execution_status) for r in rows]
    assert statuses.count("denied") >= 1, statuses                      # third read in run 1 denied by the per-run cap
    assert statuses.count("executed") <= 3, statuses                    # correlation cap 3
    assert any(_ev(r.execution_status) == "failed" and "budget exhausted" in (r.error or "") for r in rows) or statuses.count("executed") == 3
    wakes = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.REPO_READ_RESULT).count()
    assert wakes <= 2, "engineering turns are capped"
    assert len(model.calls) <= 3


def test_reads_target_candidate_worktree_when_candidate_given(db, SessionLocal, code_settings, grants_with_no_cooldown):
    from services.registry.app.society.engineering import workspace as ws_mod
    from services.registry.app.society.intents import FileEdit

    report = _seed(db, grants_with_no_cooldown)
    cid = uuid.uuid4()
    cand = CodeCandidate(id=cid, correlation_id=uuid.uuid4(), title="t", spec={"files_allowed": ["app/textutil.py"], "acceptance_tests": ["tests/test_textutil.py"], "kind": "code"}, status="requested", requested_by_agent_id=report.agents["architect"])
    db.add(cand)
    db.commit()
    ws = ws_mod.ensure_workspace(code_settings, cid)
    ws_mod.apply_edits(ws, [FileEdit(path="app/textutil.py", content="MARKER_IN_WORKTREE = 1\n")], allowed=["app/textutil.py"])
    ws_mod.commit_all(ws, "edit")
    script = {"builder": [{"decision_summary": "read ws", "intents": [{"type": "READ_REPO_FILE", "payload": {"path": "app/textutil.py", "candidate_id": str(cid)}}, {"type": "READ_REPO_FILE", "payload": {"path": "app/textutil.py"}}], "sleep_for_seconds": 1}, {"decision_summary": "x", "intents": [], "sleep_for_seconds": 1}]}
    model = FakeModel(script)
    _run(db, SessionLocal, code_settings, model, {"candidate_id": str(cid)}, roles=("builder",))
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == "READ_REPO_FILE").order_by(AgentIntent.seq).all()
    assert "MARKER_IN_WORKTREE" in rows[0].result["result"]["data"]["content"]
    assert "MARKER_IN_WORKTREE" not in rows[1].result["result"]["data"]["content"], "the trusted base is untouched"
    db.refresh(cand)
    assert cand.repo_reads == 1


# ── continuing a truncated read ────────────────────────────────────────
#
# A live Builder read a byte-truncated preview of a 92-line document, then
# asked READ_REPO_RANGE for "line" 12000 — the byte offset it had just read to.
# The range came back empty, it read again, the loop breaker tripped, and its
# candidate was stranded in `requested`. Nothing the model could see said that
# one primitive counts BYTES and its neighbour counts LINES.


def test_truncated_read_file_says_how_to_continue(code_repo):
    """The cut is by bytes and can land mid-line, so a truncated read has to
    hand back a LINE to resume from — deriving one from a byte count is the
    guess that stranded the candidate."""
    rel = "services/app/big.py"
    target = code_repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"line {i} padding padding padding\n" for i in range(1, 401)), encoding="utf-8")

    head = ri.read_file(code_repo, rel, max_bytes=500)
    assert head.truncated is True
    assert head.data["total_lines"] == 400, "a truncated read must state the WHOLE file's length"
    nxt = head.data["next_line"]
    assert 1 <= nxt <= head.data["lines"] + 1

    tail = ri.read_range(code_repo, rel, nxt, nxt + 20)
    assert tail.data["content"].strip(), "continuing at next_line must return real content"
    assert tail.data["total_lines"] == 400

    # an untruncated read carries no continuation: there is nothing to continue
    whole = ri.read_file(code_repo, rel, max_bytes=32000)
    assert whole.truncated is False and "next_line" not in whole.data


def test_read_range_past_eof_is_empty_and_reports_the_real_length(code_repo):
    """The exact live call: a line number taken from a byte offset. It must not
    look like a file that simply has nothing there."""
    rel = "services/app/small.py"
    target = code_repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"line {i}\n" for i in range(1, 93)), encoding="utf-8")

    out = ri.read_range(code_repo, rel, 12000, 12200)
    assert out.data["content"] == ""
    assert out.data["total_lines"] == 92, "the reader must be able to tell it overshot"


# ── a read result wakes the reader only (staging 2026-09-26) ────────────


def _woken_by(db, event_id):
    return sorted(n for (n,) in db.query(Agent.name).join(AgentRun, AgentRun.agent_id == Agent.id).filter(AgentRun.event_id == event_id).all())


def test_a_read_result_wakes_only_the_reading_agent(db, SessionLocal, code_settings, grants_with_no_cooldown):
    """repo.read.result is the reading agent's next engineering turn. The
    default routing subscribes the Architect, the Builder and Security to the
    type (each reads for itself); another agent's read must wake none of
    them -- they would spend runs saying "not for me" on an untrusted preview."""
    _seed(db, grants_with_no_cooldown)
    model = FakeModel({"architect": [
        {"decision_summary": "look", "intents": [{"type": "SEARCH_REPO", "payload": {"pattern": "parse_bool", "glob": "*.py"}}], "sleep_for_seconds": 1},
        {"decision_summary": "done", "intents": [], "sleep_for_seconds": 1},
    ]})
    worker = SocietyWorker(SessionLocal, settings=code_settings, model=model, worker_id="w-read", telemetry_enabled=False)
    assert {"architect", "builder", "security"} <= set(worker.routing[EventType.REPO_READ_RESULT])
    worker.routing = {**worker.routing, "t.read": ["architect"]}
    emit_event(db, event_type="t.read", payload={"x": 1}, idempotency_key=f"t-read-{uuid.uuid4()}")
    db.commit()
    asyncio.run(worker.run_until_idle(max_cycles=8))
    wake = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.REPO_READ_RESULT).one()
    assert _woken_by(db, wake.id) == ["Society_Architect"]
    assert {c.agent["name"] for c in model.calls} == {"Society_Architect"}


def test_the_live_story_reaches_the_builder_within_the_staging_run_budget(db, SessionLocal, code_settings, grants_with_no_cooldown, monkeypatch):
    """Staging 2026-09-26 11:06-11:08Z (correlation b8db5936, candidate a2788678): Scout, Governor,
    then the Architect grounding its design in three reads before
    REQUEST_CODE_CHANGE. Each read also woke the Builder and Security, so the
    correlation reached SOCIETY_MAX_RUNS_PER_CORRELATION=12 exactly when
    code_change.requested arrived: it was ignored by the loop breaker and the
    candidate stranded in REQUESTED (as candidate 9da14a08 had earlier that day).
    Same story, same cap: the request reaches the Builder."""
    from services.registry.app.society.config import SocietySettings, reset_settings_cache
    from services.registry.app.society.runs import runs_in_correlation

    monkeypatch.setenv("SOCIETY_MAX_RUNS_PER_CORRELATION", "12")
    reset_settings_cache()
    settings = SocietySettings()
    report = _seed(db, grants_with_no_cooldown)
    prop = ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=report.agents["scout"], source=ProposalSource.AUDIT, title="parse_bool rejects yes/on", problem="p", proposed_change="c", status=ProposalStatus.APPROVED, target_scope="platform", importance=70)
    db.add(prop)
    db.commit()
    request = {"type": "REQUEST_CODE_CHANGE", "payload": {"title": "fix parse_bool", "proposal_id": str(prop.id), "spec": {
        "kind": "code", "description": "accept yes/on", "files_allowed": ["app/textutil.py"],
        "acceptance_tests": ["tests/acceptance/test_parse_bool_regression.py"], "expected_effect": "parse_bool('yes') is True",
    }}}
    model = FakeModel({"architect": [
        {"decision_summary": "search", "intents": [{"type": "SEARCH_REPO", "payload": {"pattern": "parse_bool", "glob": "*.py"}}], "sleep_for_seconds": 1},
        {"decision_summary": "read", "intents": [{"type": "READ_REPO_FILE", "payload": {"path": "app/textutil.py"}}], "sleep_for_seconds": 1},
        {"decision_summary": "range", "intents": [{"type": "READ_REPO_RANGE", "payload": {"path": "app/textutil.py", "start": 1, "end": 5}}], "sleep_for_seconds": 1},
        {"decision_summary": "design", "intents": [request], "sleep_for_seconds": 1},
    ]})
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w-story", telemetry_enabled=False)
    worker.routing = {**worker.routing, "t.story": ["scout", "governor", "architect"]}  # the live story's first three runs
    root = emit_event(db, event_type="t.story", payload={"x": 1}, idempotency_key=f"t-story-{uuid.uuid4()}")
    db.commit()
    corr = root.correlation_id
    asyncio.run(worker.run_until_idle(max_cycles=30))

    reads = db.query(AgentIntent).filter(AgentIntent.intent_type.in_(["SEARCH_REPO", "READ_REPO_FILE", "READ_REPO_RANGE"])).all()
    assert len(reads) == 3 and all(_ev(r.execution_status) == "executed" for r in reads)
    requested = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CHANGE_REQUESTED, SocietyEvent.correlation_id == corr).one()
    assert _ev(requested.status) != "ignored", requested.dispatch_note
    assert _woken_by(db, requested.id) == ["Society_Builder"]
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED, SocietyEvent.correlation_id == corr).count() == 0
    # root: 3 runs; each read: 1 run for its reader; the request: 1 Builder run
    assert runs_in_correlation(db, corr) == 3 + 3 + 1
    assert db.query(CodeCandidate).filter(CodeCandidate.proposal_id == prop.id).count() == 1
