"""Execution-grounded memory (society/memory_grounding.py).

Regression for the live staging failure of 2026-09-26: a Scout decision held
``CREATE_IMPROVEMENT`` + ``WRITE_MEMORY`` ("new proposal raised"). The proposal
was refused (portfolio full), the memory was written anyway, and the next
Scout run declined the same critical public-surface signal as a "duplicate of
the 06:02 proposal" that never existed.

The invariant: a model-authored memory is admitted only if every
side-effecting intent of the same decision EXECUTED. No phrase matching: the
memory text below is identical in the admitted and the refused cases.
"""

from __future__ import annotations

import asyncio

import pytest

from services.registry.app.models import AgentCapabilityGrant, AgentIntent, ImprovementProposal, MemoryItem
from services.registry.app.society import approvals as ap
from services.registry.app.society import memory_grounding as mg
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.events import emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker


def _ev(v):
    return v.value if hasattr(v, "value") else v


CLAIM = {"type": "WRITE_MEMORY", "payload": {"title": "Triage: public.surface.anomaly - new proposal raised", "content": "Raised an improvement proposal for the failing public surface.", "importance": 45}}
EVIDENCE = {"signal": "public.surface.anomaly", "observed": "6/17 checks failing", "sample_size": 17, "actionable_reason": "critical contract items fail on two consecutive checks"}
PROPOSAL = {"type": "CREATE_IMPROVEMENT", "payload": {"title": "Public surface: login/register masked by landing redirect", "problem": "p", "proposed_change": "c", "importance": 70, "evidence": EVIDENCE}}
OBSERVATION = {"type": "WRITE_MEMORY", "payload": {"title": "Observed: 6/17 public checks failing", "content": "Structural observation only.", "importance": 30}}


# ── the decision itself (pure) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,admitted",
    [
        ("executed", True),
        ("failed", False),
        ("denied", False),
        ("awaiting_approval", False),
        ("approved", False),   # approved but not yet resumed: no outcome exists
        ("rejected", False),
        ("skipped", False),
        ("pending", False),
    ],
)
def test_a_memory_is_admitted_only_after_its_side_effect_executed(status, admitted):
    g = mg.decide(1, [(0, "CREATE_IMPROVEMENT", status), (1, "WRITE_MEMORY", "pending")])
    assert g.admitted is admitted
    if not admitted:
        assert "seq 0 CREATE_IMPROVEMENT is " + status in g.reason


def test_every_side_effect_counts_and_unknown_types_fail_closed():
    assert not mg.decide(2, [(0, "SEND_MESSAGE", "executed"), (1, "CREATE_IMPROVEMENT", "failed")]).admitted
    assert not mg.decide(1, [(0, "MADE_UP_INTENT", "denied")]).admitted, "an invalid intent the model emitted is a side effect"
    assert mg.decide(2, [(0, "SEND_MESSAGE", "executed"), (1, "CREATE_IMPROVEMENT", "executed")]).admitted


def test_observations_and_read_only_decisions_remain_supported():
    assert mg.decide(0, []).admitted, "a triage-only decision writes memory as before"
    reads_and_sleep = [(0, "READ_REPO_FILE", "failed"), (1, "SEARCH_REPO", "denied"), (2, "SLEEP", "executed"), (3, "WRITE_MEMORY", "failed")]
    assert mg.decide(4, reads_and_sleep).admitted, "reads, sleep and other memories are not side effects"


def test_memories_run_after_every_other_intent_of_the_decision():
    rows = [("WRITE_MEMORY", 0), ("CREATE_IMPROVEMENT", 1), ("WRITE_MEMORY", 2), ("SEND_MESSAGE", 3)]
    ordered = sorted(rows, key=lambda r: mg.execution_order_key(r[0], r[1]))
    assert ordered == [("CREATE_IMPROVEMENT", 1), ("SEND_MESSAGE", 3), ("WRITE_MEMORY", 0), ("WRITE_MEMORY", 2)]


# ── end to end through the real worker ─────────────────────────────────────


def _settings(monkeypatch, **env) -> SocietySettings:
    base = {"SOCIETY_RUNTIME_ENABLED": "true", "SOCIETY_MODEL_PROVIDER": "scripted", "SOCIETY_COMPANY_CYCLE_ENABLED": "true"}
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    return SocietySettings()


def _run_scout(db, SessionLocal, settings, decisions, event_type="public.surface.anomaly"):
    model = FakeModel({"Society_Scout": decisions})
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w", telemetry_enabled=False)
    for _ in decisions:
        emit_event(db, event_type=event_type, payload={"source": "public_surface_monitor", "failing_count": 6})
        db.commit()
        asyncio.run(worker.run_until_idle(max_cycles=10))
    db.expire_all()
    return model


def _intents(db, intent_type):
    return db.query(AgentIntent).filter(AgentIntent.intent_type == intent_type).order_by(AgentIntent.created_at, AgentIntent.seq).all()


def test_live_failure_reproduced_failed_proposal_writes_no_success_memory(db, SessionLocal, grants_with_no_cooldown, monkeypatch):
    """The exact staging failure: the portfolio refuses CREATE_IMPROVEMENT and
    the same decision's WRITE_MEMORY -- listed FIRST, so ordering matters --
    would claim the proposal was raised."""
    settings = _settings(monkeypatch, SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES="0")  # portfolio full
    seed_society(db)
    grants_with_no_cooldown()
    _run_scout(db, SessionLocal, settings, [{"decision_summary": "raise it", "intents": [CLAIM, PROPOSAL]}])

    proposal, = _intents(db, "CREATE_IMPROVEMENT")
    memory, = _intents(db, "WRITE_MEMORY")
    assert _ev(proposal.execution_status) == "failed" and "portfolio full" in proposal.error
    assert _ev(memory.execution_status) == "failed"
    assert "memory not grounded" in memory.error and "CREATE_IMPROVEMENT is failed" in memory.error
    assert memory.executed_at >= proposal.executed_at, "the memory was decided after its side effect"
    assert db.query(MemoryItem).filter(MemoryItem.title == CLAIM["payload"]["title"]).count() == 0
    assert db.query(ImprovementProposal).count() == 0


def test_the_same_memory_is_admitted_when_the_proposal_executed(db, SessionLocal, grants_with_no_cooldown, monkeypatch):
    settings = _settings(monkeypatch, SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES="3")
    seed_society(db)
    grants_with_no_cooldown()
    _run_scout(db, SessionLocal, settings, [{"decision_summary": "raise it", "intents": [CLAIM, PROPOSAL]}])
    assert [_ev(r.execution_status) for r in _intents(db, "CREATE_IMPROVEMENT")] == ["executed"]
    assert [_ev(r.execution_status) for r in _intents(db, "WRITE_MEMORY")] == ["executed"]
    assert db.query(MemoryItem).filter(MemoryItem.title == CLAIM["payload"]["title"]).count() == 1
    assert db.query(ImprovementProposal).count() == 1


def test_a_denied_side_effect_blocks_its_memory(db, SessionLocal, grants_with_no_cooldown, monkeypatch):
    settings = _settings(monkeypatch)
    seed_society(db)
    grants_with_no_cooldown()
    forbidden = {"type": "SHELL_EXEC", "payload": {"command": "true"}}
    _run_scout(db, SessionLocal, settings, [{"decision_summary": "x", "intents": [forbidden, CLAIM]}])
    assert _ev(_intents(db, "SHELL_EXEC")[0].execution_status) == "denied"
    memory, = _intents(db, "WRITE_MEMORY")
    assert _ev(memory.execution_status) == "failed" and "SHELL_EXEC is denied" in memory.error
    assert db.query(MemoryItem).filter(MemoryItem.title == CLAIM["payload"]["title"]).count() == 0


def test_a_side_effect_awaiting_approval_blocks_its_memory_and_rejection_never_revives_it(db, SessionLocal, grants_with_no_cooldown, monkeypatch, make_user):
    settings = _settings(monkeypatch, SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES="3")
    report = seed_society(db)
    grants_with_no_cooldown()
    g = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents["scout"]).first()
    g.approval_required_intents = ["CREATE_IMPROVEMENT"]
    db.commit()
    _run_scout(db, SessionLocal, settings, [{"decision_summary": "gated", "intents": [PROPOSAL, CLAIM]}])
    proposal, = _intents(db, "CREATE_IMPROVEMENT")
    memory, = _intents(db, "WRITE_MEMORY")
    assert _ev(proposal.execution_status) == "awaiting_approval"
    assert _ev(memory.execution_status) == "failed" and "CREATE_IMPROVEMENT is awaiting_approval" in memory.error
    ap.decide(db, intent_id=proposal.id, user=make_user("op@test"), decision="rejected", reason="not now")
    db.expire_all()
    assert _ev(db.get(AgentIntent, proposal.id).execution_status) == "rejected"
    assert db.query(MemoryItem).filter(MemoryItem.title == CLAIM["payload"]["title"]).count() == 0
    assert db.query(ImprovementProposal).count() == 0


def test_observation_memories_remain_supported(db, SessionLocal, grants_with_no_cooldown, monkeypatch):
    settings = _settings(monkeypatch)
    seed_society(db)
    grants_with_no_cooldown()
    _run_scout(db, SessionLocal, settings, [{"decision_summary": "triage only", "intents": [OBSERVATION]}])
    assert [_ev(r.execution_status) for r in _intents(db, "WRITE_MEMORY")] == ["executed"]
    assert db.query(MemoryItem).filter(MemoryItem.title == OBSERVATION["payload"]["title"]).count() == 1


def test_a_failed_side_effect_cannot_create_false_duplicate_suppression_later(db, SessionLocal, grants_with_no_cooldown, monkeypatch):
    """Run 1: the proposal is refused and its success memory is refused with
    it. Run 2 (the signal again, the portfolio free): the Scout's context
    holds NO memory claiming a proposal exists -- only the trusted refusals --
    and its proposal executes. Before this invariant the context carried
    "new proposal raised" and the live Scout declined run 2 as a duplicate."""
    seed_society(db)
    grants_with_no_cooldown()
    full = _settings(monkeypatch, SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES="0")
    _run_scout(db, SessionLocal, full, [{"decision_summary": "raise it", "intents": [PROPOSAL, CLAIM]}])
    assert db.query(ImprovementProposal).count() == 0

    free = _settings(monkeypatch, SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES="3")
    model = _run_scout(db, SessionLocal, free, [{"decision_summary": "raise it again", "intents": [PROPOSAL, CLAIM]}])
    ctx = next(c for c in model.calls if c.agent["name"] == "Society_Scout")
    titles = [m["data"]["title"] for m in ctx.memory]
    assert CLAIM["payload"]["title"] not in titles, "no memory from the failed decision claims the proposal"
    refused = {r["intent_type"]: r for r in ctx.recent_refusals}
    # trusted execution evidence, with the EXECUTION reason (not "allowed by grant")
    assert refused["CREATE_IMPROVEMENT"]["outcome"] == "failed" and "portfolio full" in refused["CREATE_IMPROVEMENT"]["reason"]
    assert refused["WRITE_MEMORY"]["outcome"] == "failed" and "memory not grounded" in refused["WRITE_MEMORY"]["reason"]
    assert db.query(ImprovementProposal).count() == 1, "the second proposal was not suppressed"
    assert [_ev(r.execution_status) for r in _intents(db, "WRITE_MEMORY")] == ["failed", "executed"]


def test_every_society_path_that_writes_model_memory_is_grounded():
    """Structural guard: inside the Society package, MemoryItem rows are created
    only by the WRITE_MEMORY executor (model text, grounded here) and the
    fitness engine (trusted outcome, validated). A new model-memory path must
    come through memory_grounding.check."""
    import pathlib
    import re

    pkg = pathlib.Path(mg.__file__).resolve().parent
    writers = sorted(p.relative_to(pkg).as_posix() for p in pkg.rglob("*.py") if re.search(r"\bMemoryItem\(", p.read_text(encoding="utf-8")))
    assert writers == ["executor.py", "fitness.py"], writers
    src = (pkg / "executor.py").read_text(encoding="utf-8")
    body = src[src.index("def _write_memory("):]
    body = body[: body.index("\ndef ", 1)]
    assert body.index("memory_grounding.check(") < body.index("MemoryItem("), "the grounding check runs before any row is built"
    fit = (pkg / "fitness.py").read_text(encoding="utf-8")
    assert 'validation_state="validated"' in fit and "author_agent_id=None" in fit, "the fitness memory is trusted, not model-authored"
