"""Live-model canary: credential safety, preflight, in-process and HTTP-driven canaries.

NO FAKE AUTONOMY: every path here proves that scripted/fake providers are
refused and that a report can never carry the credential."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess

import pytest

from services.registry.app.models import AgentCapabilityGrant, IntentApproval, User
from services.registry.app.society import canary as cn
from services.registry.app.society.seed import seed_society

TEST_KEY = "sk-test-" + "a" * 40


def _live(settings, **over):
    base = dict(model_provider="openai_compatible", model_base_url="https://model.invalid/v1", model_api_key=TEST_KEY, model_name="canary-test-model")
    base.update(over)
    return dataclasses.replace(settings, **base)


def _ok_transport(seen):
    async def transport(payload):
        seen.append(payload)
        return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}

    return transport


def _decision_transport(calls):
    """A fake *transport* (not a fake model): the runtime still runs the real
    OpenAICompatibleModel request/parse path. Scout proposes an improvement;
    every other role observes."""

    async def transport(payload):
        calls.append(payload)
        if payload.get("max_tokens") == 20:  # the preflight probe
            return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
        system = payload["messages"][0]["content"]
        intents = []
        if "Society_Scout" in system:
            intents = [{"type": "CREATE_IMPROVEMENT", "payload": {"title": "Canary proposal", "problem": "canary signal observed", "proposed_change": "record and observe", "importance": 40}}]
        content = json.dumps({"decision_summary": "canary decision", "intents": intents, "sleep_for_seconds": 0})
        return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}

    return transport


# ── credential safety ─────────────────────────────────────────────────


def test_real_denylist_holds_history_fingerprints_only():
    assert len(cn.COMPROMISED_CREDENTIAL_FINGERPRINTS) == 3
    assert all(len(f) == 64 and int(f, 16) >= 0 for f in cn.COMPROMISED_CREDENTIAL_FINGERPRINTS)


def test_denylisted_fingerprint_blocks_preflight(society_settings, monkeypatch):
    s = _live(society_settings)
    st = cn.credential_status(s, scan_history=False, compromised={cn.fingerprint(TEST_KEY)})
    assert st.configured and st.compromised and not st.safe and st.fingerprint_prefix == cn.fingerprint(TEST_KEY)[:8]
    assert cn.preflight(s, scan_history=False, skip_probe=True).verdict == cn.VERDICT_READY
    monkeypatch.setattr(cn, "COMPROMISED_CREDENTIAL_FINGERPRINTS", frozenset({cn.fingerprint(TEST_KEY)}))
    rep = cn.preflight(s, scan_history=False, skip_probe=True)
    assert rep.verdict == cn.VERDICT_NO_CREDENTIAL and "rotate" in rep.credential.reason
    assert TEST_KEY not in json.dumps(rep.to_dict())


def test_git_history_scan_refuses_key_committed_then_removed(tmp_path, society_settings):
    repo = tmp_path / "r"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, env=env)

    git("init", "-q")
    leaked = "sk-" + "b" * 40
    (repo / "cfg.py").write_text(f'KEY = "{leaked}"\n')
    git("add", ".")
    git("commit", "-qm", "add key")
    (repo / "cfg.py").write_text('KEY = ""\n')
    git("commit", "-qam", "remove key")
    assert leaked not in (repo / "cfg.py").read_text()

    fps, complete = cn.scan_git_history_fingerprints(str(repo))
    assert complete and cn.fingerprint(leaked) in fps
    st = cn.credential_status(_live(society_settings, model_api_key=leaked), repo_root=str(repo))
    assert st.compromised and "git history" in st.reason and st.history_scanned
    st = cn.credential_status(_live(society_settings, model_api_key="sk-" + "c" * 40), repo_root=str(repo))
    assert st.safe and st.history_scan_complete
    # not a repository: scan is incomplete but never crashes, and does not vouch for safety silently
    st = cn.credential_status(_live(society_settings), repo_root=str(tmp_path / "nope"))
    assert st.safe and not st.history_scan_complete and "incomplete" in st.reason


def test_preflight_without_credential_blocks(society_settings, monkeypatch):
    monkeypatch.delenv("SOCIETY_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    rep = cn.preflight(_live(society_settings, model_api_key=""), scan_history=False)
    assert rep.verdict == cn.VERDICT_NO_CREDENTIAL and rep.credential.source is None and rep.probe is None
    assert rep.to_dict()["credential"]["safe"] is False


def test_preflight_refuses_non_live_provider(society_settings):
    rep = cn.preflight(society_settings, scan_history=False)  # scripted
    assert rep.verdict == cn.VERDICT_NOT_LIVE and rep.model_name == "" and not rep.ready
    rep = cn.preflight(dataclasses.replace(society_settings, model_provider="fake"), scan_history=False)
    assert rep.verdict == cn.VERDICT_NOT_LIVE


def test_preflight_probe_ready_with_injected_transport(society_settings):
    s = _live(society_settings)
    seen = []
    rep = cn.preflight(s, transport=_ok_transport(seen), scan_history=False)
    assert rep.ready and rep.probe.ok and rep.probe.requests == 1 and rep.probe.retries == 0
    assert rep.probe.tokens_in == 12 and rep.probe.tokens_out == 3 and rep.probe.latency_ms is not None
    assert rep.base_url_host == "model.invalid" and rep.credential.fingerprint_prefix == cn.fingerprint(TEST_KEY)[:8]
    text = json.dumps(rep.to_dict())
    assert TEST_KEY not in text and "sk-test" not in text
    assert seen[0]["response_format"]["type"] == "json_object" and seen[0]["max_tokens"] == 20 and seen[0]["model"] == "canary-test-model"


def test_preflight_unreachable_paths_never_echo_credential(society_settings):
    from services.registry.app.society.cognition import _HTTPStatus

    s = _live(society_settings, model_request_retries=0)

    async def boom(payload):
        raise ConnectionError("down")

    rep = cn.preflight(s, transport=boom, scan_history=False)
    assert rep.verdict == cn.VERDICT_UNREACHABLE and "transport error" in rep.probe.error

    async def prose(payload):
        return {"choices": [{"message": {"content": "sure! ok"}}]}

    rep = cn.preflight(s, transport=prose, scan_history=False)
    assert rep.verdict == cn.VERDICT_UNREACHABLE and rep.probe.status == "unexpected_content"

    async def unauth(payload):
        raise _HTTPStatus(401, "invalid api key " + TEST_KEY)

    rep = cn.preflight(s, transport=unauth, scan_history=False)
    assert rep.verdict == cn.VERDICT_UNREACHABLE and "401" in rep.probe.error
    assert TEST_KEY not in json.dumps(rep.to_dict())
    rep = cn.preflight(_live(society_settings, model_base_url=""), scan_history=False)
    assert rep.verdict == cn.VERDICT_UNREACHABLE and "BASE_URL" in rep.probe.error


# ── in-process canary ─────────────────────────────────────────────────


def test_run_refuses_fake_providers_and_disabled_runtime(society_settings, SessionLocal):
    with pytest.raises(cn.CanaryRefused) as ei:
        cn.run_canary(SessionLocal, society_settings, scenario="single", scan_history=False)
    assert "NO FAKE AUTONOMY" in str(ei.value)
    with pytest.raises(cn.CanaryRefused):
        cn.run_canary(SessionLocal, dataclasses.replace(society_settings, model_provider="fake"), scenario="single", scan_history=False)
    with pytest.raises(cn.CanaryRefused, match="SOCIETY_RUNTIME_ENABLED"):
        cn.run_canary(SessionLocal, _live(society_settings, runtime_enabled=False), scenario="single", scan_history=False)
    with pytest.raises(cn.CanaryRefused, match="NO SAFE CREDENTIAL"):
        cn.run_canary(SessionLocal, _live(society_settings, model_api_key=""), scenario="single", scan_history=False)
    with pytest.raises(cn.CanaryRefused, match="unknown scenario"):
        cn.run_canary(SessionLocal, _live(society_settings), scenario="nope", scan_history=False)


def test_run_single_scenario_reports_live_provider(db, SessionLocal, society_settings):
    s = _live(society_settings)
    calls = []
    rep = cn.run_canary(SessionLocal, s, scenario="single", transport=_decision_transport(calls), scan_history=False, worker_id="canary-t")
    assert rep.verdict == "PASS", rep.reasons
    assert rep.mode == "in_process" and rep.transport == "injected" and rep.provider == "openai_compatible"
    assert rep.totals["runs_completed"] >= 1 and "scout" in rep.totals["distinct_roles_completed"]
    assert all(r["model_provider"] == "openai_compatible" and r["model_name"] == "canary-test-model" for r in rep.runs if r["status"] == "completed")
    assert rep.totals["model_requests"] >= 1 and rep.totals["tokens_in"] >= 100
    assert any(i["intent_type"] == "CREATE_IMPROVEMENT" and i["execution_status"] == "executed" for i in rep.intents)
    assert rep.events[0]["event_type"] == "staging.canary.signal"
    text = json.dumps(rep.to_dict())
    assert TEST_KEY not in text and "canary decision" not in text and "payload" not in text  # no prompts, no decision text, no payloads
    assert len(calls) >= 1 and all("Authorization" not in json.dumps(c) for c in calls)


def test_run_approval_scenario_needs_gate_then_resumes_after_operator_decision(db, SessionLocal, society_settings, make_user, grants_with_no_cooldown):
    s = _live(society_settings)
    with pytest.raises(cn.CanaryRefused, match="gate"):
        cn.run_canary(SessionLocal, s, scenario="approval", transport=_decision_transport([]), scan_history=False)
    seed_society(db)
    grants_with_no_cooldown()  # two stories back-to-back in one test; the canary itself never touches grants
    with pytest.raises(cn.CanaryRefused, match="not an allowed intent"):
        cn.set_gate(db, role="scout", intent_type="SHELL_EXEC")
    assert cn.set_gate(db, role="scout", intent_type="CREATE_IMPROVEMENT") == ["CREATE_IMPROVEMENT"]
    op = make_user("op-canary@test")
    with pytest.raises(cn.CanaryRefused, match="not a society operator"):
        cn.run_canary(SessionLocal, s, scenario="approval", decide="approve", operator_email=op.email, transport=_decision_transport([]), scan_history=False)
    op.society_role = "operator"
    db.commit()

    calls = []
    rep = cn.run_canary(SessionLocal, s, scenario="approval", decide="approve", operator_email=op.email, transport=_decision_transport(calls), scan_history=False, worker_id="canary-a")
    assert rep.verdict == "PASS", (rep.reasons, rep.runs, rep.events, rep.worker_stats)
    parked = [i for i in rep.intents if i["intent_type"] == "CREATE_IMPROVEMENT"]
    assert parked and parked[0]["policy_decision"] == "approval_required" and parked[0]["execution_status"] == "executed"
    assert rep.approvals and rep.approvals[0]["decision"] == "approved" and rep.approvals[0]["final_state"] == "executed" and rep.approvals[0]["resumed"]
    assert rep.worker_stats["approved_intents_resumed"] == 1
    types = [e["event_type"] for e in rep.events]
    for t in ("intent.approval_required", "intent.approved", "intent.resumed", "intent.executed"):
        assert t in types
    audit = db.query(IntentApproval).all()
    assert len(audit) == 1 and audit[0].decided_by_user_id == db.query(User).filter(User.email == op.email).first().id
    # the gate is a grant fact the runtime never touched
    g = db.query(AgentCapabilityGrant).filter(AgentCapabilityGrant.role == "scout").first()
    assert g.approval_required_intents == ["CREATE_IMPROVEMENT"]
    assert cn.set_gate(db, role="scout", intent_type=None, clear=True) == []

    # reject: the intent must never execute
    cn.set_gate(db, role="scout", intent_type="CREATE_IMPROVEMENT")
    rep = cn.run_canary(SessionLocal, s, scenario="approval", decide="reject", operator_email=op.email, transport=_decision_transport([]), scan_history=False, worker_id="canary-r")
    assert rep.verdict == "PASS", rep.reasons
    assert any(i["intent_type"] == "CREATE_IMPROVEMENT" and i["execution_status"] == "rejected" for i in rep.intents)
    assert rep.worker_stats["approved_intents_resumed"] == 0
    # the canary did not re-seed: the operator's cooldown adjustment survived both stories
    assert all(g.wake_cooldown_seconds == 0 for g in db.query(AgentCapabilityGrant).all())


# ── HTTP-driven canary ────────────────────────────────────────────────


class _StubHttp:
    """Simulates a deployed registry: status, ingress, story detail with a
    scripted timeline, and approve/reject endpoints."""

    def __init__(self, timeline, *, provider="openai_compatible", runtime=True):
        self.timeline = list(timeline)
        self.provider = provider
        self.runtime = runtime
        self.posts = []
        self.polls = 0

    def call(self, method, path, body=None):
        if path == "/v1/society/status":
            return 200, {"runtime_enabled": self.runtime, "model_provider": self.provider, "production_deploy_enabled": False}
        if method == "POST" and path == "/v1/society/events":
            self.posts.append(("event", body))
            return 201, {"id": "e1", "duplicate": False}
        if method == "POST" and "/intents/" in path:
            self.posts.append(("decide", path))
            return 200, {"already_decided": False}
        if path.endswith("/detail"):
            frame = self.timeline[min(self.polls, len(self.timeline) - 1)]
            self.polls += 1
            return 200, frame
        raise AssertionError(path)


def _frame(run_status, intent_status=None, *, provider="openai_compatible", approval=None, depth=1):
    intents = []
    if intent_status:
        intents = [{"id": "i1", "seq": 0, "intent_type": "CREATE_IMPROVEMENT", "risk_class": "low", "policy_decision": "approval_required", "execution_status": intent_status, "approval": approval}]
    return {
        "events": [{"event_type": "platform.metric.anomaly", "status": "processed", "causation_depth": 0}, {"event_type": "intent.approval_required", "status": "processed", "causation_depth": depth}],
        "runs": [{"id": "r1", "role": "scout", "status": run_status, "attempt": 1, "model_provider": provider, "model_name": "live-m", "tokens_in": 10, "tokens_out": 2, "cost_usd": "0.0001", "model_requests": 1, "model_retries": 0, "model_timeouts": 0, "intents_count": 1, "intents": intents, "decision_summary": "SECRET REASONING", "context_summary": "x"}],
        "candidates": [],
    }


def test_observe_drives_approval_over_http_and_strips_content():
    stub = _StubHttp([
        _frame("running"),
        _frame("completed", "awaiting_approval"),
        _frame("completed", "executed", approval={"decision": "approved", "final_state": "executed", "resumed_at": "t"}),
        _frame("completed", "executed", approval={"decision": "approved", "final_state": "executed", "resumed_at": "t"}),
    ])
    rep = cn.observe_canary("http://stub", "tok", scenario="approval", decide="approve", http=stub, sleep=lambda s: None, timeout_seconds=30)
    assert rep.verdict == "PASS", rep.reasons
    assert [p for p in stub.posts if p[0] == "decide"] == [("decide", "/v1/society/intents/i1/approve")]
    assert stub.posts[0][1]["event_type"] == "platform.metric.anomaly" and stub.posts[0][1]["idempotency_key"].startswith("canary-approval-")
    text = json.dumps(rep.to_dict())
    assert "SECRET REASONING" not in text and "context_summary" not in text and "payload" not in text
    assert rep.model_name == "live-m" and rep.mode == "observe"


def test_observe_flags_fake_autonomy_and_refuses_bad_deployments():
    stub = _StubHttp([_frame("completed", provider="scripted"), _frame("completed", provider="scripted")])
    rep = cn.observe_canary("http://stub", "tok", scenario="single", http=stub, sleep=lambda s: None, timeout_seconds=30)
    assert rep.verdict == "FAIL" and any("fake autonomy" in r for r in rep.reasons)
    with pytest.raises(cn.CanaryRefused, match="NO FAKE AUTONOMY"):
        cn.observe_canary("http://stub", "tok", scenario="single", http=_StubHttp([], provider="scripted"), sleep=lambda s: None)
    with pytest.raises(cn.CanaryRefused, match="disabled"):
        cn.observe_canary("http://stub", "tok", scenario="single", http=_StubHttp([], runtime=False), sleep=lambda s: None)
    with pytest.raises(cn.CanaryRefused, match="operator token"):
        cn.observe_canary("http://stub", None, scenario="single", http=_StubHttp([]), sleep=lambda s: None)


def test_observe_parks_when_no_decision_is_given():
    stub = _StubHttp([_frame("completed", "awaiting_approval")] * 3)
    rep = cn.observe_canary("http://stub", "tok", scenario="approval", http=stub, sleep=lambda s: None, timeout_seconds=30)
    assert rep.verdict == "PARKED" and not [p for p in stub.posts if p[0] == "decide"]
