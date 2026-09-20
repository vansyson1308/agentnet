"""Operator remedy for a candidate that can never finish.

The Builder is the only role that can move a candidate out of REQUESTED, and
the loop breaker can legitimately swallow the event that would wake it. Before
this endpoint the result was permanent: nothing re-emitted the wake, no route
could close the row, and the Scout then correctly declined to re-propose work
that already had an open candidate.
"""

from __future__ import annotations

import uuid

import pytest

from services.registry.app.models import (
    CodeCandidate,
    CodeCandidateStatus,
    CodePromotion,
    PromotionStatus,
    SocietyEvent,
)
from services.registry.app.society import candidate_admin as ca
from services.registry.app.society.seed import seed_society


def _candidate(db, *, status=CodeCandidateStatus.REQUESTED, task_id=None):
    row = CodeCandidate(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        title="Annotate a superseded claim",
        spec={"kind": "docs", "files_allowed": ["docs/society/candidates/x.md"]},
        status=status,
        task_id=task_id,
        changed_files=[],
        qa_report={},
        security_report={},
    )
    db.add(row)
    db.flush()
    return row


def test_only_an_operator_can_abandon_over_http(api_client, db, society_settings, user_token, agent_token):
    from .conftest import auth

    seed_society(db)
    row = _candidate(db)
    db.commit()
    path = f"/v1/society/candidates/{row.id}/abandon"
    body = {"reason": "the builder wake was lost; this candidate can never progress"}

    _, user_tok = user_token(None)
    _, producer_tok = user_token("event_producer")
    _, agent_tok = agent_token()

    assert api_client.post(path, json=body).status_code == 401
    assert api_client.post(path, headers=auth(user_tok), json=body).status_code == 403
    assert api_client.post(path, headers=auth(producer_tok), json=body).status_code == 403
    assert api_client.post(path, headers=auth(agent_tok), json=body).status_code == 403

    db.refresh(row)
    assert row.status == CodeCandidateStatus.REQUESTED, "a refused caller must not have changed anything"

    _, op_tok = user_token("operator")
    r = api_client.post(path, headers=auth(op_tok), json=body)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["candidate"]["status"] == "abandoned"
    assert payload["already_abandoned"] is False


def test_abandon_is_idempotent_and_never_deletes(api_client, db, society_settings, user_token):
    from .conftest import auth

    seed_society(db)
    row = _candidate(db)
    cid, corr, title = row.id, row.correlation_id, row.title
    db.commit()
    path = f"/v1/society/candidates/{cid}/abandon"
    _, op_tok = user_token("operator")

    first = api_client.post(path, headers=auth(op_tok), json={"reason": "stranded by a lost wake"})
    second = api_client.post(path, headers=auth(op_tok), json={"reason": "a different second reason"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["already_abandoned"] is False
    assert second.json()["already_abandoned"] is True

    db.expire_all()
    kept = db.query(CodeCandidate).filter(CodeCandidate.id == cid).one()
    assert kept.title == title and kept.correlation_id == corr, "the row is preserved, never rewritten"
    assert kept.error == "stranded by a lost wake", "the repeat must not overwrite the recorded reason"

    events = db.query(SocietyEvent).filter(SocietyEvent.event_type == "code_candidate.abandoned", SocietyEvent.subject_id == cid).all()
    assert len(events) == 1, "a repeat writes no second audit event"
    assert events[0].payload["previous_status"] == "requested"
    assert events[0].payload["reason"] == "stranded by a lost wake"


def test_a_reason_is_required_and_bounded(db, society_settings, user_token):
    seed_society(db)
    row = _candidate(db)
    operator, _ = user_token("operator")
    for bad in ("", "   "):
        with pytest.raises(ca.AbandonRefused):
            ca.abandon(db, candidate_id=row.id, reason=bad, operator=operator)
    with pytest.raises(ca.AbandonRefused):
        ca.abandon(db, candidate_id=row.id, reason="x" * (ca.MAX_REASON_CHARS + 1), operator=operator)
    db.rollback()


def test_a_merged_candidate_cannot_be_abandoned(db, society_settings, user_token):
    seed_society(db)
    row = _candidate(db, status=CodeCandidateStatus.READY)
    db.add(
        CodePromotion(
            id=uuid.uuid4(),
            candidate_id=row.id,
            correlation_id=row.correlation_id,
            risk_tier="green",
            provider="github",
            status=PromotionStatus.MERGED,
            eligibility={},
            evidence={},
        )
    )
    db.flush()
    operator, _ = user_token("operator")
    with pytest.raises(ca.AbandonRefused, match="merged"):
        ca.abandon(db, candidate_id=row.id, reason="tidying up", operator=operator)
    db.refresh(row)
    assert row.status == CodeCandidateStatus.READY


@pytest.mark.parametrize("terminal", [CodeCandidateStatus.REJECTED, CodeCandidateStatus.FAILED])
def test_an_already_closed_candidate_is_not_reopened_as_abandoned(db, society_settings, user_token, terminal):
    seed_society(db)
    row = _candidate(db, status=terminal)
    operator, _ = user_token("operator")
    with pytest.raises(ca.AbandonRefused):
        ca.abandon(db, candidate_id=row.id, reason="tidying up", operator=operator)
    db.refresh(row)
    assert row.status == terminal, "abandoning must not rewrite how the work actually ended"


def test_unknown_candidate_is_404(api_client, db, society_settings, user_token):
    from .conftest import auth

    seed_society(db)
    db.commit()
    _, op_tok = user_token("operator")
    r = api_client.post(f"/v1/society/candidates/{uuid.uuid4()}/abandon", headers=auth(op_tok), json={"reason": "x"})
    assert r.status_code == 404


def test_abandon_closes_an_in_flight_task_through_the_escrow_path():
    """Never a wallet write: the refund goes through fail_task_with_refund, which
    takes the row locks and releases the reservation exactly once."""
    import inspect

    src = inspect.getsource(ca.abandon)
    assert "task_service.fail_task_with_refund" in src
    for forbidden in ("balance_credits =", "reserved_credits =", "wallet."):
        assert forbidden not in src, f"abandon must never move money itself: {forbidden!r}"
    # a task already terminal is left alone, so a repeat cannot double-release
    assert "TaskStatus.INITIATED.value, TaskStatus.IN_PROGRESS.value" in src


def test_there_is_no_intent_type_for_abandoning():
    """A society that can retire its own unfinished work can retire the evidence
    that it failed."""
    from services.registry.app.society.intents import IntentType

    assert not [t for t in IntentType if "ABANDON" in t.value.upper()]


def test_abandoning_reopens_the_proposal_for_a_new_candidate(db, society_settings, user_token):
    """The whole point of the remedy.

    ``_request_code_change`` refuses a second candidate while one is still open
    for the same proposal — correctly, or a swallowed wake would be answered by
    an unbounded pile of duplicates. That guard is exactly what made the
    deadlock permanent, so ABANDONED must be one of the statuses it treats as
    closed, or abandoning changes nothing that matters.
    """
    import inspect

    from services.registry.app.society import executor

    src = inspect.getsource(executor._request_code_change)
    open_guard = [ln for ln in src.splitlines() if "CodeCandidate.proposal_id" in ln]
    assert open_guard, "the open-candidate guard moved; this test must follow it"
    assert "CodeCandidateStatus.ABANDONED" in open_guard[0], (
        "an abandoned candidate must not count as open, or the operator remedy "
        "closes the row without unblocking the proposal"
    )


def test_abandon_emits_exactly_one_causation_linked_audit_event(db, society_settings, user_token):
    seed_society(db)
    row = _candidate(db)
    db.commit()
    operator, _ = user_token("operator")

    ca.abandon(db, candidate_id=row.id, reason="the builder wake was lost", operator=operator)
    db.commit()

    events = (
        db.query(SocietyEvent)
        .filter(
            SocietyEvent.event_type == "code_candidate.abandoned",
            SocietyEvent.subject_id == row.id,
        )
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.correlation_id == row.correlation_id, "the audit row must join the candidate's own story"
    assert ev.actor_type == "operator"
    assert ev.idempotency_key == f"candidate-abandoned:{row.id}"
    assert ev.payload["previous_status"] == "requested"
    assert "@" not in str(ev.payload), "an operator's email never enters a payload"
