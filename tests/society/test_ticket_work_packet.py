"""A bench ticket becomes designable: the work packet (evidence + harness target), the
harness-only spec, and QA's bench proof on the candidate harness (work_packet.py,
engineering/bench_proof.py). No model is called here."""

from __future__ import annotations

import json
import pathlib
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from sqlalchemy import text

from services.registry.app.models import AgentCapabilityGrant, CodeCandidate, ImprovementProposal
from services.registry.app.society import tickets, work_packet
from services.registry.app.society.context import build_context
from services.registry.app.society.engineering import bench_proof, build_engine
from services.registry.app.society.engineering.qa import evaluate_candidate
from services.registry.app.society.engineering.workspace import Workspace
from services.registry.app.society.events import EventType, emit_event

from .test_company_tickets import _activate, _run, company  # noqa: F401 -- fixture

TASK = "activity-loop-final-turn"  # a real dev task (scripts/bench/tasks.json)
HARNESS = "services/registry/app/maintenance/harness.py"
DETAIL = [{"result": "turn_budget", "error_class": "turn_budget", "turns": 12, "test_runs": 0, "patches": 0, "tool_codes": ["read_budget", "read_budget"]},
          {"result": "pass", "error_class": None, "turns": 6, "test_runs": 2, "patches": 1, "tool_codes": []},
          {"result": "turn_budget", "error_class": "turn_budget", "turns": 12, "test_runs": 1, "patches": 1, "tool_codes": ["tests_failing", "read_budget"]}]


def _report(db, per_task, revision="r-main"):
    db.execute(text("INSERT INTO society_bench_reports (id, revision, judge_revision, path, model, repeat, summary, per_task) VALUES (:i,:r,:r,'maintenance','m',3,'{}',:p)"),
               {"i": str(uuid.uuid4()), "r": revision, "p": json.dumps(per_task)})
    db.commit()


def _bench_ticket(db, report, task=TASK):
    p = ImprovementProposal(id=uuid.uuid4(), source="audit", title=f"Builder harness: deliver dev task {task} (1/3 runs)", problem="bench", proposed_change="bench",
                            status="PROPOSED", target_scope="platform", importance=60)
    db.add(p)
    db.flush()
    tid = tickets.create(db, proposal_id=p.id, title=p.title, fields={"objective_id": "O3", "metric_id": "bench_holdout_pass_at_1", "expected_effect": 0.0111,
                                                                     "direction": "up", "proof": [f"bench:{task}"]}, source="owner", role="governor", importance=60)
    tickets._approved(db, tickets.for_proposal(db, p.id), None)  # as decide_plan / create_owner_ticket do
    db.commit()
    return p, tid


def test_the_packet_carries_the_failure_evidence_and_the_harness_target_never_a_holdout(db):
    _report(db, {TASK: {"split": "dev", "delivered": 1, "runs": ["turn_budget", "pass", "turn_budget"], "detail": DETAIL},
                 "bare-task": {"split": "dev", "delivered": 0, "runs": ["wrong_file"] * 3},
                 "secret-h": {"split": "holdout", "delivered": 0, "runs": ["tests_fail"] * 3}})
    pk = work_packet.bench_packet(db, TASK)
    assert pk["evidence"]["delivered"] == "1/3" and pk["evidence"]["failure_class"] == "turn_budget" and pk["evidence"]["last_tool_code"] == "read_budget"
    assert [r["turns"] for r in pk["evidence"]["runs"]] == [12, 6, 12] and pk["evidence"]["runs"][2]["last_tool_codes"] == ["tests_failing", "read_budget"]
    assert pk["target"]["file"] == work_packet.ACTIVITIES and pk["target"]["function"] == "run_activity"  # read_budget refines turn_budget
    assert pk["proof"] == {"acceptance": f"bench:{TASK}", "rule": work_packet.BENCH_PROOF_RULE, "baseline_delivered": 1, "repeat": 3}
    assert pk["task"]["text"] and pk["regression_tests"] == list(work_packet.REGRESSION_TESTS)
    bare = work_packet.bench_packet(db, "bare-task")  # an old report without per-run detail: the class still decides
    assert bare["target"]["function"] == "target_file_context" and bare["evidence"]["runs"] == []
    assert work_packet.bench_packet(db, "secret-h") is None and work_packet.bench_packet(db, "never-run") is None


def test_the_architect_designing_an_approved_ticket_sees_its_packet(db, company):
    report, settings = company
    _report(db, {TASK: {"split": "dev", "delivered": 1, "runs": ["turn_budget", "pass", "turn_budget"], "detail": DETAIL}})
    p, tid = _bench_ticket(db, report)
    ev = db.execute(text("SELECT correlation_id FROM society_events WHERE idempotency_key = :k"), {"k": f"company.ticket_approved:{tid}"}).scalar()
    assert ev is not None and tickets.for_correlation(db, ev)["id"] == tid
    grant = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.role == "architect").first()
    from services.registry.app.models import Agent, SocietyEvent

    event = db.query(SocietyEvent).filter(SocietyEvent.correlation_id == ev).first()
    ctx = build_context(db, agent=db.get(Agent, grant.agent_id), grant=grant, event=event, run=None, settings=settings)
    company_view = ctx.engineering["company"]
    assert company_view["ticket"]["proposal_id"] == str(p.id) and company_view["work_packet"]["target"]["function"] == "run_activity"
    assert "work_packet.target" in company_view["ticket_rule"]


def _bench_request(proposal, files, tests=()):
    return {"type": "REQUEST_CODE_CHANGE", "payload": {"title": "harness: final-turn pacing", "proposal_id": str(proposal.id), "spec": {
        "kind": "code", "description": "pace reads so the final turn submits", "files_allowed": list(files), "acceptance_tests": list(tests)}}}


def test_a_bench_ticket_is_a_harness_change_whose_bench_proof_is_always_an_acceptance_criterion(db, SessionLocal, company):
    report, settings = company
    _activate(db)
    p, tid = _bench_ticket(db, report)
    row = _run(db, SessionLocal, settings, "architect", [_bench_request(p, ["app/textutil.py"])])
    assert row.execution_status.value == "failed" and "changes the builder harness" in row.error
    row = _run(db, SessionLocal, settings, "architect", [_bench_request(p, [HARNESS, "app/textutil.py"])])
    assert row.execution_status.value == "failed" and "never the task's files" in row.error
    ticket_as_proposal = {**_bench_request(p, [HARNESS]), "payload": {**_bench_request(p, [HARNESS])["payload"], "proposal_id": str(tid)}}
    row = _run(db, SessionLocal, settings, "architect", [ticket_as_proposal])
    assert row.execution_status.value == "failed" and "is a TICKET id" in row.error
    row = _run(db, SessionLocal, settings, "architect", [_bench_request(p, [HARNESS, "tests/test_bench.py"], tests=[f"bench:{TASK}"])])
    assert row.execution_status.value == "executed", row.error
    cand = db.query(CodeCandidate).one()
    assert cand.spec["acceptance_tests"] == list(work_packet.REGRESSION_TESTS) + [f"bench:{TASK}"] and cand.spec["kind"] == "code"
    assert cand.spec["expected_effect"].startswith("[O3 bench_holdout_pass_at_1 up 0.0111]") and tickets.for_proposal(db, p.id)["status"] == "building"
    assert build_engine.pytest_targets(cand.spec) == list(work_packet.REGRESSION_TESTS)


def _ws(repo):
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "cand"], check=True)
    (repo / "app" / "textutil.py").write_text((repo / "app" / "textutil.py").read_text().replace('{"true", "1"}', '{"true", "1", "yes", "on", "y"}'))
    return Workspace(candidate_id=uuid.uuid4(), path=repo, branch="cand", base_sha="", repo_root=repo)


GREEN_TESTS = ["tests/acceptance/test_parse_bool_regression.py"]


def test_qa_runs_the_bench_proof_only_after_green_pytest_and_never_assumes_it(code_settings, code_repo):
    ws = _ws(code_repo)
    spec = {"kind": "code", "files_allowed": ["app/textutil.py"], "acceptance_tests": GREEN_TESTS + [f"bench:{TASK}"]}
    calls = []

    def runner(result):
        def run(task_id):
            calls.append(task_id)
            return {"task_id": task_id, "repeat": 3, "baseline_delivered": 1, **result}
        return run

    ok = evaluate_candidate(code_settings, ws, spec, ["app/textutil.py"], bench_runner=runner({"delivered": 3, "runs": ["pass"] * 3, "passed": True}))
    assert ok.passed and calls == [TASK] and ok.bench_proof[0]["delivered"] == 3
    assert "candidate 3/3 vs baseline 1/3" in next(c.detail for c in ok.checks if c.name == "bench_proof")
    bad = evaluate_candidate(code_settings, ws, spec, ["app/textutil.py"], bench_runner=runner({"delivered": 1, "runs": ["pass", "turn_budget", "turn_budget"], "passed": False}))
    assert not bad.passed and any(f.startswith("bench_proof:") for f in bad.failures)
    assert not evaluate_candidate(code_settings, ws, spec, ["app/textutil.py"]).passed  # no runner: never assumed
    calls.clear()
    red = {**spec, "acceptance_tests": ["tests/acceptance/test_parse_bool_regression.py::nope"] + [f"bench:{TASK}"]}
    assert not evaluate_candidate(code_settings, ws, red, ["app/textutil.py"], bench_runner=runner({"passed": True})).passed and calls == []


class _Provider(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Provider.seen.append((self.path, self.headers.get("Authorization"), body))
        out = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def test_the_model_relay_holds_the_key_and_the_child_never_sees_it(code_settings, monkeypatch):
    import urllib.error
    import urllib.request

    provider = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    real_key = "sk-" + "r" * 32
    try:
        with bench_proof.ModelRelay(f"http://127.0.0.1:{provider.server_address[1]}", real_key, max_requests=1) as relay:
            env = bench_proof.child_env(SimpleNamespace(repo_root="/repo", bench_proof_budget_usd="0.5"), relay, "/cand",
                                        environ={"PATH": "/bin", "SOCIETY_MODEL_API_KEY": real_key, "SOCIETY_GITHUB_TOKEN": "ghp_x", "POSTGRES_PASSWORD": "p",
                                                 "SOCIETY_MODEL_NAME": "deepseek-flash", "MAINTENANCE_BUILDER_SAMPLES": "3"})
            assert real_key not in json.dumps(env) and "SOCIETY_GITHUB_TOKEN" not in env and "POSTGRES_PASSWORD" not in env
            assert env["SOCIETY_MODEL_API_KEY"] == relay.token and env["SOCIETY_MODEL_BASE_URL"] == relay.url and env["BENCH_HARNESS_ROOT"] == "/cand"

            def post(token):
                req = urllib.request.Request(relay.url + "/chat/completions", data=b'{"m": 1}', method="POST", headers={"Authorization": f"Bearer {token}"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        return r.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            assert post("wrong") == 403 and _Provider.seen == []
            assert post(relay.token) == 200 and _Provider.seen[-1] == ("/chat/completions", f"Bearer {real_key}", b'{"m": 1}')
            assert post(relay.token) == 429  # request cap
    finally:
        provider.shutdown()


def test_run_proof_judges_the_candidate_harness_x3_against_the_baseline(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "scripts" / "bench").mkdir(parents=True)
    (repo / "scripts" / "bench" / "tasks.json").write_text(json.dumps({"tasks": [{"id": TASK, "files_allowed": ["x.py"]}]}))
    settings = SimpleNamespace(repo_root=str(repo), model_api_key="sk-" + "k" * 32, model_base_url="https://provider.example/v1", bench_proof_budget_usd="0.5",
                               bench_proof_timeout_seconds=60)
    seen = {}

    def spawn(delivered):
        def fake(argv, cwd, env, **kw):
            seen.update(argv=argv, env=env)
            out = argv[argv.index("--json-out") + 1]
            runs = ["pass"] * delivered + ["turn_budget"] * (3 - delivered)
            with open(out, "w") as f:
                json.dump({"per_task": {TASK: {"delivered": delivered, "runs": runs}}, "cost_usd": "0.021", "model": "deepseek-flash"}, f)
            return subprocess.Popen(["true"])
        return fake

    r = bench_proof.run_proof(settings, "/cand", TASK, baseline_delivered=1, spawn=spawn(2))
    assert r["passed"] and r["delivered"] == 2 and r["cost_usd"] == "0.021" and seen["env"]["BENCH_HARNESS_ROOT"] == "/cand"
    assert seen["argv"][seen["argv"].index("--only") + 1] == TASK and seen["argv"][seen["argv"].index("--repeat") + 1] == "3"
    assert settings.model_api_key not in json.dumps(seen["env"])
    assert not bench_proof.run_proof(settings, "/cand", TASK, baseline_delivered=2, spawn=spawn(2))["passed"]  # must beat the baseline
    holdout = bench_proof.run_proof(settings, "/cand", "secret-holdout", baseline_delivered=0, spawn=spawn(3))
    assert not holdout["passed"] and "not a dev task" in holdout["error"]


def test_ticket_approved_event_correlation_is_the_design_story(db, company):
    report, _ = company
    p, tid = _bench_ticket(db, report)
    ev = emit_event(db, event_type=EventType.REPO_READ_RESULT, payload={}, correlation_id=db.execute(text(
        "SELECT correlation_id FROM society_events WHERE idempotency_key = :k"), {"k": f"company.ticket_approved:{tid}"}).scalar())
    db.commit()
    assert tickets.for_correlation(db, ev.correlation_id)["proposal_id"] == p.id and tickets.for_correlation(db, uuid.uuid4()) is None


def test_the_module_imports_inside_the_registry_image_layout():
    """Regression (staging 2026-10-11): /app/app/society/work_packet.py has 4 parents, so a
    module-level parents[4] raised IndexError on import -- GET /company/tickets returned 500."""
    image = work_packet.default_tasks_file(pathlib.Path("/app/app/society/work_packet.py"))
    assert image == pathlib.Path("/") / work_packet.TASKS_REL and work_packet.dev_task(TASK, image) is None
    assert work_packet.default_tasks_file().exists()  # a repository checkout finds the real list
