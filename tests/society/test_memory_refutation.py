"""Audited memory refutation: correcting the record without falsifying it.

Phase 5's residual blocker: a Scout's CREATE_IMPROVEMENT was refused for a
schema violation while the same run's WRITE_MEMORY recorded "improvement
raised". That belief was false from the first minute, never expired (it came
from a real signal), and three later runs declined the same signal citing it.
Nothing could demote it without a hand-written database edit.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from services.registry.app.models import (
    Agent,
    MemoryItem,
    MemoryScope,
    MemoryValidationEvent,
    SocietyEvent,
    User,
)
from services.registry.app.society import memory_validation as mv
from services.registry.app.society.seed import seed_society


def _memory(db, agent, *, title="improvement raised", state="unvalidated"):
    row = MemoryItem(
        id=uuid.uuid4(),
        agent_id=agent.id,
        scope=MemoryScope.AGENT,
        title=title,
        content="Triage: stale doc reported; improvement raised.",
        tags=[],
        importance=60,
        confidence=60,
        validation_state=state,
        correlation_id=uuid.uuid4(),
    )
    db.add(row)
    db.commit()
    return row


def _scout(db):
    report = seed_society(db)
    return db.query(Agent).filter(Agent.id == report.agents["scout"]).first()


# ── trusted module ────────────────────────────────────────────────────


def test_refutation_demotes_without_destroying_evidence(db):
    scout = _scout(db)
    row = _memory(db, scout)
    created_before, content_before = row.created_at, row.content

    res = mv.refute(db, memory_id=row.id, reason="the intent it describes was denied", actor_type="operator")
    db.commit()

    assert res.already_refuted is False
    db.refresh(row)
    assert row.validation_state == "refuted"
    # the belief survives: we believed this and were wrong is itself evidence
    assert row.content == content_before and row.created_at == created_before
    assert db.query(MemoryItem).filter(MemoryItem.id == row.id).first() is not None


def test_refutation_writes_one_audit_row_and_one_event(db):
    scout = _scout(db)
    row = _memory(db, scout)
    mv.refute(db, memory_id=row.id, reason="denied intent", actor_type="operator")
    db.commit()

    records = db.query(MemoryValidationEvent).filter(MemoryValidationEvent.memory_id == row.id).all()
    assert len(records) == 1
    assert records[0].from_state == "unvalidated" and records[0].to_state == "refuted"
    assert records[0].actor_type == "operator" and "denied" in records[0].reason

    events = db.query(SocietyEvent).filter(SocietyEvent.event_type == "memory.refuted").all()
    assert len(events) == 1 and events[0].subject_id == row.id


def test_refutation_is_idempotent(db):
    scout = _scout(db)
    row = _memory(db, scout)
    mv.refute(db, memory_id=row.id, reason="first", actor_type="operator")
    db.commit()
    again = mv.refute(db, memory_id=row.id, reason="second", actor_type="operator")
    db.commit()

    assert again.already_refuted is True and again.record is None
    assert db.query(MemoryValidationEvent).filter(MemoryValidationEvent.memory_id == row.id).count() == 1
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "memory.refuted").count() == 1


def test_unknown_memory_and_empty_reason_are_refused(db):
    _scout(db)
    with pytest.raises(mv.MemoryNotFound):
        mv.refute(db, memory_id=uuid.uuid4(), reason="x", actor_type="operator")
    with pytest.raises(ValueError):
        mv.refute(db, memory_id=uuid.uuid4(), reason="   ", actor_type="operator")
    with pytest.raises(ValueError):
        mv.refute(db, memory_id=uuid.uuid4(), reason="ok", actor_type="model")


def test_trusted_evaluator_may_refute_without_a_human(db):
    """§52: objective evidence may disprove a belief with no operator present."""
    scout = _scout(db)
    row = _memory(db, scout)
    mv.refute(
        db, memory_id=row.id, reason="failure still observed after the claimed fix",
        actor_type="evaluator", evidence={"experiment": "e-1"},
    )
    db.commit()
    rec = db.query(MemoryValidationEvent).filter(MemoryValidationEvent.memory_id == row.id).one()
    assert rec.actor_type == "evaluator" and rec.actor_user_id is None
    assert rec.evidence == {"experiment": "e-1"}


# ── the audit log is append-only in the DATABASE ──────────────────────


def test_audit_history_cannot_be_updated_or_deleted(db):
    scout = _scout(db)
    row = _memory(db, scout)
    mv.refute(db, memory_id=row.id, reason="denied intent", actor_type="operator")
    db.commit()
    rec_id = db.query(MemoryValidationEvent).filter(MemoryValidationEvent.memory_id == row.id).one().id

    from sqlalchemy import text

    for stmt in (
        text("UPDATE memory_validation_events SET reason = 'rewritten' WHERE id = :i"),
        text("DELETE FROM memory_validation_events WHERE id = :i"),
    ):
        with pytest.raises(Exception) as exc:
            db.execute(stmt, {"i": str(rec_id)})
            db.commit()
        assert "append-only" in str(exc.value)
        db.rollback()

    assert db.query(MemoryValidationEvent).filter(MemoryValidationEvent.id == rec_id).one().reason != "rewritten"


# ── retrieval ranking ─────────────────────────────────────────────────


def test_refuted_memory_ranks_below_an_identical_live_one(db):
    from datetime import datetime, timezone

    from services.registry.app.society.context import memory_rank

    scout = _scout(db)
    live = _memory(db, scout, title="live")
    refuted = _memory(db, scout, title="refuted")
    mv.refute(db, memory_id=refuted.id, reason="disproved", actor_type="operator")
    db.commit()
    db.refresh(refuted)

    now = datetime.now(timezone.utc)
    assert memory_rank(refuted, now) < memory_rank(live, now)


def test_refuted_memory_is_still_queryable_for_audit(db):
    scout = _scout(db)
    row = _memory(db, scout)
    mv.refute(db, memory_id=row.id, reason="disproved", actor_type="operator")
    db.commit()
    assert db.query(MemoryItem).filter(MemoryItem.validation_state == "refuted").count() == 1
    assert len(mv.history(db, row.id)) == 1


# ── authority: operator only ──────────────────────────────────────────


def test_no_intent_type_can_refute_memory():
    """A model cannot grade its own evidence: there is no such intent."""
    from services.registry.app.society.intents import ALLOWED_INTENT_TYPES

    names = {t.value for t in ALLOWED_INTENT_TYPES}
    assert not any("REFUTE" in n or "VALIDATE_MEMORY" in n for n in names)


def test_memory_refuted_is_not_injectable_from_outside():
    """`memory.refuted` is society-produced; the ingress allowlist excludes it."""
    from services.registry.app.society.config import get_settings

    assert "memory.refuted" not in get_settings().ingress_event_allowlist


# ── HTTP surface: the full authority matrix ───────────────────────────


def _seeded_memory(db):
    scout = _scout(db)
    return _memory(db, scout)


def test_only_an_operator_can_refute_over_http(api_client, db, society_settings, user_token, agent_token):
    from .conftest import auth

    row = _seeded_memory(db)
    path = f"/v1/society/memory/{row.id}/refute"
    body = {"reason": "the intent it describes was denied and never executed"}

    _, user_tok = user_token(None)
    _, producer_tok = user_token("event_producer")
    _, agent_tok = agent_token()

    assert api_client.post(path, json=body).status_code == 401
    assert api_client.post(path, headers=auth(user_tok), json=body).status_code == 403
    assert api_client.post(path, headers=auth(producer_tok), json=body).status_code == 403
    assert api_client.post(path, headers=auth(agent_tok), json=body).status_code == 403

    db.refresh(row)
    assert row.validation_state == "unvalidated", "a refused caller must not have changed anything"

    _, op_tok = user_token("operator")
    r = api_client.post(path, headers=auth(op_tok), json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["memory"]["validation_state"] == "refuted"
    assert payload["already_refuted"] is False
    assert len(payload["history"]) == 1 and payload["history"][0]["actor_type"] == "operator"


def test_scoped_token_cannot_refute(api_client, db, society_settings, user_token, make_agent):
    import hashlib

    from services.registry.app.models import ScopedToken

    from .conftest import auth

    row = _seeded_memory(db)
    owner, _ = user_token("operator")
    agent = make_agent("Refute_Probe", user=owner)
    raw = "spt_" + uuid.uuid4().hex
    db.add(ScopedToken(id=uuid.uuid4(), token_hash=hashlib.sha256(raw.encode()).hexdigest(), agent_id=agent.id, resource_type="domain", allowed_actions=["read"]))
    db.commit()

    r = api_client.post(f"/v1/society/memory/{row.id}/refute", headers=auth(raw), json={"reason": "nope"})
    assert r.status_code in (401, 403), r.text
    db.refresh(row)
    assert row.validation_state == "unvalidated"


def test_http_refute_unknown_memory_is_404_and_empty_reason_is_422(api_client, db, society_settings, user_token):
    from .conftest import auth

    _, op_tok = user_token("operator")
    r = api_client.post(f"/v1/society/memory/{uuid.uuid4()}/refute", headers=auth(op_tok), json={"reason": "x"})
    assert r.status_code == 404

    row = _seeded_memory(db)
    r = api_client.post(f"/v1/society/memory/{row.id}/refute", headers=auth(op_tok), json={"reason": ""})
    assert r.status_code == 422


def test_http_refute_is_idempotent(api_client, db, society_settings, user_token):
    from .conftest import auth

    row = _seeded_memory(db)
    _, op_tok = user_token("operator")
    path = f"/v1/society/memory/{row.id}/refute"
    body = {"reason": "disproved by trusted execution records"}

    first = api_client.post(path, headers=auth(op_tok), json=body)
    second = api_client.post(path, headers=auth(op_tok), json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["already_refuted"] is False
    assert second.json()["already_refuted"] is True
    assert db.query(MemoryValidationEvent).filter(MemoryValidationEvent.memory_id == row.id).count() == 1
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "memory.refuted").count() == 1


# ── §51 memory truth hierarchy ────────────────────────────────────────

TRUTH_HIERARCHY = (
    "trusted current facts",
    "trusted refusal/execution records",
    "validated memory",
    "unvalidated memory",
    "refuted memory",
)


def test_memory_tiers_rank_in_the_documented_order(db):
    """validated > unvalidated > refuted, for otherwise identical rows."""
    from datetime import datetime, timezone

    from services.registry.app.society.context import memory_rank

    scout = _scout(db)
    validated = _memory(db, scout, title="v", state="validated")
    unvalidated = _memory(db, scout, title="u", state="unvalidated")
    refuted = _memory(db, scout, title="r", state="unvalidated")
    mv.refute(db, memory_id=refuted.id, reason="disproved", actor_type="operator")
    db.commit()
    db.refresh(refuted)

    now = datetime.now(timezone.utc)
    assert memory_rank(validated, now) > memory_rank(unvalidated, now) > memory_rank(refuted, now)


def test_a_superseded_memory_sinks_below_its_replacement(db):
    from datetime import datetime, timezone

    from services.registry.app.society.context import memory_rank

    scout = _scout(db)
    old = _memory(db, scout, title="old")
    new = _memory(db, scout, title="new")
    mv.refute(db, memory_id=old.id, reason="replaced", actor_type="evaluator", superseded_by=new.id)
    db.commit()
    db.refresh(old)

    assert old.superseded_by == new.id
    now = datetime.now(timezone.utc)
    assert memory_rank(old, now) < memory_rank(new, now)


def test_trusted_refusal_records_outrank_agent_notes_in_the_prompt():
    """A model-authored memory may never outrank contradictory trusted
    execution evidence. The runtime states that precedence explicitly."""
    import re

    from services.registry.app.society.cognition import SYSTEM_PROMPT

    # the rule is wrapped across lines in the source; compare on flattened text
    flat = re.sub(r"\s+", " ", SYSTEM_PROMPT)
    assert "recent_refusals" in flat
    assert "A refused intent never took effect" in flat
    assert "no matter what a memory item" in flat
    assert "Trust that list over your own notes when they disagree" in flat


def test_the_hierarchy_is_documented_where_operators_look():
    import pathlib

    doc = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "SOCIETY_RUNTIME.md").read_text(encoding="utf-8")
    for tier in TRUTH_HIERARCHY:
        assert tier in doc, f"truth hierarchy tier not documented: {tier}"
