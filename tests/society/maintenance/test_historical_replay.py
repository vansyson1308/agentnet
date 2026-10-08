"""Historical replay (mission 184-185): the control-plane failures that each
needed a one-off Society patch (#51 #52 #53 #55 #57 #58 #59 #60 #61 #62),
replayed against the Maintenance OS lifecycle. None of them needs a patch
here, because none of the mechanisms they broke is on the maintenance path:

  #51 portfolio full / hypothesis counting -> maintenance has its own queue
  #52 false memory                          -> memory never covers an incident
  #53 trusted signal coverage               -> coverage = active RepairCase
  #55 read result woke the wrong agent      -> activities are calls, not wakes
  #57 reads lost between stories            -> inputs come from durable rows
  #58/#59 truncated context                 -> paged reads, explicit cursors
  #60 Builder cannot finish within spec     -> NEEDS_RESCOPE -> revision N+1
  #61 concluded proposal covered the defect -> no proposal lifecycle at all
  #62 wake swallowed by the loop breaker    -> reconciliation needs no event
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from services.registry.app.maintenance import incidents as inc_mod
from services.registry.app.maintenance.orm import RepairActivity, RepairCase

from .conftest import PAGE, at, green_repair_script, raise_incident
from .test_kernel_scenarios import base_script

pytestmark = pytest.mark.timeout(600)


def _case(db):
    db.expire_all()
    return db.query(RepairCase).one()


def _drive(k, n=10, start=2):
    for i in range(n):
        k.reconcile(now=at(start + i * 0.01))


def test_pr51_61_a_full_innovation_portfolio_and_concluded_proposals_are_irrelevant(db, mset, kernel_factory, make_agent, monkeypatch):
    from services.registry.app.models import ImprovementProposal, ProposalSource, ProposalStatus

    monkeypatch.setenv("SOCIETY_COMPANY_MAX_ACTIVE_HYPOTHESES", "0")
    agent = make_agent("Society_Scout")
    for status in (ProposalStatus.PROPOSED, ProposalStatus.APPROVED, ProposalStatus.IMPLEMENTED):
        db.add(ImprovementProposal(id=uuid.uuid4(), title=f"fix the marketplace page ({status.value})", problem="the page renders raw dicts", proposed_by_agent_id=agent.id, source=ProposalSource.AUDIT, status=status))
    db.commit()
    raise_incident(db, mset)
    _drive(kernel_factory(green_repair_script()))
    assert _case(db).state == "PROMOTING", "no portfolio slot, no proposal state, no Scout decides coverage"


def test_pr52_53_memory_and_signals_never_cover_an_incident(db, mset, make_agent):
    from services.registry.app.models import MemoryItem, MemoryScope

    inc = raise_incident(db, mset)
    a = make_agent("Society_Scout")
    db.add(MemoryItem(id=uuid.uuid4(), agent_id=a.id, scope=MemoryScope.AGENT, title="covered", content="dashboard incident is covered by proposal X", tags=[]))
    db.commit()
    assert inc_mod.is_covered(db, inc.fingerprint) is False


def test_pr55_62_swallowed_or_missing_events_cannot_strand_a_case(db, mset, kernel_factory, monkeypatch):
    monkeypatch.setenv("SOCIETY_MAX_RUNS_PER_CORRELATION", "1")
    raise_incident(db, mset)
    k = kernel_factory(green_repair_script())
    k.reconcile(now=at(2))
    db.execute(text("DELETE FROM society_events"))  # every wake-up hint lost
    db.commit()
    k2 = kernel_factory(green_repair_script(), worker_id="restarted")
    _drive(k2, start=20)
    assert _case(db).state == "PROMOTING"


def test_pr57_58_59_inputs_come_from_durable_rows_and_reads_are_paged(db, mset, kernel_factory):
    raise_incident(db, mset)
    seen = []

    def diag(messages, n):
        seen.append(messages[1]["content"])
        if n == 1:
            return {"action": "read_range", "args": {"path": PAGE, "start_line": 1}}
        return {"action": "submit", "result": {"root_cause": "template prints the dict", "suspected_files": [PAGE], "evidence": [], "confidence": "high"}}

    k = kernel_factory(base_script({"DiagnoseIncident": diag}))
    k.reconcile(now=at(2))
    act = db.query(RepairActivity).filter(RepairActivity.kind == "DiagnoseIncident").first()
    assert act is not None and act.turns >= 2
    assert '"desired_state_ref": "marketplace"' in seen[0] and "latest_structural_evidence" in seen[0]


def test_pr60_builder_inability_is_a_rescope_in_the_same_case(db, mset, kernel_factory):
    from services.registry.app.maintenance.orm import RepairPlanRevision

    from .conftest import MAIN, VERIFY
    from .test_kernel_scenarios import amber_patch

    raise_incident(db, mset)

    def design(messages, n):
        return {"action": "submit", "result": {"root_cause": "x" * 20, "approach": "y" * 20, "files_allowed": [PAGE] if n == 1 else [PAGE, MAIN], "acceptance_tests": [VERIFY]}}

    def patch(messages, n):
        if n == 1:
            return {"action": "needs_rescope", "result": {"reason": "acceptance_unsatisfiable", "required_files": [MAIN], "evidence": "needs the route helper"}}
        return amber_patch(messages, n)

    _drive(kernel_factory(base_script({"DesignRepair": design, "AuthorPatch": patch})), n=12)
    assert db.query(RepairCase).count() == 1 and db.query(RepairPlanRevision).count() == 2
