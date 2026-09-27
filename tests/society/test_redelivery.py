"""A candidate wake the loop breaker swallowed is re-delivered once, in a fresh
story, while the candidate still waits for it (society/redelivery.py).

Staging: 9da14a08 (REQUESTED) and f8296297 were stranded when their story hit
the per-correlation run limit, and only an operator abandon closed them;
23ac830a's ``code_candidate.built`` was swallowed in correlation 4017ce48. The
Builder's heartbeat covers only the Builder's stages -- nothing re-woke
Security for ``security_review`` or the Governor for ``ready``.
"""

from __future__ import annotations

import asyncio
import uuid

from services.registry.app.models import AgentRun, CodeCandidate, CodePromotion, SocietyEvent
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.config import SocietySettings, reset_settings_cache
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.redelivery import MAX_PER_SWEEP, REDELIVERED_FROM, redeliver_swallowed_wakes
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

OBSERVE = {"decision_summary": "observe", "intents": [], "sleep_for_seconds": 0}


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    reset_settings_cache()
    return SocietySettings()


def _candidate(db, report, status):
    cand = CodeCandidate(
        id=uuid.uuid4(), correlation_id=uuid.uuid4(), requested_by_agent_id=report.agents["architect"],
        builder_agent_id=report.agents["builder"], title=f"candidate in {status}",
        spec={"kind": "code", "files_allowed": ["services/dashboard/app/main.py"], "acceptance_tests": ["services/dashboard/tests/test_public_surface.py"]},
        status=status, risk_tier="amber",
    )
    db.add(cand)
    db.commit()
    return cand


def _swallowed(db, report, cand, event_type, *, payload=None, role="qa"):
    """A lifecycle wake exactly as the loop breaker leaves it."""
    ev = emit_event(
        db, event_type=event_type, payload=payload or {"candidate_id": str(cand.id), "title": cand.title},
        actor_type="agent", actor_id=report.agents[role], subject_type="code_candidate", subject_id=cand.id,
        correlation_id=uuid.uuid4(), idempotency_key=f"t-{uuid.uuid4()}",
    )
    ev.status = "ignored"
    ev.dispatch_note = f"loop breaker: correlation {ev.correlation_id} reached 12 runs"
    db.commit()
    return ev


def _redeliveries(db, original):
    return [e for e in db.query(SocietyEvent).filter(SocietyEvent.event_type == original.event_type).all()
            if (e.payload or {}).get(REDELIVERED_FROM) == str(original.id)]


def test_a_security_wake_swallowed_by_the_loop_breaker_reaches_security_in_a_fresh_story(db, SessionLocal, society_settings, monkeypatch, grants_with_no_cooldown):
    """The live failure mode, through the real dispatcher: the story's run budget
    is spent, the QA -> Security wake is ignored, and without re-delivery the
    candidate would sit in security_review forever."""
    settings = _settings(monkeypatch, SOCIETY_MAX_RUNS_PER_CORRELATION=1)
    report = seed_society(db)
    grants_with_no_cooldown()
    cand = _candidate(db, report, "security_review")
    story = uuid.uuid4()
    emit_event(db, event_type="t.start", correlation_id=story, idempotency_key=f"t-start-{story}")
    db.commit()
    model = FakeModel(lambda ctx: OBSERVE)
    worker = SocietyWorker(SessionLocal, settings=settings, model=model, worker_id="w-redeliver", telemetry_enabled=False)
    worker.routing = {**worker.routing, "t.start": ["scout"]}
    asyncio.run(worker.run_until_idle(max_cycles=5))  # the story spends its one run

    wake = emit_event(
        db, event_type=EventType.CODE_CANDIDATE_SECURITY_REVIEW, payload={"candidate_id": str(cand.id), "title": cand.title},
        actor_type="agent", actor_id=report.agents["qa"], subject_type="code_candidate", subject_id=cand.id,
        correlation_id=story, idempotency_key=f"t-wake-{cand.id}",
    )
    db.commit()
    asyncio.run(worker.run_until_idle(max_cycles=8))
    db.expire_all()

    wake = db.get(SocietyEvent, wake.id)
    assert _ev(wake.status) == "ignored" and wake.dispatch_note.startswith("loop breaker"), "the breaker did swallow it"
    again = _redeliveries(db, wake)
    assert len(again) == 1, "re-delivered exactly once"
    fresh = again[0]
    assert fresh.correlation_id != story and int(fresh.causation_depth or 0) == 0 and fresh.actor_type == "system"
    assert fresh.subject_id == cand.id and fresh.payload["candidate_id"] == str(cand.id)
    woken = db.query(AgentRun).filter(AgentRun.event_id == fresh.id).all()
    assert [r.role for r in woken] == ["security"], "the stage owner is woken, in its own fresh story"

    asyncio.run(worker.run_until_idle(max_cycles=3))
    db.expire_all()
    assert len(_redeliveries(db, wake)) == 1, "idempotent: never re-sent twice"


def test_only_an_owed_lifecycle_wake_is_redelivered_and_never_twice(db, society_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()

    # owed: the candidate still waits in the stage the wake was for
    owed = {
        EventType.CODE_CHANGE_REQUESTED: _candidate(db, report, "requested"),
        EventType.CODE_CANDIDATE_BUILT: _candidate(db, report, "built"),
        EventType.CODE_CANDIDATE_QA_FAILED: _candidate(db, report, "qa_failed"),
        EventType.CODE_CANDIDATE_READY: _candidate(db, report, "ready"),
    }
    owed_wakes = [_swallowed(db, report, c, et) for et, c in owed.items()]

    # not owed
    moved_on = _swallowed(db, report, _candidate(db, report, "ready"), EventType.CODE_CANDIDATE_SECURITY_REVIEW)
    promoted = _candidate(db, report, "ready")
    db.add(CodePromotion(candidate_id=promoted.id, correlation_id=uuid.uuid4(), risk_tier="amber", provider="fake", status="requested"))
    db.commit()
    already_promoted = _swallowed(db, report, promoted, EventType.CODE_CANDIDATE_READY)
    not_lifecycle = _swallowed(db, report, _candidate(db, report, "requested"), EventType.REPO_READ_RESULT)
    second_swallow = _swallowed(db, report, _candidate(db, report, "built"), EventType.CODE_CANDIDATE_BUILT,
                                payload={"candidate_id": "x", REDELIVERED_FROM: str(uuid.uuid4())})

    assert redeliver_swallowed_wakes(db, society_settings) == len(owed_wakes)
    for w in owed_wakes:
        assert len(_redeliveries(db, w)) == 1, w.event_type
    for w in (moved_on, already_promoted, not_lifecycle, second_swallow):
        assert _redeliveries(db, w) == [], w.event_type
    assert redeliver_swallowed_wakes(db, society_settings) == 0, "idempotent across sweeps"


def test_a_sweep_is_bounded(db, society_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    wakes = [_swallowed(db, report, _candidate(db, report, "built"), EventType.CODE_CANDIDATE_BUILT) for _ in range(MAX_PER_SWEEP + 2)]
    assert redeliver_swallowed_wakes(db, society_settings) == MAX_PER_SWEEP
    assert redeliver_swallowed_wakes(db, society_settings) == 2
    assert all(len(_redeliveries(db, w)) == 1 for w in wakes)


def test_wakes_already_handled_never_crowd_out_one_still_owed(db, society_settings, grants_with_no_cooldown):
    """Re-delivered and moved-on wakes stay IGNORED for the whole lookback; they
    must not fill the sweep so that a newer owed wake is never reached."""
    report = seed_society(db)
    grants_with_no_cooldown()
    handled = [_swallowed(db, report, _candidate(db, report, "built"), EventType.CODE_CANDIDATE_BUILT) for _ in range(MAX_PER_SWEEP * 4)]
    moved_on = [_swallowed(db, report, _candidate(db, report, "qa_running"), EventType.CODE_CANDIDATE_BUILT) for _ in range(MAX_PER_SWEEP * 4)]
    while redeliver_swallowed_wakes(db, society_settings):
        pass
    assert all(len(_redeliveries(db, w)) == 1 for w in handled)

    owed = _swallowed(db, report, _candidate(db, report, "security_review"), EventType.CODE_CANDIDATE_SECURITY_REVIEW)
    assert redeliver_swallowed_wakes(db, society_settings) == 1
    assert len(_redeliveries(db, owed)) == 1
    assert all(_redeliveries(db, w) == [] for w in moved_on)
