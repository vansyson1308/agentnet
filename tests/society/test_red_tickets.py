"""Harness tickets are RED: a plan holds only the RED work the company can still take on,
and a RED candidate's PR goes to the owner merge queue with its BENCH VERDICT numbers."""

from __future__ import annotations

import uuid

from services.registry.app.models import CodeCandidate, CodePromotion, ImprovementProposal
from services.registry.app.society import candidate_health, promotion, tickets
from services.registry.app.society.events import utcnow

from .test_build_engine import TESTS
from .test_company_tickets import _activate, company  # noqa: F401 -- fixture

BENCH = {"task_id": "activity-loop-final-turn", "repeat": 3, "baseline_delivered": 0, "delivered": 2, "runs": ["pass", "turn_budget", "pass"],
         "cost_usd": "0.031", "passed": True, "rule": "delivered >= 2/3 and > baseline", "judge": "running revision"}


def _proposed(db, *, bench=None, effect=0.02, department_role="architect"):
    p = ImprovementProposal(id=uuid.uuid4(), source="audit", title=f"t-{uuid.uuid4().hex[:6]}", problem="p", proposed_change="c", status="PROPOSED",
                            target_scope="platform", importance=60)
    db.add(p)
    db.flush()
    proof = [f"bench:{bench}"] if bench else TESTS + [f"tests/test_textutil.py::t{uuid.uuid4().hex[:4]}"]
    tickets.create(db, proposal_id=p.id, title=p.title, fields={"objective_id": "O3", "metric_id": "bench_holdout_pass_at_1", "expected_effect": effect,
                                                                "direction": "up", "proof": proof}, source="scout", role=department_role, importance=60)
    db.commit()
    return tickets.for_proposal(db, p.id)


def test_a_plan_holds_at_most_the_free_red_slots_and_fills_the_rest_with_non_red(db, company):
    _activate(db)
    reds = [_proposed(db, bench=f"task-{i}", effect=0.05) for i in range(3)]
    green = _proposed(db, effect=0.01, department_role="qa")
    plan = tickets.build_plan(db, None, utcnow(), red_cap=1)
    db.commit()
    chosen = {r["ticket_id"]: r["risk"] for r in plan["ranking"]}
    assert list(chosen.values()).count("RED") == 1 and chosen[str(green["id"])] == "judged_from_diff"
    assert str(reds[0]["id"]) in chosen and len(chosen) == 2
    # the RED ticket is approved and waits for its design: the next plan has no RED slot
    tickets.decide_plan(db, uuid.UUID(plan["id"]), approve=True, user_id=None)
    assert tickets.red_slots(db, 1) == 0
    again = tickets.build_plan(db, None, utcnow(), red_cap=1)
    assert again["id"] is None and "2 proposed RED ticket(s) wait: no high-risk slot is free" in again["empty_reason"]
    assert tickets.build_plan(db, None, utcnow(), red_cap=2)["ranking"][0]["risk"] == "RED"


def test_a_red_harness_pr_carries_the_bench_verdict_and_sits_in_the_owner_merge_queue(db):
    cand = CodeCandidate(id=uuid.uuid4(), correlation_id=uuid.uuid4(), title="harness: final-turn pacing", status="ready", risk_tier="red",
                         spec={"kind": "code", "acceptance_tests": ["tests/test_bench.py", "bench:activity-loop-final-turn"],
                               "expected_effect": "[O3 bench_holdout_pass_at_1 up 0.0111] the builder harness delivers bench dev task activity-loop-final-turn"},
                         qa_report={"verdict": "pass", "attempts": 1, "summary": "PASS: all checks green", "bench_proof": [BENCH]})
    db.add(cand)
    db.flush()
    promo = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="red", provider="fake", status="awaiting_approval",
                          external_pr_number=101, external_pr_url="https://github.com/o/r/pull/101")
    db.add(promo)
    green = CodeCandidate(id=uuid.uuid4(), correlation_id=uuid.uuid4(), title="docs", status="ready", risk_tier="green", spec={})
    db.add(green)
    db.flush()
    db.add(CodePromotion(id=uuid.uuid4(), candidate_id=green.id, correlation_id=green.correlation_id, risk_tier="green", provider="fake", status="pr_open"))
    db.commit()
    body = promotion.pr_body_from_facts(db, promo, cand)
    assert "[O3 bench_holdout_pass_at_1 up 0.0111]" in body and "**owner merge queue**" in body
    assert "### Bench verdict" in body and "**PASS** · candidate harness 2/3 vs main baseline 0/3" in body and "cost $0.031" in body
    queue = candidate_health.owner_merge_queue(db)
    assert len(queue) == 1 and queue[0]["pr_number"] == 101 and queue[0]["bench_verdict"][0]["delivered"] == 2
    assert promotion.bench_verdict_lines({"verdict": "pass"}) == []
