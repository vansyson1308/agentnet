"""Crash safety at every transition boundary, concurrency, and a 24-hour
accelerated reconciliation replay (ADR-0010 D6; mission items 16, 86, 91, 199)."""

from __future__ import annotations

import threading

import pytest

from services.registry.app.maintenance import ledger as ledger_mod
from services.registry.app.maintenance import reconciler as rec_mod
from services.registry.app.maintenance import state_machine as sm
from services.registry.app.maintenance.orm import MaintenanceIncident, MaintenanceRelease, RepairActivity, RepairCase, RepairTransition
from services.registry.app.maintenance.reconciler import stranded_count
from services.registry.app.models import CodeCandidate, CodePromotion
from services.registry.app.society import promotion as pm

from .conftest import at, drive_promotion, green_repair_script, raise_incident, violation
from .test_kernel_e2e import fake_release_env, release_controller

pytestmark = pytest.mark.timeout(900)


class Crash(BaseException):
    """Process death: not an Exception, so no handler in the kernel catches it."""


def _case(db):
    db.expire_all()
    return db.query(RepairCase).one()


def legal_trail(db):
    rows = db.query(RepairTransition).order_by(RepairTransition.id).all()
    for r in rows[1:]:
        src, dst = sm.CaseState(r.from_state), sm.CaseState(r.to_state)
        assert dst in sm.spec(src).allowed and r.actor_type in {a.value for a in sm.spec(src).allowed[dst]}, (r.from_state, r.to_state, r.actor_type)
    return [r.to_state for r in rows]


@pytest.mark.parametrize("crash_at", ["CONFIRMED", "TRIAGED", "DIAGNOSING", "PLAN_READY", "BUILDING", "VERIFYING", "PROMOTING"])
def test_process_death_before_each_transition_commits_converges_without_duplicates(db, SessionLocal, mset, sset, kernel_factory, monkeypatch, crash_at):
    from services.registry.app.society.seed import seed_society

    seed_society(db)
    raise_incident(db, mset)
    real = ledger_mod.transition
    fired = {"n": 0}

    def dying(dbs, case, dst, **kw):
        if dst.value == crash_at and not fired["n"]:
            fired["n"] += 1
            raise Crash(f"killed before {crash_at} committed")
        return real(dbs, case, dst, **kw)

    monkeypatch.setattr(rec_mod, "transition", dying)
    k1 = kernel_factory(green_repair_script(), worker_id="k1")
    with pytest.raises(Crash):
        for i in range(10):
            k1.reconcile(now=at(2 + i * 0.01))
    assert fired["n"] == 1
    monkeypatch.setattr(rec_mod, "transition", real)
    # restart: a NEW process after the dead one's lease expired
    k2 = kernel_factory(green_repair_script(), worker_id="k2")
    for i in range(12):
        k2.reconcile(now=at(20 + i))
    provider = pm.FakePromotionProvider()
    for m in (40, 45, 50):
        drive_promotion(SessionLocal, sset, provider)
        k2.reconcile(now=at(m))
    case = _case(db)
    assert case.state in ("READY_FOR_RELEASE", "RELEASING"), (crash_at, case.state, case.terminal_reason)
    assert db.query(CodeCandidate).count() == 1 and db.query(CodePromotion).count() == 1 and provider.merge_count == 1
    assert db.query(MaintenanceRelease).count() <= 1
    trail = legal_trail(db)
    assert trail.count("PROMOTING") == 1, trail
    assert stranded_count(db, now=at(60)) == 0


def test_death_mid_activity_marks_the_try_abandoned_and_retries(db, mset, kernel_factory, monkeypatch):
    raise_incident(db, mset)
    state = {"die": True}
    green = green_repair_script()

    def script(messages):
        if "DiagnoseIncident" in messages[0]["content"] and state["die"]:
            state["die"] = False
            raise Crash("killed during the model call")
        return green(messages)

    k1 = kernel_factory(script, worker_id="k1")
    with pytest.raises(Crash):
        k1.reconcile(now=at(2))
    running = db.query(RepairActivity).one()
    assert running.status == "running", "the try was durable before the model was called"
    k2 = kernel_factory(green, worker_id="k2")
    for i in range(8):
        k2.reconcile(now=at(20 + i))
    db.expire_all()
    diag = db.query(RepairActivity).filter(RepairActivity.kind == "DiagnoseIncident").order_by(RepairActivity.started_at).all()
    assert [d.status for d in diag] == ["abandoned", "succeeded"]
    assert _case(db).state == "PROMOTING"


def test_two_kernels_racing_produce_exactly_one_authoritative_history(db, SessionLocal, mset, kernel_factory):
    raise_incident(db, mset)
    kernels = [kernel_factory(green_repair_script(), worker_id=f"race-{i}") for i in range(2)]
    errors = []

    def spin(k):
        try:
            for i in range(10):
                k.reconcile(now=at(2 + i * 0.01))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=spin, args=(k,)) for k in kernels]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    trail = legal_trail(db)
    assert trail == ["DETECTED", "CONFIRMED", "TRIAGED", "DIAGNOSING", "PLAN_READY", "BUILDING", "VERIFYING", "PROMOTING"], trail
    assert db.query(CodeCandidate).count() == 1 and db.query(RepairCase).count() == 1
    succeeded = db.query(RepairActivity).filter(RepairActivity.status == "succeeded").count()
    assert succeeded == 5, "one successful try per activity kind"


def test_duplicate_anomalies_from_concurrent_collectors_make_one_incident(SessionLocal, db, mset):
    from services.registry.app.maintenance import incidents as inc_mod

    errors = []

    def collect(i):
        s = SessionLocal()
        try:
            inc_mod.ingest_violation(s, mset, violation(), now=at(i * 0.01))
            s.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=collect, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    inc = db.query(MaintenanceIncident).one()
    assert inc.observation_count == 8


def test_two_release_controllers_merge_and_deploy_once(db, SessionLocal, mset, sset, kernel_factory):
    from .test_kernel_e2e import reach_releasing

    reach_releasing(db, SessionLocal, mset, sset, kernel_factory)
    rel = db.query(MaintenanceRelease).one()
    gh, rw, probe = fake_release_env(rel.head_sha)
    rcs = [release_controller(SessionLocal, mset, gh, rw, probe) for _ in range(2)]
    rcs[1].worker_id = "rc2"
    errors = []

    def spin(rc):
        try:
            for i in range(12):
                rc.run_once(now=at(40 + i * 2))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=spin, args=(rc,)) for rc in rcs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    db.expire_all()
    assert not errors and db.query(MaintenanceRelease).one().status == "succeeded"
    assert gh.merges == 1 and rw.deploy_calls == 1 and len(gh.prs) == 1


def test_monitor_recovery_during_release_does_not_resolve_early(db, SessionLocal, mset, sset, kernel_factory):
    from services.registry.app.maintenance import incidents as inc_mod

    from .test_kernel_e2e import reach_releasing

    reach_releasing(db, SessionLocal, mset, sset, kernel_factory)
    for i in range(5):
        inc_mod.observe_healthy(db, mset, target="production", desired_state_ref="marketplace", sli="public_pages", source="m", collector_version="v", now=at(40 + i))
    db.commit()
    inc = db.query(MaintenanceIncident).one()
    assert inc.status == "open" and inc.healthy_streak == 0, "a release still settling: observations prove nothing yet"


def test_24_hours_of_accelerated_reconciliation_never_strands_a_case(db, SessionLocal, mset, sset, kernel_factory):
    """Seeded, deterministic day: incidents appear every few hours, some
    recover on their own, some are repaired, models fail now and then; the
    kernel runs every 5 simulated minutes. At EVERY tick: no stranded case.
    At the end: every case terminal or waiting on the owner."""
    import random

    from services.registry.app.maintenance import incidents as inc_mod

    rnd = random.Random(20260927)
    green = green_repair_script()

    def flaky(messages):
        if rnd.random() < 0.15:
            return "}{ not json"
        return green(messages)

    k = kernel_factory(flaky, worker_id="day")
    provider = pm.FakePromotionProvider()
    refs = ["marketplace", "login", "register", "network", "landing"]
    for tick in range(0, 24 * 60, 5):
        now = at(tick)
        if tick % 180 == 0:
            ref = refs[(tick // 180) % len(refs)]
            for j in range(2):
                inc_mod.ingest_violation(db, mset, violation(desired_state_ref=ref), now=at(tick + j * 0.5))
            db.commit()
            db.query(MaintenanceIncident).filter(MaintenanceIncident.desired_state_ref == ref, MaintenanceIncident.status == "open").update({"provenance": {"contract": "public_surface"}})
            db.commit()
        if tick % 240 == 120:
            inc_mod.observe_healthy(db, mset, target="production", desired_state_ref="network", sli="public_pages", source="m", collector_version="v", now=now)
            db.commit()
        k.reconcile(now=now)
        if tick % 15 == 0:
            drive_promotion(SessionLocal, sset, provider, rounds=1)
        assert stranded_count(db, now=now, stall_seconds=mset.stall_seconds) == 0, tick
    db.expire_all()
    for c in db.query(RepairCase).all():
        assert c.state in {s.value for s in sm.TERMINAL} or c.state in ("PROMOTING", "READY_FOR_RELEASE", "RELEASING"), (c.state, c.terminal_reason)
        assert c.state in {s.value for s in sm.TERMINAL} or (c.next_action_at is not None and c.deadline_at is not None)
