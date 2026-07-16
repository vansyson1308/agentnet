"""PostgreSQL-backed managed execution concurrency and persistence tests.

Run explicitly with ``RUN_MANAGED_POSTGRES_TESTS=1`` and a migrated test DB.
The default suite skips this module so it never targets a developer database
by accident.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MANAGED_POSTGRES_TESTS") != "1",
    reason="requires an explicitly provisioned PostgreSQL test database",
)

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "services" / "registry"))

from app.database import SessionLocal  # noqa: E402
from app.managed_execution_service import (  # noqa: E402
    IdempotencyMismatch,
    create_managed_shadow,
)
from app.managed_models import (  # noqa: E402
    Attempt,
    ExecutionRun,
    IntegrationOutbox,
    Lease,
    LeaseStatus,
    ManagedExecution,
    RunStatus,
    Runtime,
    RuntimeSlot,
    RuntimeStatus,
)
from app.managed_schemas import ManagedExecutionCreate  # noqa: E402
from app.models import TaskSession, Transaction  # noqa: E402
from app.run_service import claim_assignment  # noqa: E402
from app.runtime_allocator import NoRuntimeAvailable  # noqa: E402


def _runtime(db, suffix: str) -> Runtime:
    runtime = Runtime(
        id=uuid.uuid4(),
        registration_key=f"postgres-test-{suffix}",
        name="Postgres integration runtime",
        role="builder",
        adapter="codex",
        capabilities=["repository.edit"],
        repository_scopes=["https://github.com/example/*"],
        permissions={"tools": ["git", "codex"], "network": False},
        model="codex",
        provider="openai",
        capacity=1,
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        status=RuntimeStatus.ONLINE,
    )
    slot = RuntimeSlot(id=uuid.uuid4(), runtime_id=runtime.id, slot_number=1, enabled=True)
    db.add_all([runtime, slot])
    db.commit()
    return runtime


def _payload(suffix: str, **changes) -> ManagedExecutionCreate:
    values = {
        "goal_id": f"goal-{suffix}",
        "work_item_id": f"work-{suffix}",
        "work_item_revision": "1",
        "role": "builder",
        "capability": "repository.edit",
        "repository": "https://github.com/example/repo.git",
        "base_commit_sha": "a" * 40,
        "prompt": "Implement the accepted change",
        "acceptance": {"commands": ["pytest -q"]},
        "requirements": {
            "adapter": "codex",
            "model": "codex",
            "provider": "openai",
            "permissions": {"tools": ["git"]},
        },
        "budgets": {"time_seconds": 300},
        "approval_policy_version": "shadow-v1",
        "idempotency_key": f"postgres-idempotency-{suffix}",
        "trace_id": uuid.uuid4(),
    }
    values.update(changes)
    return ManagedExecutionCreate(**values)


def test_atomic_creation_100_replays_and_concurrent_claim_survive_restart():
    suffix = uuid.uuid4().hex
    db = SessionLocal()
    runtime = _runtime(db, suffix)
    runtime_id = runtime.id
    payload = _payload(suffix, required_runtime_id=runtime_id)

    first = create_managed_shadow(db, payload)
    replays = [create_managed_shadow(db, payload) for _ in range(99)]
    assert all(item.id == first.id and item.idempotent_replay for item in replays)

    assert db.query(ManagedExecution).filter(ManagedExecution.work_item_id == payload.work_item_id).count() == 1
    assert db.query(TaskSession).filter(TaskSession.id == first.task_session_id).count() == 1
    assert db.query(ExecutionRun).filter(ExecutionRun.managed_execution_id == first.id).count() == 1
    assert db.query(Attempt).filter(Attempt.run_id == first.initial_run_id).count() == 1
    assert db.query(Lease).filter(Lease.run_id == first.initial_run_id).count() == 1
    assert db.query(IntegrationOutbox).filter(IntegrationOutbox.aggregate_id == first.id).count() == 1
    assert db.query(Transaction).filter(Transaction.task_session_id == first.task_session_id).count() == 0

    with pytest.raises(IdempotencyMismatch):
        create_managed_shadow(db, _payload(suffix, prompt="different payload"))

    task_count = db.query(TaskSession).count()
    with pytest.raises(NoRuntimeAvailable):
        create_managed_shadow(
            db,
            _payload(
                uuid.uuid4().hex,
                capability="repository.admin",
                idempotency_key=f"no-runtime-{uuid.uuid4().hex}",
            ),
        )
    assert db.query(TaskSession).count() == task_count
    db.close()

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def claim():
        session = SessionLocal()
        try:
            claimed_runtime = session.query(Runtime).filter(Runtime.id == runtime_id).one()
            barrier.wait(timeout=10)
            results.append(claim_assignment(session, claimed_runtime))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=claim), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert claimed[0].run_id == first.initial_run_id

    restarted = SessionLocal()
    try:
        run = restarted.query(ExecutionRun).filter(ExecutionRun.id == first.initial_run_id).one()
        lease = restarted.query(Lease).filter(Lease.run_id == run.id).one()
        assert run.status == RunStatus.ACKNOWLEDGED
        assert lease.state == LeaseStatus.ACKNOWLEDGED
        assert lease.token_hash is not None
    finally:
        restarted.close()
