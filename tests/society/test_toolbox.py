"""The toolbox (owner 2026-11-02, "Hyra"): agents develop reusable tools, measured on later work.

design (PROPOSE_TOOL / REVISE_TOOL, model) -> experiment (deterministic arms) -> evaluate
(execution-grounded trials, deterministic law) -> reuse (adopted, with a holdout) -> upgrade.

Money invariant: nothing here touches wallets or transactions -- tools are advisory rows and
trials only READ agent_intents/agent_runs; asserted below by comparing wallet + ledger state.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from services.registry.app.models import AgentIntent
from services.registry.app.society import toolbox
from services.registry.app.society.cognition import FakeModel
from services.registry.app.society.config import SocietyConfigError, validate_settings
from services.registry.app.society.engineering import build_engine
from services.registry.app.society.events import EventType, emit_event, utcnow
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker

from .conftest import auth

STEPS = "1. read the failing test first. 2. patch with exact-text replacements only. 3. rerun only that test, then the file."


@pytest.fixture
def society(db, code_settings, grants_with_no_cooldown):
    report = seed_society(db)
    grants_with_no_cooldown()
    return report, dataclasses.replace(code_settings, toolbox_enabled=True, toolbox_min_trials=4)


def _runs(db, report, n=2, role="builder"):
    ids = [uuid.uuid4() for _ in range(n)]
    for i in ids:
        seed = emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT_IDLE, payload={}, actor_type="system", idempotency_key=f"seed:{uuid.uuid4()}")
        db.execute(text("INSERT INTO agent_runs (id, agent_id, event_id, correlation_id, role, status, completed_at) VALUES (:i, :a, :e, :e, :r, 'completed', NOW())"),
                   {"i": i, "a": report.agents[role], "e": seed.id, "r": role})
    db.commit()
    return ids


def _propose(db, report, name="red-test-first-patch", roles=("builder",), occurrences=None, **kw):
    args = dict(agent_id=report.agents["builder"], kind="workflow", name=name, roles=list(roles), problem="builds keep failing on the same flaky patch step",
                body=STEPS, occurrences=occurrences if occurrences is not None else _runs(db, report), now=utcnow())
    args.update(kw)
    made = toolbox.propose(db, **args)
    db.commit()
    return made


def _wallets(db):
    return db.execute(text("SELECT COUNT(*), COALESCE(SUM(balance_usdc), 0), COALESCE(SUM(balance_credits), 0) FROM wallets")).one(), \
        db.execute(text("SELECT COUNT(*) FROM transactions")).scalar()


# ── the law (pure) ──────────────────────────────────────────────────────


def _s(tn, tw, cn, cw, tc="0.01", cc="0.01"):
    return {"treatment": {"n": tn, "rate": tw / tn if tn else 0.0, "cost": Decimal(tc)}, "control": {"n": cn, "rate": cw / cn if cn else 0.0, "cost": Decimal(cc)}}


def test_the_law_adopts_only_a_measured_lift_at_bounded_cost_and_retires_harm_and_noise():
    assert toolbox.judge(_s(8, 8, 8, 4), status="experimental", min_trials=8)[0] == "adopted"
    assert toolbox.judge(_s(7, 7, 8, 0), status="experimental", min_trials=8) is None, "too few trials in one arm: keep measuring"
    assert toolbox.judge(_s(8, 8, 8, 4, tc="0.10", cc="0.01"), status="experimental", min_trials=8) is None, "a lift that costs 10x is not adopted"
    assert toolbox.judge(_s(8, 2, 8, 6), status="experimental", min_trials=8)[0] == "retired"
    assert toolbox.judge(_s(8, 5, 8, 5), status="experimental", min_trials=8) is None
    verdict = toolbox.judge(_s(30, 15, 30, 15), status="experimental", min_trials=8)
    assert verdict[0] == "retired" and verdict[1].startswith("inconclusive")
    assert toolbox.judge(_s(8, 2, 8, 7), status="adopted", min_trials=8)[1].startswith("regressed")
    assert toolbox.judge(_s(8, 8, 8, 2), status="adopted", min_trials=8) is None, "an adopted tool is never re-adopted or promoted further"


def test_config_bounds_the_trial_count(code_settings):
    with pytest.raises(SocietyConfigError, match="SOCIETY_TOOLBOX_MIN_TRIALS"):
        dataclasses.replace(code_settings, toolbox_min_trials=2)
    assert not any("TOOLBOX" in p for p in validate_settings(dataclasses.replace(code_settings, toolbox_min_trials=8)))


# ── design: what a tool must be ─────────────────────────────────────────


def test_a_tool_needs_a_recurring_problem_a_unique_name_and_a_free_slot(db, society):
    report, _ = society
    with pytest.raises(toolbox.ToolRefused, match="recurring"):
        _propose(db, report, occurrences=[uuid.uuid4(), uuid.uuid4()])
    one = _runs(db, report, 1)
    with pytest.raises(toolbox.ToolRefused, match="recurring"):
        _propose(db, report, occurrences=one + one)
    made = _propose(db, report, name="Red Test First  Patch")
    assert made["name"] == "red-test-first-patch" and made["version"] == 1
    with pytest.raises(toolbox.ToolRefused, match="REVISE_TOOL"):
        _propose(db, report, name="red test first patch", agent_id=report.agents["qa"])
    with pytest.raises(toolbox.ToolRefused, match="roles"):
        _propose(db, report, name="other-tool", roles=("ceo",), agent_id=report.agents["qa"])


def test_a_toolkit_only_points_at_code_already_on_main(db, society):
    report, settings = society
    with pytest.raises(toolbox.ToolRefused, match="not found|not a file"):
        _propose(db, report, name="textutil-kit", kind="toolkit", toolkit_paths=["app/missing.py"], repo_root=settings.repo_root)
    with pytest.raises(toolbox.ToolRefused, match="refused"):
        _propose(db, report, name="textutil-kit", kind="toolkit", toolkit_paths=["../../etc/passwd"], repo_root=settings.repo_root)
    made = _propose(db, report, name="textutil-kit", kind="toolkit", toolkit_paths=["app/textutil.py"], repo_root=settings.repo_root)
    assert toolbox.tool_texts(db, [made["tool_id"]])[0]["toolkit_paths"] == ["app/textutil.py"]


def test_caps_per_agent_per_day_and_on_the_experiment_portfolio(db, society):
    report, _ = society
    for i in range(toolbox.PER_AGENT_PER_DAY):
        _propose(db, report, name=f"tool-{i}")
    with pytest.raises(toolbox.ToolRefused, match="today"):
        _propose(db, report, name="tool-x")
    for i in range(toolbox.MAX_EXPERIMENTAL - toolbox.PER_AGENT_PER_DAY):
        _propose(db, report, name=f"qa-tool-{i}", agent_id=report.agents[["qa", "security", "scout"][i]])
    with pytest.raises(toolbox.ToolRefused, match="portfolio is full"):
        _propose(db, report, name="one-too-many", agent_id=report.agents["architect"])


def test_tool_text_is_redacted_before_it_is_stored(db, society):
    report, _ = society
    made = _propose(db, report, body=STEPS + " use key sk-ant-api03-" + "A" * 80)
    stored = db.execute(text("SELECT body FROM society_tools WHERE id = :i"), {"i": made["tool_id"]}).scalar()
    assert "sk-ant-api03-AAAA" not in stored


# ── experiment: who sees what ───────────────────────────────────────────


def test_arms_are_deterministic_per_story_and_split_experimental_tools(db, society):
    report, _ = society
    made = _propose(db, report)
    stories = [uuid.uuid4() for _ in range(200)]
    arms = [toolbox.assign(db, role="builder", correlation_id=c)[0]["arm"] for c in stories]
    assert arms == [toolbox.assign(db, role="builder", correlation_id=c)[0]["arm"] for c in stories], "same story, same arm"
    assert 60 <= arms.count("treatment") <= 140
    assert toolbox.assign(db, role="scout", correlation_id=stories[0]) == [], "only the tool's roles are measured"
    shown = [c for c, a in zip(stories, arms) if a == "treatment"][0]
    hidden = [c for c, a in zip(stories, arms) if a == "control"][0]
    block, trials = toolbox.context_view(db, role="builder", correlation_id=shown, now=utcnow())
    assert [t["name"] for t in block["tools"]] == ["red-test-first-patch"] and trials == [{"id": made["tool_id"], "arm": "treatment"}]
    block, trials = toolbox.context_view(db, role="builder", correlation_id=hidden, now=utcnow())
    assert block["tools"] == [] and trials == [{"id": made["tool_id"], "arm": "control"}]
    assert block["library"][0]["name"] == "red-test-first-patch" and "steps" not in block["library"][0], "the library is structural"


def test_a_revision_competes_with_the_version_it_revises(db, society):
    report, _ = society
    v1 = _propose(db, report)
    db.execute(text("UPDATE society_tools SET status = 'adopted', decided_at = NOW() WHERE id = :i"), {"i": v1["tool_id"]})
    v2 = _propose(db, report, revises="red-test-first-patch", body=STEPS + " 4. stop after two red reruns and decline.")
    assert v2["version"] == 2 and v2["parent_id"] == v1["tool_id"]
    with pytest.raises(toolbox.ToolRefused, match="already has an experimental"):
        _propose(db, report, revises="red-test-first-patch", agent_id=report.agents["qa"])
    seen = set()
    for _ in range(60):
        block, trials = toolbox.context_view(db, role="builder", correlation_id=uuid.uuid4(), now=utcnow())
        assert [t["id"] for t in trials] == [v2["tool_id"]], "only the challenger is measured while it is on trial"
        assert [t["version"] for t in block["tools"]] == ([2] if trials[0]["arm"] == "treatment" else [1])
        seen.add(trials[0]["arm"])
    assert seen == {"treatment", "control"}


def test_at_most_max_shown_tools_reach_one_run(db, society):
    report, _ = society
    for i, who in enumerate(("builder", "qa", "security", "scout", "architect")):
        _propose(db, report, name=f"kit-{i}", agent_id=report.agents[who])
    db.execute(text("UPDATE society_tools SET status = 'adopted', decided_at = NOW()"))
    db.commit()
    for _ in range(20):
        block, trials = toolbox.context_view(db, role="builder", correlation_id=uuid.uuid4(), now=utcnow())
        assert len(block["tools"]) <= toolbox.MAX_SHOWN and sum(t["arm"] == "treatment" for t in trials) == len(block["tools"])


# ── the loop end to end: scripted model, real worker, real executor ─────


def test_the_society_designs_a_tool_measures_it_on_later_runs_and_adopts_it(db, SessionLocal, society):
    """One Scout run writes the tool; later Scout stories are split by arm. Runs that SAW the tool
    take an intent that executes, runs that did not take one that fails: the execution record --
    never the model's word -- makes the toolbox adopt it."""
    report, settings = society
    before = _wallets(db)
    occurrences = [str(i) for i in _runs(db, report, 2, role="scout")]
    state = {"proposed": False}

    def decide(ctx):
        if not state["proposed"]:
            state["proposed"] = True
            return {"decision_summary": "write the method down once", "sleep_for_seconds": 1, "intents": [
                {"type": "PROPOSE_TOOL", "payload": {"kind": "checklist", "name": "evidence-before-proposal", "roles": ["scout"],
                                                      "problem": "proposals refused for missing evidence, again", "steps": STEPS,
                                                      "occurrences": occurrences}}]}
        if ctx.toolbox.get("tools"):
            intent = {"type": "SEND_MESSAGE", "payload": {"title": "observed", "content": "signal checked against the checklist"}}
        else:  # refused by the executor: not a key result of an active objective
            intent = {"type": "PROPOSE_OWNER_ACTION", "payload": {"title": "post something somewhere", "reason": "a public post in the owner's name",
                                                                   "expected_effect": "visitors arrive", "metric_id": "revenue_usd_30d"}}
        return {"decision_summary": "work", "sleep_for_seconds": 1, "intents": [intent]}

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-tools", telemetry_enabled=False)
    worker.routing = {EventType.SOCIETY_HEARTBEAT: ["scout"]}
    for _ in range(40):
        emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={"bucket": uuid.uuid4().hex}, actor_type="system", idempotency_key=f"hb:{uuid.uuid4()}")
        db.commit()
        asyncio.run(worker.run_until_idle(max_cycles=3))
        db.expire_all()
        status = db.execute(text("SELECT status FROM society_tools WHERE name = 'evidence-before-proposal'")).scalar()
        if status != "experimental":
            break
    row = db.execute(text("SELECT status, status_reason FROM society_tools WHERE name = 'evidence-before-proposal'")).mappings().one()
    assert row["status"] == "adopted", row
    arms = dict(db.execute(text("SELECT arm, COUNT(*) FILTER (WHERE success) || '/' || COUNT(*) FROM society_tool_trials GROUP BY arm")).all())
    assert arms["treatment"].split("/")[0] == arms["treatment"].split("/")[1] and arms["control"].startswith("0/"), arms
    first = db.query(AgentIntent).filter(AgentIntent.intent_type == "PROPOSE_TOOL").one()
    assert first.execution_status.value == "executed"
    assert db.execute(text("SELECT COUNT(*) FROM society_events WHERE event_type = :t"), {"t": EventType.TOOL_PROPOSED}).scalar() == 1
    assert _wallets(db) == before, "the toolbox never touches money"


def test_with_the_flag_off_nothing_is_shown_measured_or_proposed(db, SessionLocal, society):
    report, settings = society
    settings = dataclasses.replace(settings, toolbox_enabled=False)
    _propose(db, report, roles=("scout",))
    occurrences = [str(i) for i in _runs(db, report, 2, role="scout")]
    seen = []

    def decide(ctx):
        seen.append(ctx.toolbox)
        return {"decision_summary": "try", "sleep_for_seconds": 1, "intents": [
            {"type": "PROPOSE_TOOL", "payload": {"kind": "prompt", "name": "another-tool", "roles": ["scout"], "problem": "a recurring problem here",
                                                  "steps": STEPS, "occurrences": occurrences}}]}

    worker = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(decide), worker_id="w-off", telemetry_enabled=False)
    worker.routing = {EventType.SOCIETY_HEARTBEAT: ["scout"]}
    emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={"bucket": 1}, actor_type="system", idempotency_key=f"hb:{uuid.uuid4()}")
    db.commit()
    asyncio.run(worker.run_until_idle(max_cycles=3))
    db.expire_all()
    assert seen and all(s == {} for s in seen)
    row = db.query(AgentIntent).filter(AgentIntent.intent_type == "PROPOSE_TOOL").one()
    assert row.execution_status.value == "denied" and "SOCIETY_TOOLBOX_ENABLED" in (row.policy_reason or "")
    assert db.execute(text("SELECT COUNT(*) FROM society_tool_trials")).scalar() == 0


def test_an_adopted_tool_that_falls_behind_its_holdout_is_retired_and_a_revision_supersedes(db, society):
    report, _ = society
    v1 = _propose(db, report)
    adopted_at = utcnow() - timedelta(hours=1)
    db.execute(text("UPDATE society_tools SET status = 'adopted', decided_at = :t WHERE id = :i"), {"i": v1["tool_id"], "t": adopted_at})
    runs = _runs(db, report, 16)
    for k, r in enumerate(runs):
        arm, ok = ("treatment", k % 4 == 0) if k < 8 else ("control", True)
        db.execute(text("INSERT INTO society_tool_trials (id, tool_id, run_id, role, arm, success) VALUES (:i, :t, :r, 'builder', :a, :s)"),
                   {"i": uuid.uuid4(), "t": v1["tool_id"], "r": r, "a": arm, "s": ok})
    assert toolbox.evaluate(db, v1["tool_id"], min_trials=8, now=utcnow()) == "retired"
    db.commit()
    v1b = _propose(db, report, name="second-tool")
    db.execute(text("UPDATE society_tools SET status = 'adopted', decided_at = NOW() WHERE id = :i"), {"i": v1b["tool_id"]})
    v2 = _propose(db, report, revises="second-tool")
    for k, r in enumerate(_runs(db, report, 16)):
        arm = "treatment" if k < 8 else "control"
        db.execute(text("INSERT INTO society_tool_trials (id, tool_id, run_id, role, arm, success) VALUES (:i, :t, :r, 'builder', :a, :s)"),
                   {"i": uuid.uuid4(), "t": v2["tool_id"], "r": r, "a": arm, "s": arm == "treatment"})
    assert toolbox.evaluate(db, v2["tool_id"], min_trials=8, now=utcnow()) == "adopted"
    assert db.execute(text("SELECT status FROM society_tools WHERE id = :i"), {"i": v1b["tool_id"]}).scalar() == "superseded"


def test_the_builder_harness_reads_its_storys_tools_as_advisory_text(db, society, tmp_path):
    report, _ = society
    made = _propose(db, report)
    tools = toolbox.builder_tools(db, {"toolbox_trials": [{"id": made["tool_id"], "arm": "treatment"}]})
    assert [t["name"] for t in tools] == ["red-test-first-patch"]
    assert toolbox.builder_tools(db, {"toolbox_trials": [{"id": made["tool_id"], "arm": "control"}]}) == []
    ws = type("WS", (), {"path": tmp_path, "base_sha": "abc"})()
    ms = type("MS", (), {"builder_max_read_calls": 4})()
    out = build_engine.spec_input({"files_allowed": [], "acceptance_tests": []}, title="t", ws=ws, ms=ms, toolbox=tools)
    assert out["toolbox"]["trust"] == "society_toolbox" and out["toolbox"]["tools"][0]["steps"] == STEPS
    assert "toolbox" not in build_engine.spec_input({"files_allowed": [], "acceptance_tests": []}, title="t", ws=ws, ms=ms)


def test_only_an_operator_reads_the_toolbox_and_can_retire_a_tool(db, society, api_client, user_token):
    report, _ = society
    made = _propose(db, report)
    _, plain = user_token(None)
    _, op = user_token("operator")
    assert api_client.get("/v1/society/company/toolbox", headers=auth(plain)).status_code == 403
    view = api_client.get("/v1/society/company/toolbox", headers=auth(op)).json()
    assert view["counts"]["experimental"] == 1 and view["tools"][0]["trials"]["treatment"]["n"] == 0
    assert api_client.post(f"/v1/society/company/toolbox/{made['tool_id']}/retire", headers=auth(plain), json={"reason": "bad"}).status_code == 403
    r = api_client.post(f"/v1/society/company/toolbox/{made['tool_id']}/retire", headers=auth(op), json={"reason": "misleading steps"})
    assert r.status_code == 200 and r.json()["status"] == "retired"
    assert api_client.post(f"/v1/society/company/toolbox/{made['tool_id']}/retire", headers=auth(op), json={"reason": "again"}).status_code == 404
