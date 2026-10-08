"""Durable events + dispatch rules (real Postgres)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from services.registry.app.models import AgentRun, SocietyEvent
from services.registry.app.society.events import EventType, emit_event, expire_stale_events, utcnow
from services.registry.app.society.roles import subscriptions_by_event
from services.registry.app.society.runs import dispatch_pending_events
from services.registry.app.society.seed import seed_society


def _ev(v):
    return v.value if hasattr(v, "value") else v


def test_emit_event_is_durable_and_dedupes_on_idempotency_key(db):
    a = emit_event(db, event_type="x.test", payload={"n": 1}, idempotency_key="k1")
    db.commit()
    b = emit_event(db, event_type="x.test", payload={"n": 2}, idempotency_key="k1")
    db.commit()
    assert a.id == b.id
    assert db.query(SocietyEvent).count() == 1
    row = db.query(SocietyEvent).first()
    assert row.payload == {"n": 1}
    assert _ev(row.status) == "pending"
    assert row.correlation_id is not None and row.trace_id == row.correlation_id


def test_causation_chain_increments_depth_and_shares_correlation(db):
    root = emit_event(db, event_type="root")
    child = emit_event(db, event_type="child", causation=root)
    grandchild = emit_event(db, event_type="grandchild", causation=child)
    db.commit()
    assert child.correlation_id == root.correlation_id == grandchild.correlation_id
    assert (root.causation_depth, child.causation_depth, grandchild.causation_depth) == (0, 1, 2)
    assert grandchild.causation_id == child.id


def test_expire_stale_events(db):
    old = emit_event(db, event_type="old")
    db.commit()
    old.created_at = utcnow() - timedelta(hours=48)
    db.commit()
    fresh = emit_event(db, event_type="fresh")
    db.commit()
    n = expire_stale_events(db, ttl_seconds=3600)
    db.commit()
    assert n == 1
    db.refresh(old)
    db.refresh(fresh)
    assert _ev(old.status) == "expired" and _ev(fresh.status) == "pending"


def test_dispatch_routes_by_role_and_never_to_the_actor_itself(db, society_settings):
    report = seed_society(db)
    routing = subscriptions_by_event(__import__("services.registry.app.society.roles", fromlist=["DEFAULT_ROLES"]).DEFAULT_ROLES)
    # Scout emits a proposal.created event: Governor subscribes; Scout must not be woken by its own event.
    ev = emit_event(db, event_type=EventType.PROPOSAL_CREATED, actor_type="agent", actor_id=report.agents["scout"], payload={})
    db.commit()
    stats = dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert stats.events_dispatched == 1 and stats.runs_created == 1
    runs = db.query(AgentRun).filter(AgentRun.event_id == ev.id).all()
    assert [r.role for r in runs] == ["governor"]
    db.refresh(ev)
    assert _ev(ev.status) == "dispatched"


def test_dispatch_targets_a_specific_agent_even_if_it_is_the_actor(db, society_settings):
    report = seed_society(db)
    routing = {}
    ev = emit_event(
        db,
        event_type=EventType.AGENT_MESSAGE_RECEIVED,
        actor_type="agent",
        actor_id=report.agents["scout"],
        subject_type="agent",
        subject_id=report.agents["scout"],
        payload={"target_agent_id": str(report.agents["scout"])},
    )
    db.commit()
    stats = dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert stats.runs_created == 1
    assert db.query(AgentRun).filter(AgentRun.event_id == ev.id, AgentRun.agent_id == report.agents["scout"]).count() == 1


def test_dispatch_without_subscriber_marks_event_ignored(db, society_settings):
    seed_society(db)
    ev = emit_event(db, event_type="nobody.cares")
    db.commit()
    stats = dispatch_pending_events(db, settings=society_settings, routing={})
    db.refresh(ev)
    assert stats.events_ignored == 1 and _ev(ev.status) == "ignored" and ev.dispatch_note == "no subscriber"


def test_unique_agent_event_prevents_double_dispatch(db, society_settings):
    report = seed_society(db)
    routing = {"t.x": ["scout", "scout"]}  # duplicated subscription on purpose
    ev = emit_event(db, event_type="t.x")
    db.commit()
    stats = dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert stats.runs_created == 1
    # simulate a replay: reset the event to pending and dispatch again
    ev.status = "pending"
    db.commit()
    stats2 = dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert stats2.runs_created == 0 and stats2.duplicates_prevented >= 1
    assert db.query(AgentRun).filter(AgentRun.event_id == ev.id).count() == 1
    assert report.agents["scout"] == db.query(AgentRun).filter(AgentRun.event_id == ev.id).first().agent_id


def test_causation_depth_loop_breaker(db, society_settings, monkeypatch):
    seed_society(db)
    routing = {"t.deep": ["scout"]}
    root = emit_event(db, event_type="t.root")
    root.status = "processed"
    ev = emit_event(db, event_type="t.deep", causation=root)
    ev.causation_depth = society_settings.max_causation_depth + 1
    db.commit()
    stats = dispatch_pending_events(db, settings=society_settings, routing=routing)
    db.refresh(ev)
    assert stats.loop_breaks == 1 and _ev(ev.status) == "ignored"
    assert "causation depth" in ev.dispatch_note
    breaker = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED).all()
    assert len(breaker) == 1 and breaker[0].correlation_id == ev.correlation_id and breaker[0].causation_depth == 0
    # a second breaker for the same correlation is deduped
    ev2 = emit_event(db, event_type="t.deep", causation=root)
    ev2.causation_depth = society_settings.max_causation_depth + 5
    db.commit()
    dispatch_pending_events(db, settings=society_settings, routing=routing)
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.LOOP_BREAKER_TRIPPED).count() == 1


def test_correlation_run_limit_loop_breaker(db, society_settings, monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    monkeypatch.setenv("SOCIETY_MAX_RUNS_PER_CORRELATION", "2")
    reset_settings_cache()
    settings = SocietySettings()
    report = seed_society(db)
    routing = {"t.c": ["scout", "governor", "architect"]}
    corr = uuid.uuid4()
    e1 = emit_event(db, event_type="t.c", correlation_id=corr)
    db.commit()
    dispatch_pending_events(db, settings=settings, routing=routing)  # creates 3 runs (limit checked before dispatch)
    assert db.query(AgentRun).filter(AgentRun.correlation_id == corr).count() == 3
    e2 = emit_event(db, event_type="t.c", correlation_id=corr, causation=e1)
    db.commit()
    stats = dispatch_pending_events(db, settings=settings, routing=routing)
    db.refresh(e2)
    assert stats.loop_breaks == 1 and _ev(e2.status) == "ignored"
    assert db.query(AgentRun).filter(AgentRun.correlation_id == corr).count() == 3
    assert report.agents  # seeded


def test_every_open_candidate_state_has_a_role_that_can_be_woken_periodically():
    """A candidate is only ever moved by a role that wakes. The Builder's wakes
    were all one-shot events, so when the loop breaker swallowed a live
    repo.read.result its candidate was stranded in REQUESTED permanently:
    nothing re-emits code_change.requested (a re-request returns duplicate=True
    without emitting), no operator route closes a candidate, and the Scout then
    CORRECTLY declines to re-propose work that already has an open candidate.
    Three locally-correct behaviours composing into a deadlock.
    """
    from services.registry.app.society.events import EventType
    from services.registry.app.society.intents import IntentType
    from services.registry.app.society.roles import DEFAULT_ROLES, ROLE_BUILDER

    builder = DEFAULT_ROLES[ROLE_BUILDER]
    assert EventType.SOCIETY_HEARTBEAT in builder.subscriptions, (
        "the only role that can move a candidate out of REQUESTED must have a "
        "periodic wake, or one lost event strands the work forever"
    )
    assert IntentType.SUBMIT_CODE_CANDIDATE.value in builder.allowed_intents
    # the wake is only useful because the Builder can already SEE open work
    from services.registry.app.society.context import _candidates  # noqa: F401


def test_the_heartbeat_reaches_the_builder_through_the_dispatch_table():
    from services.registry.app.society.events import EventType
    from services.registry.app.society.roles import DEFAULT_ROLES, ROLE_BUILDER, subscriptions_by_event

    routing = subscriptions_by_event(DEFAULT_ROLES)
    assert ROLE_BUILDER in routing.get(EventType.SOCIETY_HEARTBEAT, ())


def _agent_targeted_event_types() -> set:
    """Every event type the Society emits with ``subject_type="agent"`` (a
    targeted wake), read from the source so a new emitter is seen."""
    import ast
    import pathlib

    from services.registry.app.society import runs as runs_mod

    def targets_agent(node) -> bool:
        return any(isinstance(n, ast.Constant) and n.value == "agent" for n in ast.walk(node))

    def resolve(node):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "EventType":
            return getattr(EventType, node.attr)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    found = set()
    pkg = pathlib.Path(runs_mod.__file__).resolve().parent
    for path in pkg.rglob("*.py"):
        for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(call, ast.Call):
                continue
            kw = {k.arg: k.value for k in call.keywords if k.arg}
            if "subject_type" not in kw or not targets_agent(kw["subject_type"]):
                continue
            name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
            node = kw.get("event_type") if name == "emit_event" else (call.args[1] if name == "_emit" and len(call.args) > 1 else None)
            etype = resolve(node) if node is not None else None
            assert etype is not None, f"{path.name}:{call.lineno}: cannot resolve the event type of an agent-targeted emit"
            found.add(etype)
    return found


def test_an_agent_targeted_event_is_never_also_broadcast_to_its_subscribers():
    """Structural guard for the staging failure of 2026-09-26: repo.read.result
    targets the reading agent, but dispatch also woke every role subscribed to
    the type, spending three runs of the correlation's loop-breaker budget per
    read until the Architect's code_change.requested was ignored. An event type
    emitted as a targeted wake may be subscribed by a role only if it is
    TARGETED_ONLY (dispatch then wakes the target alone); otherwise a new
    subscription silently recreates the broadcast."""
    from services.registry.app.society.roles import DEFAULT_ROLES
    from services.registry.app.society.runs import TARGETED_ONLY_EVENT_TYPES

    targeted = _agent_targeted_event_types()
    assert EventType.REPO_READ_RESULT in targeted, "the scan must see the executor's read-result wake"
    routing = subscriptions_by_event(DEFAULT_ROLES)
    broadcast = sorted(t for t in targeted if routing.get(t) and t not in TARGETED_ONLY_EVENT_TYPES)
    assert broadcast == [], f"agent-targeted events also broadcast to subscribers: {broadcast}"
