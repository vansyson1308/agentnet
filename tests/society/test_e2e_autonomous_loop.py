"""
Deterministic end-to-end proof of the autonomous society loop.

The test injects exactly ONE event (platform.metric.anomaly). Every later
row — Scout run, proposal, Governor review, Architect design, Builder
worktree commit, QA verdict, memories — must be produced by the runtime
pipeline (dispatch -> claim -> context -> model -> policy -> execute ->
events). Nothing is inserted by the test after the first event.

The model is the offline ScriptedRoleModel (deterministic rules); the
engineering loop runs against a throw-away git repository.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid

import pytest

from services.registry.app.models import (
    AgentIntent,
    AgentRun,
    CodeCandidate,
    ImprovementProposal,
    MemoryItem,
    SocietyEvent,
    Span,
)
from services.registry.app.society.cognition import ScriptedRoleModel
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker


def _ev(v):
    return v.value if hasattr(v, "value") else v


@pytest.mark.timeout(300)
def test_single_event_drives_full_engineering_loop(db, SessionLocal, society_settings, temp_repo, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    assert set(report.agents) == {"governor", "scout", "architect", "builder", "qa", "security"}

    # ── the ONLY external input ──
    correlation = uuid.uuid4()
    ev = emit_event(
        db,
        event_type=EventType.PLATFORM_METRIC_ANOMALY,
        payload={
            "metric": "task_failure_rate",
            "value": 0.42,
            "threshold": 0.10,
            "description": "task failure rate above threshold for 15 minutes",
            "severity_score": 70,
        },
        actor_type="system",
        correlation_id=correlation,
        idempotency_key="e2e-anomaly-1",
    )
    db.commit()

    worker = SocietyWorker(SessionLocal, settings=society_settings, model=ScriptedRoleModel(), worker_id="e2e-worker")
    stats = asyncio.run(worker.run_until_idle(max_cycles=40))

    # ── reconstruct the chain from the database ──
    runs = db.query(AgentRun).filter(AgentRun.correlation_id == correlation).order_by(AgentRun.created_at).all()
    by_role = {}
    for r in runs:
        by_role.setdefault(r.role, []).append(r)
    assert stats.runs_completed >= 5, stats.as_dict()
    for role in ("scout", "governor", "architect", "builder", "qa"):
        assert role in by_role, f"no run for {role}: {[(r.role, _ev(r.status), r.error) for r in runs]}"
        assert any(_ev(r.status) == "completed" for r in by_role[role]), role

    proposals = db.query(ImprovementProposal).all()
    assert len(proposals) == 1, [p.title for p in proposals]
    proposal = proposals[0]
    assert proposal.title == "Improve: task_failure_rate"
    assert proposal.proposed_by_agent_id == report.agents["scout"]
    assert _ev(proposal.status) == "CONVERTED_TO_TASK"

    candidates = db.query(CodeCandidate).filter(CodeCandidate.correlation_id == correlation).all()
    assert len(candidates) == 1
    cand = candidates[0]
    assert _ev(cand.status) == "ready", (cand.qa_report, cand.error)
    assert cand.builder_agent_id == report.agents["builder"]
    assert cand.qa_agent_id == report.agents["qa"]
    assert cand.builder_agent_id != cand.qa_agent_id
    assert cand.branch_name == f"agentnet-auto/{cand.id}"
    assert cand.qa_report["verdict"] == "pass"
    assert cand.changed_files == ["docs/society/candidates/improve-task-failure-rate.md"]

    # worktree branch exists in the temp repo; main is untouched
    branches = subprocess.run(["git", "branch", "--list", "agentnet-auto/*"], cwd=temp_repo, capture_output=True, text=True).stdout
    assert cand.branch_name in branches
    main_files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=temp_repo, capture_output=True, text=True).stdout
    assert "improve-task-failure-rate.md" not in main_files

    # every event after the first has a causation chain back to the initial event
    events = db.query(SocietyEvent).filter(SocietyEvent.correlation_id == correlation).order_by(SocietyEvent.created_at).all()
    types = [e.event_type for e in events]
    for expected in (
        EventType.PROPOSAL_CREATED,
        EventType.PROPOSAL_APPROVED,
        EventType.CODE_CHANGE_REQUESTED,
        EventType.CODE_CANDIDATE_BUILT,
        EventType.CODE_CANDIDATE_QA_PASSED,
        EventType.CODE_CANDIDATE_READY,
    ):
        assert expected in types, types
    ids = {e.id for e in events}
    for e in events:
        if e.id != ev.id and e.event_type != EventType.LOOP_BREAKER_TRIPPED:
            assert e.causation_id in ids, f"{e.event_type} has no causation inside the correlation"
            assert e.source_run_id is not None, e.event_type
    assert all(_ev(e.status) in ("processed", "ignored") for e in events), [(e.event_type, _ev(e.status)) for e in events]

    # intents were typed, adjudicated and executed
    intents = db.query(AgentIntent).join(AgentRun).filter(AgentRun.correlation_id == correlation).all()
    executed = {i.intent_type for i in intents if _ev(i.execution_status) == "executed"}
    for t in ("CREATE_IMPROVEMENT", "REVIEW_IMPROVEMENT", "REQUEST_CODE_CHANGE", "SUBMIT_CODE_CANDIDATE", "EVALUATE_CODE_CANDIDATE", "WRITE_MEMORY"):
        assert t in executed, (t, [(i.intent_type, _ev(i.execution_status), i.error) for i in intents])
    assert all(_ev(i.policy_decision) in ("allow", "deny", "invalid") for i in intents)

    # society learned something, and the trace is queryable through spans
    memories = db.query(MemoryItem).all()
    assert any(_ev(m.scope) == "SOCIETY" for m in memories)
    spans = db.query(Span).filter(Span.trace_id == ev.trace_id).all()
    assert any(s.event == "society.run" for s in spans)
    assert any(s.event == "society.intent.SUBMIT_CODE_CANDIDATE" for s in spans)
    # no hidden chain-of-thought: only bounded summaries are stored
    for r in runs:
        assert r.decision_summary is None or len(r.decision_summary) <= 1000
        assert r.context_digest and len(r.context_digest) == 64
