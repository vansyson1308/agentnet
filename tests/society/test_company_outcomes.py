"""Company mode: a merged ticket's KR delta at +24h / +7d, and the owner's KPIs (outcomes.py)."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from sqlalchemy import text

from services.registry.app.models import CodeCandidate, CodePromotion, ImprovementProposal
from services.registry.app.society import outcomes, tickets
from services.registry.app.society.events import utcnow
from services.registry.app.society.seed import seed_society


def _holdout(db, value, at):
    db.execute(text("INSERT INTO society_bench_reports (id, revision, judge_revision, summary, created_at) VALUES (:i, 'r', 'r', :s, :at)"),
               {"i": str(uuid.uuid4()), "s": json.dumps({"splits": {"holdout": {"pass_at_1": value}}}), "at": at})
    db.commit()


def _merged_ticket(db, report, now):
    p = ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=report.agents["scout"], source="audit", title="harness", problem="p",
                            proposed_change="c", status="APPROVED", target_scope="platform", importance=60)
    db.add(p)
    db.flush()
    tid = tickets.create(db, proposal_id=p.id, title="harness", fields={"objective_id": "O3", "metric_id": "bench_holdout_pass_at_1", "expected_effect": 0.02,
                         "direction": "up", "proof": ["bench:t"]}, source="owner", role="governor", importance=60)
    cand = CodeCandidate(id=uuid.uuid4(), proposal_id=p.id, correlation_id=uuid.uuid4(), requested_by_agent_id=report.agents["architect"], title="harness",
                         status="ready", risk_tier="red", spec={"kind": "code"})
    db.add(cand)
    db.flush()
    tickets.set_status(db, tid, "ready", candidate_id=cand.id, cost_usd=0.5)
    db.add(CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="red", provider="fake", status="merged",
                         merged_sha="abc123", updated_at=now))
    db.commit()
    return tid


def _row(db, tid):
    return db.execute(text("SELECT * FROM society_tickets WHERE id = :i"), {"i": tid}).mappings().one()


def test_outcome_compares_the_reading_with_the_one_at_merge_in_the_krs_direction():
    assert outcomes.outcome("up", 0.50, 0.53, 0.02) == "moved"
    assert outcomes.outcome("up", 0.50, 0.501, 0.02) == "no_effect", "within 10% of the expected effect"
    assert outcomes.outcome("down", 5.0, 6.0, 1.0) == "regressed"
    assert outcomes.outcome("up", None, 0.6, 0.02) is None, "no reading at merge, no verdict"


def test_a_merged_ticket_gets_its_kr_readings_at_merge_24h_and_7d(db):
    report = seed_society(db)
    now = utcnow()
    _holdout(db, 0.50, now - timedelta(hours=1))
    tid = _merged_ticket(db, report, now)
    assert outcomes.record(db, now) == 1
    db.commit()
    row = _row(db, tid)
    assert row["status"] == "merged" and float(row["metric_at_merge"]) == 0.5 and row["metric_24h"] is None
    _holdout(db, 0.60, now + timedelta(hours=20))
    outcomes.record(db, now + timedelta(hours=25))
    db.commit()
    row = _row(db, tid)
    assert float(row["metric_24h"]) == 0.6 and row["outcome"] == "moved" and row["metric_7d"] is None, "+24h is recorded once, provisionally"
    _holdout(db, 0.45, now + timedelta(days=6))
    outcomes.record(db, now + timedelta(days=8))
    db.commit()
    row = _row(db, tid)
    assert float(row["metric_7d"]) == 0.45 and row["outcome"] == "regressed", "+7d is final"
    assert outcomes.record(db, now + timedelta(days=9)) == 0, "idempotent"


def test_kpis_show_kr_progress_meaningful_rate_and_dollars_per_meaningful_pr(db):
    report = seed_society(db)
    now = utcnow()
    db.execute(text("INSERT INTO society_objective_status (objective_id, status) VALUES ('O3', 'active')"))
    _holdout(db, 0.50, now - timedelta(hours=1))
    tid = _merged_ticket(db, report, now)
    outcomes.record(db, now)
    db.execute(text("UPDATE society_tickets SET outcome = 'moved' WHERE id = :i"), {"i": tid})
    db.commit()
    k = outcomes.kpis(db, now)
    o3 = next(r for r in k["key_results"] if r["metric_id"] == "bench_holdout_pass_at_1")
    assert o3["current"] == 0.5 and o3["baseline"] == 0.763 and o3["progress"] < 0, "below its baseline: negative progress, shown as is"
    assert k["tickets_by_outcome"] == {"moved": 1} and k["meaningful_rate"] == 1.0 and k["usd_per_meaningful_pr"] == 0.5
    assert k["summary_text"].splitlines()[-1] == "meaningful rate: 1.0; $ per meaningful PR: 0.5"
    assert "O3 bench_holdout_pass_at_1: 0.5 -> 0.863 (up)" in k["summary_text"]
