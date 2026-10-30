"""G1v PR B -- the standing work generator and the funnel by source (owner 2026-10-29).

"The company must never run out of work": each company cycle tops the proposed tickets of every
ACTIVE objective up to SOCIETY_WORK_GENERATOR_MIN_BACKLOG (5), at most
SOCIETY_WORK_GENERATOR_MAX_PER_CYCLE (10) new ones, deterministic, deduplicated by title/proof, each
with a proof -- from the product surface, the directive's themes, off-target key results, docs/SDK
gaps, failing tests, TODOs and rejected candidates (a rescope). The funnel counts tickets by source,
per-role idle % and queue, $/hour and spend vs budget; the validator checks the live proof."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import pathlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from services.registry.app.models import AgentRun, CodeCandidate
from services.registry.app.society import company as company_mod
from services.registry.app.society import directives, funnel, tickets, work_generator
from services.registry.app.society.events import EventType, emit_event, utcnow

from .test_company_tickets import _activate, company as company_fixture  # noqa: F401 -- fixture

ROOT = pathlib.Path(__file__).resolve().parents[2]
company = company_fixture


def _repo(tmp_path):
    """A tiny product tree: two SDK methods (one undocumented, one without a docstring), an example without a README."""
    (tmp_path / "sdk/python/agentnet").mkdir(parents=True)
    (tmp_path / "sdk/python/agentnet/client.py").write_text(
        'class AgentNetClient:\n    def search_agents(self):\n        """Find agents."""\n\n    def hire(self):\n        pass\n\n    def _private(self):\n        pass\n')
    (tmp_path / "sdk/python/README.md").write_text("# SDK\n\n`search_agents()` finds agents.\n")
    (tmp_path / "examples/echo_agent").mkdir(parents=True)
    (tmp_path / "examples/echo_agent/agent.py").write_text("print('echo')\n")
    (tmp_path / "examples/weather_agent").mkdir(parents=True)
    (tmp_path / "examples/weather_agent/README.md").write_text("# weather\n")
    return str(tmp_path)


def _gen(settings, tmp_path, **kw):
    return dataclasses.replace(settings, work_generator_enabled=True, work_generator_min_backlog=kw.get("floor", 5),
                               work_generator_max_per_cycle=kw.get("cap", 10), repo_root=_repo(tmp_path))


def _rows(db):
    return db.execute(text("SELECT title, source, origin, objective_id, metric_id, proof, status FROM society_tickets ORDER BY created_at, title")).mappings().all()


# ── the generator ───────────────────────────────────────────────────────


def test_the_directive_becomes_page_tickets_and_the_backlog_is_topped_up_once(db, company, tmp_path):
    report, settings = company
    s = _gen(settings, tmp_path)
    _activate(db, "O5")
    owner_words = "Nâng cấp giao diện và tăng traffic cho trang chủ"
    d = directives.set_directive(db, text_=owner_words, objective_ids=["O5"], user_id=None)
    out = work_generator.generate(db, s, utcnow())
    db.commit()
    rows = _rows(db)
    assert len(out["created"]) == 5 and len(rows) == 5, "an active objective's backlog is topped up to the floor, no further"
    assert {r["source"] for r in rows} == {"generator"} and {r["origin"] for r in rows} == {"directive"}
    assert {r["objective_id"] for r in rows} == {"O5"} and {r["metric_id"] for r in rows} == {"verified_signups_7d"}
    assert all(f"(directive {d['id'][:8]})" in r["title"] and owner_words.lower() not in r["title"].lower() for r in rows), "system-written titles: the owner's text is matched, never copied"
    assert all(any(p.startswith("surface:") for p in r["proof"]) and any(p.startswith("directive:") for p in r["proof"]) for r in rows), "each ticket carries its proof"
    assert all(tickets.proof_tests(dict(r)) == [] for r in rows), "generator proofs are evidence, never pytest node ids"

    assert work_generator.generate(db, s, utcnow())["created"] == [], "a full backlog gets nothing more"
    db.execute(text("UPDATE society_tickets SET status = 'closed'"))
    db.commit()
    again = work_generator.generate(db, s, utcnow())
    db.commit()
    titles = [r["title"] for r in _rows(db)]
    assert len(again["created"]) == 5 and again["duplicates"] >= 5 and len(set(titles)) == len(titles) == 10, "duplicate titles/proofs are refused for 7 days"


def test_at_most_max_per_cycle_tickets_across_active_objectives(db, company, tmp_path):
    report, settings = company
    s = _gen(settings, tmp_path, floor=8, cap=10)
    _activate(db, "O5")
    _activate(db, "O2")
    out = work_generator.generate(db, s, utcnow())
    db.commit()
    per = {r["objective_id"] for r in _rows(db)}
    assert len(out["created"]) == 10 and per == {"O2", "O5"}, "the cap bounds a cycle; the larger deficit is served first"
    assert set(out["by_origin"]) <= set(work_generator.ORIGINS) and "kr_lever" in out["by_origin"]


def test_off_target_key_results_and_docs_gaps_supply_work_every_cycle(db, company, tmp_path):
    report, settings = company
    s = _gen(settings, tmp_path, floor=20, cap=20)
    _activate(db, "O2")
    out = work_generator.generate(db, s, utcnow())
    db.commit()
    rows = _rows(db)
    lever = [r for r in rows if r["origin"] == "kr_lever"]
    assert {r["metric_id"] for r in lever} == {"escrow_tasks_completed_7d", "a2a_agents_live_card"}, "one ticket per product idea of each off-target KR"
    assert all(r["proof"][0] == f"probe:{r['metric_id']}" for r in lever), "the KR probe judges it (work_packet.lever_packet, outcomes)"
    docs = sorted(r["title"] for r in rows if r["origin"] == "docs_sdk")
    assert docs == ["Example echo_agent: a README to run it in five minutes", "SDK README: document hire", "SDK: docstrings for hire"]
    assert out["created"] and not any(r["origin"] == "directive" for r in rows), "no directive, no directive tickets"


def _observe(db, ref, ok=False, **payload):
    p = {"ref": ref, **payload}
    db.execute(text("INSERT INTO maintenance_observations (sli, target, source, collector_version, trust_class, ok, digest, payload) "
                    "VALUES ('public_surface', 'production', 'public_surface_monitor', 't', 'trusted_probe', :ok, :d, CAST(:p AS JSONB))"),
               {"ok": ok, "d": hashlib.sha256(json.dumps(p).encode()).hexdigest(), "p": json.dumps(p)})


def test_a_failing_page_no_incident_covers_becomes_a_surface_ticket(db, company, tmp_path):
    report, settings = company
    s = _gen(settings, tmp_path)
    _activate(db, "O5")
    contract = __import__("services.registry.app.society.surface", fromlist=["load_contract"]).load_contract()
    _observe(db, "landing", failure="marker_missing", severity="minor")
    _observe(db, "register", failure="slow", severity="minor")
    _observe(db, "network", ok=True)
    db.execute(text("INSERT INTO maintenance_incidents (id, fingerprint, incident_class, priority, severity, source, target, desired_state_ref, "
                    "first_observed_at, last_observed_at, current_evidence_digest) VALUES (:id, 'fp', 'UI_RENDERING', 'P2', 'major', 'public_surface_monitor', "
                    "'production', 'register', NOW(), NOW(), 'd')"), {"id": uuid.uuid4()})
    db.commit()
    items = work_generator.surface_items(db, contract, s.repo_root, utcnow())
    assert [(i["title"], i["proof"]) for i in items] == [("Fix /landing: marker_missing", ["surface:landing"])], \
        "a healthy page is no work; an OPEN maintenance incident owns its page (never the innovation portfolio)"


def test_a_rejected_candidate_gets_one_rescoped_follow_up(db, company, tmp_path):
    report, settings = company
    s = _gen(settings, tmp_path, floor=1, cap=5)
    _activate(db, "O5")
    made = []
    for reason_payload in ({"declined": True, "reason_code": "spec_outside_files_allowed"}, {}):
        cand = CodeCandidate(id=uuid.uuid4(), correlation_id=uuid.uuid4(), requested_by_agent_id=report.agents["architect"], title="r", status="rejected",
                             spec={"kind": "code", "files_allowed": ["src/a.py"]}, qa_report={}, security_report={},
                             error=None if reason_payload else "closed_by_owner: pull request closed without merge")
        db.add(cand)
        db.flush()
        emit_event(db, event_type=EventType.CODE_CANDIDATE_REJECTED, payload={"candidate_id": str(cand.id), **reason_payload}, actor_type="system",
                   subject_type="code_candidate", subject_id=cand.id, idempotency_key=f"t-rej-{cand.id}")
        pid = uuid.uuid4()
        db.execute(text("INSERT INTO improvement_proposals (id, source, title, problem, proposed_change, status, target_scope, importance) "
                        "VALUES (:id, 'audit', 't', 'p', 'c', 'APPROVED', 'platform', 60)"), {"id": pid})
        tid = tickets.create(db, proposal_id=pid, title=f"Signup page ticket {len(made)}", source="scout", role="scout", importance=60,
                             fields={"objective_id": "O5", "metric_id": "verified_signups_7d", "expected_effect": 3, "direction": "up", "proof": ["probe:verified_signups_7d"]})
        tickets.set_status(db, tid, "closed", candidate_id=cand.id)
        made.append(cand)
    db.commit()
    out = work_generator.generate(db, s, utcnow())
    db.commit()
    rows = [r for r in _rows(db) if r["source"] == "rescope"]
    assert len(rows) == 1 and rows[0]["title"] == "Rescope: Signup page ticket 0", "a candidate the owner closed is not pushed again"
    assert rows[0]["proof"] == ["probe:verified_signups_7d", f"rescope:{str(made[0].id)[:8]}"] and rows[0]["origin"] == "rescope"
    assert out["by_origin"].get("rescope") == 1
    assert work_generator.generate(db, dataclasses.replace(s, work_generator_min_backlog=5), utcnow())["by_origin"].get("rescope") is None, "once"


def test_failing_ci_tests_and_todos_are_bounded_read_only_sources(db, company, tmp_path):
    report, settings = company
    s = _gen(settings, tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_x.py").write_text("def test_a():\n    assert True\n")
    emit_event(db, event_type=EventType.PROMOTION_CI_FAILED, payload={"detail": "FAILED tests/test_x.py::test_a - assert 0, FAILED tests/gone.py::test_b"},
               actor_type="system", idempotency_key="t-ci-failed")
    (tmp_path / "examples/agent.py").write_text("x = 1  # TODO: retry on 429\n")
    db.commit()
    tests_ = work_generator.test_items(db, s.repo_root, utcnow())
    assert [i["proof"] for i in tests_] == [["tests/test_x.py::test_a"]], "a node id still on base is the acceptance proof; a vanished file is not"
    assert [i["title"] for i in work_generator.todo_items(s.repo_root)] == ["Resolve TODO/FIXME in examples/agent.py"]


def test_directive_themes_are_a_fixed_keyword_map():
    assert work_generator.directive_themes("Nâng cấp giao diện, tăng traffic") == ["ui", "traffic"]
    assert work_generator.directive_themes("more sign ups and A2A agents") == ["signup", "agents"]
    assert work_generator.directive_themes("make money") == ["ui"], "a directive with no theme still gets its pages looked at"
    assert work_generator.directive_themes("build a guide") == ["ui"], "'ui' is a word, not a substring of build/guide"


def test_the_cycle_runs_the_generator_only_when_enabled(db, company, tmp_path):
    report, settings = company
    _activate(db, "O5")
    off = company_mod.start_cycle(db, dataclasses.replace(settings, repo_root=_repo(tmp_path)), trigger="operator")
    assert "tickets_generated" not in off.evidence
    on = company_mod.start_cycle(db, _gen(settings, tmp_path / "b"), trigger="operator")
    assert on.evidence["tickets_generated"] == 5 and sum(on.evidence["generated_by_origin"].values()) == 5


def test_generator_flags_default_off_and_are_documented():
    from services.registry.app.society.config import SocietyConfigError, SocietySettings, validate_settings

    s = SocietySettings()
    assert s.work_generator_enabled is False and s.work_generator_min_backlog == 5 and s.work_generator_max_per_cycle == 10
    env = (ROOT / ".env.example").read_text()
    assert all(k in env for k in ("SOCIETY_WORK_GENERATOR_ENABLED=false", "SOCIETY_WORK_GENERATOR_MIN_BACKLOG=5", "SOCIETY_WORK_GENERATOR_MAX_PER_CYCLE=10"))
    with pytest.raises(SocietyConfigError, match="0..20"):
        validate_settings(dataclasses.replace(s, work_generator_max_per_cycle=50))


# ── the funnel by source, idle %, $/hour ────────────────────────────────


def test_the_funnel_counts_tickets_by_source_and_roles_idle(db, company):
    report, settings = company
    now = utcnow()
    for i, (src, origin, proof) in enumerate((("owner_story", "directive", ["probe:x"]), ("owner_story", "message", ["probe:x"]), ("generator", "kr_lever", ["probe:x"]),
                                              ("generator", "surface", ["surface:landing"]), ("rescope", "rescope", ["rescope:1"]),
                                              ("backlog", None, ["bench:t1"]), ("owner", None, []))):
        pid = uuid.uuid4()
        db.execute(text("INSERT INTO improvement_proposals (id, source, title, problem, proposed_change, status, target_scope, importance) "
                        "VALUES (:id, 'audit', 't', 'p', 'c', 'PROPOSED', 'platform', 60)"), {"id": pid})
        db.execute(text("INSERT INTO society_tickets (id, proposal_id, title, objective_id, metric_id, expected_effect, direction, proof, source, origin) "
                        "VALUES (:id, :p, :t, 'O5', 'verified_signups_7d', 1, 'up', CAST(:proof AS JSONB), :s, :o)"),
                   {"id": uuid.uuid4(), "p": pid, "t": f"t{i}", "proof": json.dumps(proof), "s": src, "o": origin})
    emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={"daily_model_budget_usd": 80.0}, actor_type="system", idempotency_key="t-hb-budget")
    for n, (status, err, cost) in enumerate((("completed", None, 2.0), ("skipped", "idle: nothing to act on", 0), ("skipped", "idle: nothing to act on", 0),
                                             ("skipped", "idle: x", 0), ("queued", None, 0))):
        ev = emit_event(db, event_type=EventType.SOCIETY_HEARTBEAT, payload={}, actor_type="system", idempotency_key=f"t-hb-run-{n}")
        db.add(AgentRun(id=uuid.uuid4(), agent_id=report.agents["builder"], event_id=ev.id, correlation_id=ev.correlation_id, role="builder", status=status,
                        error=err, cost_usd=cost, completed_at=now if status != "queued" else None))
    db.commit()
    assert funnel.worker_budget(db, 2.0) == 80.0, "the WORKER's budget, from its heartbeat"
    f = funnel.funnel(db, now + timedelta(seconds=1), hours=12, daily_budget_usd=80.0)
    assert f["tickets_by_source"] == {"directive": 1, "owner": 2, "kr_gap": 1, "generator": 1, "rescope": 1, "bench": 1}
    b = f["roles"]["builder"]
    assert (b["worked"], b["idle"], b["idle_pct"], b["queued"]) == (1, 3, 75.0, 1)
    assert f["budget_usd"] == 40.0 and f["spend_pct_of_budget"] == 5.0 and f["usd_per_hour"] == round(2.0 / 12, 4)
    line = funnel.summary(f)
    assert "of $40.00" in line and "directive 1" in line and "idle builder 75.0%" in line


def _p5():
    spec = importlib.util.spec_from_file_location("phase5_live_g1v_b", ROOT / "deploy" / "railway" / "phase5_live.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_validator_checks_the_live_proof_from_numbers(monkeypatch):
    p5 = _p5()
    assert p5.parse_plan("funnel:12:proof") == [("funnel", ["12", "proof"])]
    with pytest.raises(ValueError):
        p5.parse_plan("funnel:12:claims")
    body = {"spend_usd": 31.5, "tickets_created": 22, "designs_go": 8, "builds_delivered": 4, "prs_opened": 3, "by_2h": [{}], "stages": [],
            "roles": {"builder": {"idle_pct": 25.0, "queued": 0}}, "tickets_by_source": {"generator": 12}}
    monkeypatch.setattr(p5, "api", lambda method, url, token, body_=None, timeout=30.0, _b=body: (200, _b))
    checks = []

    class Out:
        def check(self, group, code, ok, detail=""):
            checks.append((code, bool(ok)))
            return bool(ok)

        def info(self, group, detail):
            pass

        def json(self, group, key, obj):
            pass

    p5.step_funnel(Out(), "https://api.test", "operator-jwt", 12, proof=True)
    assert checks == [("F00", True), ("F01", True), ("G01", True), ("G02", True), ("G03", False), ("G04", True), ("G05", True), ("G06", True)]
