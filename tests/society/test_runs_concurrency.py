"""Claim/lease/retry semantics under real concurrency (threads + separate
sessions against Postgres). Exactly one worker may win a run."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

import pytest

from services.registry.app.models import AgentCapabilityGrant, AgentRun, SocietyEvent
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.policy import check_run_budget
from services.registry.app.society.runs import claim_next_run, complete_run, dispatch_pending_events, extend_lease, fail_run, skip_run
from services.registry.app.society.seed import seed_society


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _queue_runs(db, settings, n_events: int, roles=("scout",)):
    for i in range(n_events):
        emit_event(db, event_type="t.claim", payload={"i": i})
    db.commit()
    dispatch_pending_events(db, settings=settings, routing={"t.claim": list(roles)})
    return db.query(AgentRun).count()


def test_two_workers_racing_for_one_run_exactly_one_wins(db, SessionLocal, society_settings):
    seed_society(db)
    assert _queue_runs(db, society_settings, 1) == 1
    winners = []
    barrier = threading.Barrier(2)

    def worker(wid):
        s = SessionLocal()
        try:
            barrier.wait()
            run = claim_next_run(s, worker_id=wid, lease_seconds=60)
            winners.append((wid, run.id if run else None))
        finally:
            s.close()

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    claimed = [w for w in winners if w[1] is not None]
    assert len(claimed) == 1, winners
    run = db.query(AgentRun).first()
    db.refresh(run)
    assert _ev(run.status) == "claimed" and run.worker_id == claimed[0][0] and run.attempt == 1


def test_many_workers_many_runs_no_duplicates(db, SessionLocal, society_settings):
    seed_society(db)
    total = _queue_runs(db, society_settings, 12, roles=("scout", "governor"))
    assert total == 24
    seen = []
    lock = threading.Lock()

    def worker(wid):
        s = SessionLocal()
        try:
            while True:
                run = claim_next_run(s, worker_id=wid, lease_seconds=60)
                if run is None:
                    break
                with lock:
                    seen.append(run.id)
        finally:
            s.close()

    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert len(seen) == total
    assert len(set(seen)) == total, "a run was claimed twice"


def test_expired_lease_is_reclaimable_and_attempt_increments(db, SessionLocal, society_settings):
    seed_society(db)
    _queue_runs(db, society_settings, 1)
    s1 = SessionLocal()
    run = claim_next_run(s1, worker_id="crashed", lease_seconds=1)
    assert run is not None and run.attempt == 1
    # nobody else can claim while the lease is valid
    s2 = SessionLocal()
    assert claim_next_run(s2, worker_id="other", lease_seconds=60) is None
    # simulate the crashed worker: expire the lease
    run.lease_expires_at = utcnow() - timedelta(seconds=1)
    s1.commit()
    reclaimed = claim_next_run(s2, worker_id="other", lease_seconds=60)
    assert reclaimed is not None and reclaimed.id == run.id
    assert reclaimed.attempt == 2 and reclaimed.worker_id == "other"
    s1.close()
    s2.close()


def test_extend_lease_keeps_run_unclaimable(db, SessionLocal, society_settings):
    seed_society(db)
    _queue_runs(db, society_settings, 1)
    run = claim_next_run(db, worker_id="w", lease_seconds=1)
    extend_lease(db, run, lease_seconds=120)
    s2 = SessionLocal()
    try:
        assert claim_next_run(s2, worker_id="w2", lease_seconds=60) is None
    finally:
        s2.close()


def test_fail_run_requeues_with_backoff_then_dies_and_trips_breaker(db, society_settings, monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    monkeypatch.setenv("SOCIETY_RETRY_BACKOFF_BASE_SECONDS", "10")
    monkeypatch.setenv("SOCIETY_RUN_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("SOCIETY_CIRCUIT_BREAKER_FAILURES", "1")
    reset_settings_cache()
    settings = SocietySettings()
    report = seed_society(db)
    _queue_runs(db, settings, 1)
    run = claim_next_run(db, worker_id="w", lease_seconds=60)
    assert run.max_attempts == 2
    status = fail_run(db, run, "boom", settings=settings)
    assert status.value == "queued"
    db.refresh(run)
    assert run.not_before is not None and run.not_before > utcnow() + timedelta(seconds=5)
    assert run.worker_id is None and run.lease_expires_at is None
    # not claimable before not_before
    assert claim_next_run(db, worker_id="w", lease_seconds=60) is None
    run.not_before = utcnow() - timedelta(seconds=1)
    db.commit()
    run2 = claim_next_run(db, worker_id="w", lease_seconds=60)
    assert run2.id == run.id and run2.attempt == 2
    status = fail_run(db, run2, "boom again", settings=settings)
    assert status.value == "dead"
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents["scout"]).first()
    assert grant.consecutive_failures == 1 and grant.paused_until is not None and grant.paused_until > utcnow()
    types = {e.event_type for e in db.query(SocietyEvent).all()}
    assert EventType.RUN_DEAD in types and EventType.LOOP_BREAKER_TRIPPED in types
    # circuit breaker blocks the next run of that agent
    agent = run2.agent
    verdict = check_run_budget(db, agent=agent, grant=grant, settings=settings, run=run2)
    assert not verdict.ok and "circuit breaker" in verdict.reason
    # the event is finalised as processed once its only run is terminal
    ev = db.query(SocietyEvent).filter(SocietyEvent.id == run2.event_id).first()
    assert _ev(ev.status) == "processed"


def test_complete_run_resets_failure_counter_and_finalizes_event(db, society_settings):
    report = seed_society(db)
    _queue_runs(db, society_settings, 1)
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents["scout"]).first()
    grant.consecutive_failures = 2
    db.commit()
    run = claim_next_run(db, worker_id="w", lease_seconds=60)
    complete_run(db, run)
    db.refresh(grant)
    assert grant.consecutive_failures == 0
    ev = db.query(SocietyEvent).filter(SocietyEvent.id == run.event_id).first()
    assert _ev(ev.status) == "processed" and ev.processed_at is not None


def test_skip_run_is_terminal_and_not_a_failure(db, society_settings):
    report = seed_society(db)
    _queue_runs(db, society_settings, 1)
    run = claim_next_run(db, worker_id="w", lease_seconds=60)
    skip_run(db, run, "cooldown")
    db.refresh(run)
    assert _ev(run.status) == "skipped" and run.error == "cooldown"
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.agent_id == report.agents["scout"]).first()
    assert grant.consecutive_failures == 0
    assert claim_next_run(db, worker_id="w", lease_seconds=60) is None


@pytest.mark.parametrize("n", [1, 5])
def test_claim_order_is_fifo(db, society_settings, n):
    seed_society(db)
    _queue_runs(db, society_settings, n)
    created_order = [r.id for r in db.query(AgentRun).order_by(AgentRun.created_at).all()]
    claimed = []
    while True:
        r = claim_next_run(db, worker_id="w", lease_seconds=60)
        if r is None:
            break
        claimed.append(r.id)
    assert claimed == created_order
    assert uuid.UUID(str(claimed[0]))
