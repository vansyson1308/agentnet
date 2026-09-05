"""Auto-refund worker lifecycle (Phase 2.5 §15).

No database or Redis: every collaborator is stubbed so the tests prove the
LOOP contract itself —

* a stop request (SIGTERM/SIGINT or an explicit event) ends the loop after
  the current pass and every per-pass session is closed;
* a database outage in one pass is logged, the pass's session is closed,
  the loop waits the normal poll interval (no busy loop) and continues;
* Redis being down at startup does not block refunds and is retried each
  pass until it returns;
* the poll interval can never be configured below one second.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import signal

import pytest
from sqlalchemy.exc import OperationalError


class _FakeSession:
    def __init__(self):
        self.closed = False
        self.rolled_back = False

    def close(self):
        self.closed = True

    def rollback(self):
        self.rolled_back = True


class _FakeRedis:
    def __init__(self):
        self.closed = False

    async def ping(self):
        return True

    async def publish(self, *a, **k):
        return 1

    async def aclose(self):
        self.closed = True


@pytest.fixture
def worker_mod(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("JAEGER_ENABLED", "false")
    return importlib.import_module("services.worker.app.worker")


async def _noop(*a, **k):
    return None


def _quiet(mod, monkeypatch, sessions, *, db_fail=False, redis_factory=None):
    """Stub out every side effect of one loop pass."""
    monkeypatch.setattr(mod, "start_http_server", lambda port: None)

    async def _init_redis():
        if redis_factory is None:
            raise ConnectionError("redis down")
        return redis_factory()

    monkeypatch.setattr(mod, "init_redis", _init_redis)

    def _session():
        s = _FakeSession()
        sessions.append(s)
        return s

    monkeypatch.setattr(mod, "SessionLocal", _session)

    async def _process(db, redis_client):
        if db_fail:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(mod, "process_timed_out_tasks", _process)
    monkeypatch.setattr(mod, "process_offline_agents", _noop)
    monkeypatch.setattr(mod, "run_reflection_loop", lambda db: 0)
    monkeypatch.setattr(mod, "convert_proposals_to_backlog", lambda db: 0)
    monkeypatch.setattr(mod, "WORKER_POLL_INTERVAL_SEC", 0.02)


def _stop_after(mod, monkeypatch, passes: int):
    """Replace the inter-pass wait with one that records the requested
    delay and stops the loop after ``passes`` passes."""
    waits = []

    async def _wait(stop_event, seconds):
        waits.append(seconds)
        if len(waits) >= passes:
            stop_event.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(mod, "wait_or_stop", _wait)
    return waits


def test_stop_event_ends_loop_and_closes_every_session(worker_mod, monkeypatch):
    sessions = []
    _quiet(worker_mod, monkeypatch, sessions)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(worker_mod.main(stop))
        await asyncio.sleep(0.15)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())
    assert len(sessions) >= 2, "loop should have run several passes"
    assert all(s.closed for s in sessions), "every pass closes its own session"


def test_sigterm_requests_graceful_stop(worker_mod, monkeypatch):
    sessions = []
    _quiet(worker_mod, monkeypatch, sessions)

    async def run():
        task = asyncio.create_task(worker_mod.main())
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)  # what `docker stop` sends
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())  # a missing handler would kill the test process here
    assert sessions and all(s.closed for s in sessions)


def test_signal_handlers_cover_sigterm_and_sigint(worker_mod):
    installed = {}

    class _Loop:
        def add_signal_handler(self, sig, cb):
            installed[sig] = cb

    ev = asyncio.Event()
    worker_mod.install_signal_handlers(_Loop(), ev)
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    installed[signal.SIGTERM]()
    assert ev.is_set()


def test_db_outage_is_survived_without_busy_looping(worker_mod, monkeypatch):
    sessions = []
    _quiet(worker_mod, monkeypatch, sessions, db_fail=True)
    waits = _stop_after(worker_mod, monkeypatch, passes=3)
    asyncio.run(worker_mod.main())
    assert len(sessions) == 3, "one fresh session per pass, outage or not"
    assert all(s.closed for s in sessions), "a failing pass still closes its session"
    assert waits == [worker_mod.WORKER_POLL_INTERVAL_SEC] * 3, "every failing pass waits the full poll interval"


def test_redis_down_at_startup_is_tolerated_then_reconnected(worker_mod, monkeypatch):
    sessions = []
    attempts = []
    clients = []

    def _factory():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("redis down")
        c = _FakeRedis()
        clients.append(c)
        return c

    _quiet(worker_mod, monkeypatch, sessions, redis_factory=_factory)
    _stop_after(worker_mod, monkeypatch, passes=5)
    asyncio.run(worker_mod.main())
    assert len(sessions) == 5, "refund passes ran while Redis was down"
    assert len(attempts) == 3, "reconnect retried each pass and stopped once connected"
    assert len(clients) == 1 and clients[0].closed, "the live client is closed on graceful stop"


@pytest.mark.parametrize("raw,expected", [("0", 1), ("-5", 1), ("banana", 30), ("45", 45)])
def test_poll_interval_has_a_floor_of_one_second(worker_mod, monkeypatch, raw, expected):
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SEC", raw)
    assert worker_mod._poll_interval_from_env() == expected


def test_metrics_port_collision_is_fatal(worker_mod, monkeypatch):
    """Two replicas on one host must not both publish metrics; the second
    exits so the orchestrator restarts it elsewhere (documented behaviour)."""

    def _bind(port):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(worker_mod, "start_http_server", _bind)
    with pytest.raises(OSError):
        asyncio.run(worker_mod.main())
