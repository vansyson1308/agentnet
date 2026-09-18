"""Phase 3.1 — ONE autonomous self-improvement control plane.

The general worker (timeouts, reputation, presence, simulations) must never
consume Society improvement proposals, write a file backlog or generate a
competing proposal stream; nothing may start the legacy synthetic agents.
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import re
import subprocess
import uuid
from datetime import timedelta

import pytest

from services.registry.app.models import AgentRun, ImprovementProposal, ProposalSource, ProposalStatus, SocietyEvent, TaskSession, TaskStatus
from services.registry.app.society.cognition import ScriptedRoleModel
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker
from services.registry.app.society.world import ingest_task_outcomes

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _tracked(pattern: str) -> list:
    out = subprocess.run(["git", "ls-files", "--", pattern], cwd=REPO, capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


# ── structural: the legacy paths are archived and unreachable ──────────


def test_worker_has_no_reflection_or_backlog_bridge():
    src = (REPO / "services/worker/app/worker.py").read_text(encoding="utf-8")
    for token in ("reflection_loop import", "run_reflection_loop(", "convert_proposals_to_backlog", "REFLECTION_LOOP_INTERVAL_SEC", "AGENT_BACKLOG"):
        assert token not in src, token
    assert not (REPO / "services/worker/app/reflection_loop.py").exists()
    mod = importlib.import_module("services.worker.app.worker")
    assert not hasattr(mod, "run_reflection_loop") and not hasattr(mod, "convert_proposals_to_backlog")


def test_no_file_backlog_or_synthetic_agents_in_the_active_tree():
    assert not (REPO / "AGENT_BACKLOG.md").exists(), "the file backlog lives only under legacy/"
    assert not (REPO / "agents").exists(), "synthetic agents live only under legacy/synthetic-agents/"
    assert (REPO / "legacy/hermes/AGENT_BACKLOG.md").exists()
    assert (REPO / "legacy/synthetic-agents/poll_agent.py").exists()
    active = [p for p in _tracked("services/**") + _tracked("sdk/**") + _tracked("examples/**") + _tracked("scripts/**") if p.endswith(".py")]
    assert active
    for rel in active:
        text = (REPO / rel).read_text(encoding="utf-8", errors="ignore")
        assert "AGENT_BACKLOG" not in text, rel
        assert "from legacy" not in text and "import legacy" not in text, rel
        assert "poll_agent" not in text and "synthetic-agents" not in text, rel


@pytest.mark.parametrize("rel", ["docker-compose.yml", "docker-compose.demo.yml", "docker-compose.staging.yml", "docker-compose.staging.shared-infra.yml", "Makefile"] + [p for p in _tracked("deploy/*.sh") + _tracked("scripts/**/*.sh") + _tracked("deploy/*.yml")])
def test_no_deployment_or_script_starts_legacy_activity(rel):
    p = REPO / rel
    if not p.exists():
        pytest.skip(f"{rel} not present")
    text = p.read_text(encoding="utf-8", errors="ignore")
    for token in ("poll_agent", "echo_agent", "storyteller_agent", "synthetic-agents", "AGENT_BACKLOG", "reflection_loop", "hermes_planner", "hermes_builder", "legacy/"):
        assert token not in text, (rel, token)


def test_env_example_has_no_retired_reflection_settings():
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    assert not re.search(r"^REFLECTION_LOOP_\w+=", text, re.M)
    assert not re.search(r"^AGENT_BACKLOG_PATH=", text, re.M)


# ── behavioural: the general worker never touches Society proposals ────


def _run_general_worker(monkeypatch, SessionLocal, *, passes: int = 3) -> None:
    """Drive the REAL general worker main loop for a few passes against the
    Society test database (its own session factory is swapped for the
    test's; Redis is unavailable, which the worker tolerates)."""
    mod = importlib.import_module("services.worker.app.worker")
    monkeypatch.setattr(mod, "start_http_server", lambda port: None)

    async def _no_redis():
        raise ConnectionError("redis down")

    monkeypatch.setattr(mod, "init_redis", _no_redis)
    monkeypatch.setattr(mod, "SessionLocal", SessionLocal)
    waits = []

    async def _wait(stop_event, seconds):
        waits.append(seconds)
        if len(waits) >= passes:
            stop_event.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(mod, "wait_or_stop", _wait)
    asyncio.run(mod.main())
    assert len(waits) == passes


def test_general_worker_ticks_leave_society_proposals_untouched(db, SessionLocal, society_settings, grants_with_no_cooldown, monkeypatch, tmp_path):
    report = seed_society(db)
    grants_with_no_cooldown()
    # A Society proposal (as the Scout would create it) plus its event, NOT yet dispatched.
    prop = ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=report.agents["scout"], source=ProposalSource.AUDIT, title="Improve: translate", problem="p", proposed_change="c", status=ProposalStatus.PROPOSED, target_scope="platform", importance=70)
    db.add(prop)
    db.commit()
    corr = uuid.uuid4()
    emit_event(db, event_type=EventType.PROPOSAL_CREATED, payload={"proposal_id": str(prop.id), "title": prop.title, "importance": 70}, actor_type="agent", subject_type="improvement_proposal", subject_id=prop.id, correlation_id=corr, idempotency_key=f"t-prop-{prop.id}")
    db.commit()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_BACKLOG_PATH", str(tmp_path / "AGENT_BACKLOG.md"))

    _run_general_worker(monkeypatch, SessionLocal, passes=3)

    db.expire_all()
    prop = db.query(ImprovementProposal).filter(ImprovementProposal.id == prop.id).one()
    assert _ev(prop.status) == "PROPOSED", "the general worker must not move a Society proposal"
    assert not list(tmp_path.glob("*BACKLOG*")) and not (REPO / "AGENT_BACKLOG.md").exists()
    assert db.query(ImprovementProposal).count() == 1, "the worker generates no proposals of its own"

    # The Society lifecycle proceeds normally: Governor approves, Architect receives proposal.approved once.
    worker = SocietyWorker(SessionLocal, settings=society_settings, model=ScriptedRoleModel(), worker_id="w", telemetry_enabled=False)
    asyncio.run(worker.run_until_idle(max_cycles=4))
    db.expire_all()
    prop = db.query(ImprovementProposal).filter(ImprovementProposal.id == prop.id).one()
    assert _ev(prop.status) in ("APPROVED", "CONVERTED_TO_TASK"), prop.status
    approved = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.PROPOSAL_APPROVED).all()
    assert len(approved) == 1
    architect_runs = db.query(AgentRun).filter(AgentRun.role == "architect", AgentRun.event_id == approved[0].id).all()
    assert len(architect_runs) == 1, "exactly one Architect workstream"
    assert db.query(ImprovementProposal).count() == 1, "no duplicate workstream"


def test_same_failure_evidence_yields_one_open_workstream(db, SessionLocal, make_agent, society_settings, grants_with_no_cooldown, monkeypatch, tmp_path):
    seed_society(db)
    grants_with_no_cooldown()
    a, b = make_agent("Ext_A"), make_agent("Ext_B")
    for _ in range(3):
        db.add(TaskSession(id=uuid.uuid4(), trace_id=uuid.uuid4(), span_id=uuid.uuid4(), caller_agent_id=a.id, callee_agent_id=b.id, capability="summarise", input={"x": 1}, escrow_amount=0, status=TaskStatus.FAILED, timeout_at=utcnow(), completed_at=utcnow() - timedelta(minutes=2), error_message="upstream 500"))
    db.commit()
    assert ingest_task_outcomes(db, lookback_seconds=3600) == 3
    assert ingest_task_outcomes(db, lookback_seconds=3600) == 0, "ingestion is idempotent per task"
    worker = SocietyWorker(SessionLocal, settings=society_settings, model=ScriptedRoleModel(), worker_id="w", telemetry_enabled=False)
    worker.routing = {EventType.TASK_FAILED: ["scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=6))
    monkeypatch.chdir(tmp_path)
    _run_general_worker(monkeypatch, SessionLocal, passes=2)
    db.expire_all()
    open_props = db.query(ImprovementProposal).filter(ImprovementProposal.title == "Improve: summarise").all()
    assert len(open_props) == 1, [(_ev(p.status), p.source) for p in open_props]
    assert _ev(open_props[0].status) == "PROPOSED"
    assert db.query(ImprovementProposal).count() == 1
    assert not list(tmp_path.glob("*BACKLOG*"))
    scout_runs = db.query(AgentRun).filter(AgentRun.role == "scout").count()
    assert scout_runs == 3, "every signal is observed; only the first opens a workstream"
