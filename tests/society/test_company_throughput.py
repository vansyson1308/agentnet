"""Company throughput: SOCIETY_COMPANY_CYCLES_PER_DAY scheduled cycles a day, a configurable plan size."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest

from services.registry.app.models import CompanyCycle, ImprovementProposal
from services.registry.app.society import company, tickets
from services.registry.app.society.config import SocietyConfigError, SocietySettings

from .test_company_tickets import _activate, company as company_fixture  # noqa: F401 -- fixture


def _at(h, m=0):
    return datetime(2026, 10, 26, h, m, tzinfo=timezone.utc)


def _s(society_settings, n, hour=1):
    return dataclasses.replace(society_settings, company_cycle_enabled=True, company_cycles_per_day=n, company_cycle_hour_utc=hour)


def test_slots_spread_from_the_cycle_hour_to_midnight(society_settings):
    four = _s(society_settings, 4)
    assert company.scheduled_slot(four, _at(0, 59)) is None
    assert [company.scheduled_slot(four, _at(h, m)) for h, m in ((1, 0), (6, 44), (6, 45), (12, 30), (18, 15), (23, 59))] == [0, 0, 1, 2, 3, 3]
    one = _s(society_settings, 1)
    assert {company.scheduled_slot(one, _at(h)) for h in range(1, 24)} == {0}, "default: one cycle a day, as before"


def test_four_cycles_a_day_each_once_and_operator_cycles_stay_extra(db, society_settings):
    s = _s(society_settings, 4)
    started = [company.maybe_start_scheduled_cycle(db, s, now=_at(h, m)) for h, m in ((1, 5), (2, 0), (7, 0), (13, 0), (19, 0), (23, 0))]
    assert [c.slot if c else None for c in started] == [0, None, 1, 2, 3, None], "one per slot, never two"
    assert company.start_cycle(db, s, trigger="operator", now=_at(20)) is not None
    rows = db.query(CompanyCycle).all()
    assert len(rows) == 5 and sum(r.trigger == "scheduled" for r in rows) == 4


def test_a_missed_slot_is_not_back_filled(db, society_settings):
    s = _s(society_settings, 4)
    assert company.maybe_start_scheduled_cycle(db, s, now=_at(14)).slot == 2
    assert company.maybe_start_scheduled_cycle(db, s, now=_at(15)) is None
    assert db.query(CompanyCycle).count() == 1


def test_the_plan_size_is_configurable(db, company_fixture):
    report, _ = company_fixture
    _activate(db, "O3")
    for i in range(6):
        p = ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=report.agents["scout"], source="audit", title=f"t{i} {uuid.uuid4().hex[:6]}",
                                problem="p", proposed_change="c", status="APPROVED", target_scope="platform", importance=60)
        db.add(p)
        db.flush()
        role = ("scout", "architect", "qa")[i % 3]
        tickets.create(db, proposal_id=p.id, title=p.title, fields={"objective_id": "O3", "metric_id": "bench_holdout_pass_at_1", "expected_effect": 0.01 * (i + 1),
                       "direction": "up", "proof": [f"tests/test_textutil.py::t{i}"]}, source="scout", role=role, importance=60)
    db.commit()
    plan = tickets.build_plan(db, None, _at(12), plan_max=5, per_department=2)
    assert len(plan["ranking"]) == 5
    assert max(sum(r["department"] == d for r in plan["ranking"]) for d in {r["department"] for r in plan["ranking"]}) <= 2


@pytest.mark.parametrize("env,bad", [({"SOCIETY_COMPANY_CYCLES_PER_DAY": "25"}, "CYCLES_PER_DAY"),
                                     ({"SOCIETY_COMPANY_PLAN_MAX": "2", "SOCIETY_COMPANY_PLAN_PER_DEPARTMENT": "3"}, "PLAN_MAX")])
def test_out_of_range_throughput_settings_are_refused(monkeypatch, env, bad):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SocietyConfigError, match=bad):
        SocietySettings()
