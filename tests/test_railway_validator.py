"""Pure helpers of the in-Railway staging validator (deploy/railway/validate_staging.py).

The validator itself runs only inside the staging environment; these tests
pin the parts that decide PASS/FAIL and the never-print-a-secret contract."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PATH = REPO / "deploy/railway/validate_staging.py"


def _load():
    spec = importlib.util.spec_from_file_location("validate_staging", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_derived_password_satisfies_the_registry_policy():
    v = _load()
    for secret in ("a" * 64, "0123456789abcdef" * 4, "", "ZZ-99"):
        pw = v.derive_password(secret)
        assert len(pw) >= 12 and any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw)
    assert v.derive_password("a" * 64) != v.derive_password("b" * 64)


def test_spoof_analysis_passes_only_when_forged_headers_share_the_bucket():
    v = _load()
    ok, _ = v.analyse_spoof([401] * 150 + [429] * 70, [429] * 60)
    assert ok
    ok, _ = v.analyse_spoof([401] * 150 + [429] * 70, [401] * 10 + [429] * 50)
    assert ok, "429 within the forged run and no later than the baseline"
    ok, note = v.analyse_spoof([401] * 150 + [429] * 70, [401] * 60)
    assert not ok and "fresh bucket" in note
    ok, note = v.analyse_spoof([401] * 220, [429] * 60)
    assert not ok and "never tripped" in note
    ok, _ = v.analyse_spoof([401] * 100 + [429] * 120, [401] * 120 + [429] * 5)
    assert not ok, "forged run tripped later than the baseline"


def test_child_output_summary_keeps_only_verdict_lines():
    v = _load()
    text = "Authorization: Bearer eyJ.secret\nPASS C01 healthz\nFAIL C05 leak — note\nSKIP C12 probe\ngarbage\nSOCIETY SMOKE: PASS\n"
    lines = v.summarise_script_output(text)
    assert lines == ["PASS C01 healthz", "FAIL C05 leak — note", "SKIP C12 probe", "SOCIETY SMOKE: PASS"]
    assert not any("eyJ" in ln for ln in lines)


def test_validator_never_prints_tokens_or_passwords():
    text = PATH.read_text(encoding="utf-8")
    # every stdout write is a CHECK/VALIDATOR/summary line; tokens and the password are never interpolated
    for m in re.finditer(r"sys\.stdout\.write\((.+)\)", text):
        arg = m.group(1)
        assert "token" not in arg.lower() and "password" not in arg.lower() and "secret\"" not in arg, arg
    assert 'expected_runtime(env("VALIDATOR_EXPECT_RUNTIME"))' in text, "the runtime flag asserted comes from VALIDATOR_EXPECT_RUNTIME (off unless set to on)"
    assert "auth/user/login" in text and "auth/user/register" in text and "/v1/agents/public/" in text


# ── Phase 5: VALIDATOR_EXPECT_RUNTIME + the live-society driver (deploy/railway/phase5_live.py) ──

DRIVER = REPO / "deploy/railway/phase5_live.py"


def _driver():
    spec = importlib.util.spec_from_file_location("phase5_live", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_expected_runtime_defaults_to_off_and_only_on_turns_the_assertion_on():
    v = _load()
    assert v.expected_runtime("") == "off"
    assert v.expected_runtime("off") == "off"
    assert v.expected_runtime("false") == "off"
    assert v.expected_runtime("garbage") == "off"
    for on in ("on", "ON", "true", "1", "yes", " on "):
        assert v.expected_runtime(on) == "on"
    text = PATH.read_text(encoding="utf-8")
    assert 'expected_runtime(env("VALIDATOR_EXPECT_RUNTIME"))' in text
    assert '"--expect-runtime", expect_runtime' in text


def test_driver_plan_grammar():
    d = _driver()
    plan = d.parse_plan(" baseline, gate:scout:CREATE_IMPROVEMENT ,canary:approval:approve,gate:scout:clear,fund:100:2,signal:candidate,audit:6,status ")
    assert plan == [
        ("baseline", []),
        ("gate", ["scout", "CREATE_IMPROVEMENT"]),
        ("canary", ["approval", "approve"]),
        ("gate", ["scout", "clear"]),
        ("fund", ["100", "2"]),
        ("signal", ["candidate"]),
        ("audit", ["6"]),
        ("status", []),
    ]
    assert d.parse_plan("intents:b5cba482-1b16-422a-9bc2-8055fbaa1403") == [("intents", ["b5cba482-1b16-422a-9bc2-8055fbaa1403"])]
    for bad in ("", "nuke", "canary", "canary:bogus", "canary:single:maybe", "gate:scout", "fund", "fund:0", "fund:ten", "signal:now", "audit:soon", "intents", "intents:not-an-id"):
        with pytest.raises(ValueError):
            d.parse_plan(bad)


def test_driver_scrubs_every_secret_shape_and_chunks_long_json():
    d = _driver()
    key = "sk-" + "A" * 40
    jwt = "eyJ" + "a" * 20 + ".eyJ" + "b" * 20 + "." + "c" * 30
    text = f"error {key} Authorization: Bearer {jwt} and dsk-{'Z' * 30} but task-{'x' * 30} stays"
    out = d.scrub(text)
    assert key not in out and jwt not in out and "dsk-" not in out
    assert "task-" + "x" * 30 in out, "idempotency keys are not secrets"
    lines = d.json_lines("step.key", {"k": "v" * 30, "leak": key})
    assert lines == ['PHASE5-JSON step.key {"k":"' + "v" * 30 + '","leak":"***"}']
    big = d.json_lines("s.k", {"blob": "x" * 30000}, max_len=10000)
    assert len(big) == 4 and big[0].startswith("PHASE5-JSON s.k#1/4 ") and big[-1].startswith("PHASE5-JSON s.k#4/4 ")
    assert "".join(ln.split(" ", 2)[2] for ln in big) == '{"blob":"' + "x" * 30000 + '"}'


def test_driver_gate_only_narrows_and_fund_key_is_bounded():
    d = _driver()
    assert d.gate_list(["WRITE_MEMORY", "CREATE_IMPROVEMENT"], [], "CREATE_IMPROVEMENT") == ["CREATE_IMPROVEMENT"]
    assert d.gate_list(["CREATE_IMPROVEMENT"], ["CREATE_IMPROVEMENT"], "CREATE_IMPROVEMENT") == ["CREATE_IMPROVEMENT"]
    assert d.gate_list(["CREATE_IMPROVEMENT"], ["CREATE_IMPROVEMENT"], "clear") == []
    with pytest.raises(ValueError):
        d.gate_list(["WRITE_MEMORY"], [], "SHELL_EXEC")
    key = d.fund_idempotency_key("Society_Architect", 3)
    assert key == "phase5-fund-society-architect-3" and len(key) <= 64
    assert len(d.fund_idempotency_key("X" * 90, 1)) <= 64


def test_driver_public_surface_walker_finds_private_keys_anywhere():
    d = _driver()
    assert d.private_keys_in({"runtime_enabled": True, "fleet": [{"role": "scout", "enabled": True}]}) == []
    found = d.private_keys_in({"runs": [{"id": "1", "decision_summary": "x"}], "events": [{"payload": {}}], "ok": {"nested": {"workspace_path": "/w"}}})
    assert found == ["runs[0].decision_summary", "events[0].payload", "ok.nested.workspace_path"]


def _story(*, runs, events=(), candidates=()):
    return {"runs": list(runs), "events": list(events), "candidates": list(candidates)}


def test_driver_chain_checks_pass_only_on_real_live_evidence():
    d = _driver()
    live_run = {"id": "r1", "role": "scout", "status": "completed", "model_provider": "openai_compatible", "intents": [{"intent_type": "CREATE_IMPROVEMENT", "execution_status": "executed"}]}
    gov_run = {"id": "r2", "role": "governor", "status": "completed", "model_provider": "openai_compatible", "intents": [{"intent_type": "REVIEW_IMPROVEMENT", "execution_status": "executed"}]}
    events = [{"causation_depth": 0}, {"causation_depth": 1}]
    checks = dict((c, (ok, n)) for c, ok, n in d.chain_checks(_story(runs=[live_run, gov_run], events=events), [], expect_candidate=False))
    assert set(checks) == {"K01", "K02", "K03", "K04", "K05"} and all(ok for ok, _ in checks.values())
    # a scripted/fake provider is never live proof
    fake = dict(live_run, model_provider="scripted")
    ok = {c: ok for c, ok, _ in d.chain_checks(_story(runs=[fake], events=events), [], expect_candidate=False)}
    assert ok["K01"] is False
    # a forbidden HIGH intent that executed fails K03; a dead run fails K02
    high = dict(live_run, intents=[{"intent_type": "SHELL_EXEC", "execution_status": "executed"}])
    ok = {c: ok for c, ok, _ in d.chain_checks(_story(runs=[high, dict(gov_run, status="dead", error="boom")], events=events), [], expect_candidate=False)}
    assert ok["K03"] is False and ok["K02"] is False
    # candidate expectations: READY + QA actually ran + bounded allow-list + single candidate
    cand = {"id": "c1", "status": "ready", "qa_verdict": "pass", "security_verdict": "pass"}
    full = {"id": "c1", "spec": {"kind": "docs", "files_allowed": ["docs/society/candidates/x.md"]}, "changed_files": ["docs/society/candidates/x.md"], "qa_report": {"checks": [{"name": "acceptance_tests", "passed": True, "detail": "1 passed"}]}}
    ok = {c: ok for c, ok, _ in d.chain_checks(_story(runs=[live_run, gov_run], events=events, candidates=[cand]), [full], expect_candidate=True)}
    assert all(ok[c] for c in ("K06", "K07", "K08", "K09", "K10")), ok
    skipped = dict(full, qa_report={"checks": [{"name": "acceptance_tests", "passed": False, "detail": "skipped: pre-flight gates failed"}]}, changed_files=["docs/other.md"])
    ok = {c: ok for c, ok, _ in d.chain_checks(_story(runs=[live_run], events=events, candidates=[cand, dict(cand, id="c2", status="rejected")]), [skipped], expect_candidate=True)}
    assert ok["K08"] is False and ok["K09"] is False and ok["K10"] is False


def test_driver_economics_checks_enforce_one_payment_per_task_and_reserved_consistency():
    d = _driver()
    tasks = [{"id": "t1", "status": "completed", "escrow_amount": 10}, {"id": "t2", "status": "initiated", "escrow_amount": 10}]
    txs = [{"task_session_id": "t1", "type": "payment", "status": "completed", "amount": 10}, {"task_session_id": "t2", "type": "payment", "status": "pending", "amount": 10}]
    wallets = [{"agent": "Society_Architect", "wallet_id": "w1", "balance_credits": 90, "reserved_credits": 10}]
    ok = {c: ok for c, ok, _ in d.economics_checks(tasks, txs, wallets, {"w1": 10})}
    assert ok == {"E01": True, "E02": True, "E03": True}
    # duplicate payment, wrong status, and a wallet whose reservation drifted from the in-flight escrow
    txs2 = txs + [{"task_session_id": "t1", "type": "payment", "status": "completed", "amount": 10}]
    ok = {c: ok for c, ok, _ in d.economics_checks(tasks, txs2, wallets, {"w1": 20})}
    assert ok["E01"] is False and ok["E03"] is False
    txs3 = [dict(txs[0], status="pending"), dict(txs[1], amount=11)]
    ok = {c: ok for c, ok, _ in d.economics_checks(tasks, txs3, wallets, {"w1": 10})}
    assert ok["E02"] is False


def test_driver_never_prints_tokens_or_secrets():
    text = DRIVER.read_text(encoding="utf-8")
    for m in re.finditer(r"(?:_w|out\.info|out\.check)\((.+)\)", text):
        arg = m.group(1).lower()
        for forbidden in ("{token", "{secret", "{password", "{api_key", "{raw", "payload_json"):
            assert forbidden not in arg, m.group(0)
    assert "STAGING_VALIDATOR_SECRET" in text and "derive_password" in text
    assert "SOCIETY_MODEL_API_KEY" not in text, "the driver never even names the model credential"
    # every log line goes through scrub()
    assert text.count("def scrub(") == 1 and "scrub(detail)" in text and "text = scrub(json.dumps(" in text


def test_driver_paces_stories_past_the_fleet_cooldown():
    from datetime import datetime, timezone

    d = _driver()
    now = datetime(2026, 9, 19, 8, 24, 0, tzinfo=timezone.utc)
    assert d.seconds_to_wait(None, 30, now) == 0
    assert d.seconds_to_wait("garbage", 30, now) == 0
    assert d.seconds_to_wait("2026-09-19T08:23:50+00:00", 30, now) == 25
    assert d.seconds_to_wait("2026-09-19T08:23:50Z", 30, now, margin_seconds=0) == 20
    assert d.seconds_to_wait("2026-09-19T08:23:00", 30, now) == 0, "naive timestamps are UTC; elapsed cooldown waits 0"
    text = DRIVER.read_text(encoding="utf-8")
    assert text.count("pace_for_cooldown(out, ") == 3, "canary, signal and taskfail all pace before a story starts"
    for step in ("canary", "signal", "taskfail"):
        assert f'pace_for_cooldown(out, "{step}"' in text, f"{step} must pace past the wake cooldown"


def test_driver_intent_rows_keep_trusted_reasons_and_key_names_only():
    d = _driver()
    key = "sk-" + "B" * 40
    detail = {
        "runs": [
            {"id": "44719395-b014-48a3-858e-a4c3377316ea", "role": "scout", "status": "completed", "decision_summary": "s", "intents": [
                {"seq": 0, "intent_type": "CREATE_IMPROVEMENT", "risk_class": "low", "policy_decision": "invalid", "execution_status": "denied",
                 "policy_reason": "payload schema violation: [{'type': 'uuid_parsing', 'loc': ('source_task_id',)}] " + key, "error": None,
                 "payload": {"title": "secret-looking value " + key, "evidence": {"signal": "x"}}, "approval": None},
            ]},
        ]
    }
    rows = d.intent_rows(detail)
    assert rows == [{
        "run": "44719395", "role": "scout", "seq": 0, "type": "CREATE_IMPROVEMENT", "risk": "low", "policy": "invalid", "execution": "denied",
        "reason": "payload schema violation: [{'type': 'uuid_parsing', 'loc': ('source_task_id',)}] ***", "error": None,
        "payload_keys": ["evidence", "title"], "approval": None,
    }]
    assert key not in json.dumps(rows) and "secret-looking" not in json.dumps(rows), "payload values never printed; reasons scrubbed"



# ── Gate A: the `memory:<role>` diagnostic. Memory is the only runtime state a
# finished run leaves for the next one, so reading it back is how a canary that
# trained the fleet against its own signal becomes visible.


def test_plan_grammar_accepts_a_memory_role_and_rejects_junk():
    p5 = _driver()
    assert p5.parse_plan("memory:scout") == [("memory", ["scout"])]
    for bad in ("memory", "memory:Scout", "memory:scout:extra", "memory:../etc"):
        with pytest.raises(ValueError):
            p5.parse_plan(bad)


def test_memory_view_is_read_only_scrubbed_and_never_reads_contents():
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5.memory_view)
    lowered = src.lower()
    for forbidden in ("update ", "delete ", "insert ", "drop ", "m.content"):
        assert forbidden not in lowered, f"memory_view must stay read-only and title-only: {forbidden!r}"

    class _Cur:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=()):
            self.sql.append((sql, params))
            # built at runtime: a key-shaped LITERAL in a tracked file is itself
            # a finding (tests/test_no_hardcoded_secrets.py), and rightly so.
            key_shaped = "sk-" + "A" * 24
            self._rows = (
                [("AGENT", f"Repeated signal: {key_shaped}", None, None, 30, 40, "unvalidated", "run", None, "9f8e7d6c-5b4a-4938-8271-6f5e4d3c2b1a")]
                if "ORDER BY" in sql
                else [(7, 0, 3)]
            )

        def fetchall(self):
            return self._rows

    cur = _Cur()
    view = p5.memory_view(cur, "Society_Scout")
    assert view == {
        "agent": "Society_Scout",
        "live_rows": 7,
        "rows_with_expiry": 0,
        "distinct_correlations": 3,
        "newest": [
            {
                "scope": "AGENT",
                "title": "Repeated signal: ***",
                "created_at": None,
                "expires_at": None,
                "importance": 30,
                "confidence": 40,
                "validation": "unvalidated",
                "source": "run",
                "correlation": None,
                "id": "9f8e7d6c-5b4a-4938-8271-6f5e4d3c2b1a",
            }
        ],
    }
    assert all(isinstance(params, tuple) for _, params in cur.sql)  # parameterised, never interpolated


# ── Phase 5 closure: the real-domain step. A synthetic canary proves the
# runtime; only a REAL platform fact proves the product. This step creates and
# fails a task through the ordinary API and lets the runtime's own ingest turn
# it into task.failed — it must never inject the event or write a task row.


def test_plan_grammar_accepts_taskfail_with_an_optional_escrow():
    p5 = _driver()
    assert p5.parse_plan("taskfail") == [("taskfail", [])]
    assert p5.parse_plan("taskfail:5") == [("taskfail", ["5"])]
    for bad in ("taskfail:abc", "taskfail:5:6"):
        with pytest.raises(ValueError):
            p5.parse_plan(bad)


def test_taskfail_uses_the_ordinary_api_and_never_injects_the_event():
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5.step_taskfail)
    # the platform fact is made through the public API …
    assert '"POST", f"{base}/v1/tasks/"' in src
    assert "/fail?error_message=" in src
    # … and the society event must come from the runtime's own ingest, never
    # from the driver injecting one or editing the task row.
    assert "/v1/society/events" not in src, "the real-domain step must not inject a society event"
    for forbidden in ("UPDATE task_sessions", "INSERT INTO task_sessions", "INSERT INTO society_events"):
        assert forbidden not in src, f"the real-domain step must not write {forbidden!r}"
    # it asserts exactly one outcome event for the task, and no DEAD run
    assert "exactly one task-outcome event" in src
    assert "no DEAD run" in src


def test_taskfail_agent_registration_is_authenticated_and_staging_only():
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5._ensure_canary_agent)
    assert '"POST", f"{base}/v1/agents/", token' in src, "agent registration must be the authenticated route"
    assert "public-register" not in src
    assert "staging.invalid" in src, "the canary agent must not advertise a reachable endpoint"


def test_taskfail_reads_the_task_id_the_create_endpoint_actually_returns():
    """POST /v1/tasks answers {'task_session_id', 'trace_id', 'span_id'} — not
    {'id'}. Reading the wrong key made the step report FAIL on an HTTP 201."""
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5.step_taskfail)
    assert '"task_session_id"' in src


def test_plan_grammar_accepts_a_refute_by_memory_id_and_rejects_junk():
    p5 = _driver()
    mid = "3f2a1b4c5d6e47889a0b1c2d3e4f5061"
    assert p5.parse_plan(f"refute:{mid}") == [("refute", [mid])]
    for bad in ("refute", "refute:scout", "refute:" + mid + ":extra", "refute:../etc"):
        with pytest.raises(ValueError):
            p5.parse_plan(bad)


def test_refute_writes_through_the_operator_api_and_never_through_sql():
    """The driver holds a database connection, so the ONE thing this step must
    not do is the thing it could most easily do."""
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5.step_refute)
    assert '"POST", f"{base}/v1/society/memory/{memory_id}/refute"' in src
    lowered = src.lower()
    for forbidden in ("update memory_items", "insert into memory_items", "insert into memory_validation_events", "delete from"):
        assert forbidden not in lowered, f"a refutation must go through the API, never {forbidden!r}"
    # the history is read back OUT of the table rather than trusted from the response
    assert "_refute_history(conn, memory_id)" in src
    # and it proves the row was corrected, not erased
    assert "R03" in src and "untouched" in src


def test_refute_history_reader_is_read_only_and_never_prints_the_operator_email():
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5._refute_history)
    lowered = src.lower()
    for forbidden in ("update ", "insert ", "delete ", "drop "):
        assert forbidden not in lowered, f"the audit reader must stay read-only: {forbidden!r}"
    assert 'split("@")[0]' in src, "only the local part of the operator address may be printed"


def test_memory_view_exposes_the_id_so_a_refutation_can_name_its_target():
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5.memory_view)
    assert "m.id" in src and '"id": str(r[9])' in src


def test_memory_search_is_read_only_and_matches_a_literal_substring():
    """ILIKE wildcards in operator input would silently widen the search, and a
    refutation must hit the row the operator meant."""
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5.memory_search)
    lowered = src.lower()
    for forbidden in ("update ", "delete ", "insert ", "drop ", "m.content"):
        assert forbidden not in lowered, f"memory_search must stay read-only and title-only: {forbidden!r}"

    class _Cur:
        def execute(self, sql, params=()):
            self.sql, self.params = sql, params
            self._rows = [("9f8e7d6c-5b4a-4938-8271-6f5e4d3c2b1a", "Improvement raised for X", None, "unvalidated", None, "AGENT")]

        def fetchall(self):
            return self._rows

    cur = _Cur()
    hits = p5.memory_search(cur, "Society_Scout", "100%_raw\\")
    assert isinstance(cur.params, tuple)  # parameterised, never interpolated
    assert cur.params[1] == r"%100\%\_raw\\%", cur.params[1]
    assert "ESCAPE" in cur.sql
    assert hits[0]["id"] == "9f8e7d6c-5b4a-4938-8271-6f5e4d3c2b1a"
    assert hits[0]["validation"] == "unvalidated"
    assert hits[0]["correlation"] is None


def test_plan_grammar_accepts_promotions_with_an_optional_limit():
    p5 = _driver()
    assert p5.parse_plan("promotions") == [("promotions", [])]
    assert p5.parse_plan("promotions:5") == [("promotions", ["5"])]
    for bad in ("promotions:all", "promotions:5:6"):
        with pytest.raises(ValueError):
            p5.parse_plan(bad)


def test_promotions_step_is_read_only_and_checks_the_gates_not_just_the_status():
    """A PR that is not merged could simply not have been tried yet; the
    eligibility record is what 'auto-merge is off' has to be checked against."""
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5.step_promotions)
    lowered = src.lower()
    for forbidden in ("update ", "delete ", "insert ", "drop "):
        assert forbidden not in lowered, f"the promotion reader must stay read-only: {forbidden!r}"
    assert '"P02"' in src and '"P03"' in src
    assert "auto_merge_enabled" in src and "auto_merge_allowed" in src
    assert "human_approval_required" in src and "blocking" in src


def test_plan_grammar_accepts_candidates_with_an_optional_limit():
    p5 = _driver()
    assert p5.parse_plan("candidates") == [("candidates", [])]
    assert p5.parse_plan("candidates:3") == [("candidates", ["3"])]
    for bad in ("candidates:all", "candidates:3:4"):
        with pytest.raises(ValueError):
            p5.parse_plan(bad)


def test_candidates_step_is_read_only_and_reports_why_not_just_what():
    """A status alone cannot tell a guard rejecting busywork (working) from a
    pipeline break (a defect), and that difference decides whether to repair."""
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5.step_candidates)
    lowered = src.lower()
    for forbidden in ("update ", "delete ", "insert ", "drop "):
        assert forbidden not in lowered, f"the candidate reader must stay read-only: {forbidden!r}"
    assert "c.error" in src and "qa_failures" in src and "security_verdict" in src
    assert "scrub(" in src, "model-authored titles and errors are untrusted text"


def test_plan_grammar_accepts_an_abandon_by_candidate_id_and_rejects_junk():
    p5 = _driver()
    cid = "f8296297a1b24c3d8e9f0a1b2c3d4e5f"
    assert p5.parse_plan(f"abandon:{cid}") == [("abandon", [cid])]
    for bad in ("abandon", "abandon:builder", "abandon:" + cid + ":extra", "abandon:../etc"):
        with pytest.raises(ValueError):
            p5.parse_plan(bad)


def test_abandon_writes_through_the_operator_api_and_never_through_sql():
    """Same discipline as refute, and for the same reason.

    The driver holds a database connection. Setting status='abandoned' directly
    would "work", leave no audit row, and release no escrow — which is exactly
    the difference between a remedy and a cover-up."""
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5.step_abandon)
    assert '"POST", f"{base}/v1/society/candidates/{candidate_id}/abandon"' in src
    lowered = src.lower()
    for forbidden in ("update code_candidates", "insert into", "delete from", "update wallets", "update task_sessions"):
        assert forbidden not in lowered, f"abandoning must go through the API, never {forbidden!r}"
    # the outcome is read back OUT of the tables rather than trusted from the response
    assert "_candidate_row(conn, candidate_id)" in src
    assert "_abandon_events(conn, candidate_id)" in src
    # and it proves the row was closed, not erased
    assert "A03" in src and "untouched" in src


def test_abandon_proves_the_escrow_moved_once_and_only_once():
    import inspect

    p5 = _driver()
    src = inspect.getsource(p5.step_abandon)
    # the repeat call is what makes "exactly once" checkable at all
    assert src.count('/abandon"') == 2, "the step must call abandon twice to prove idempotency"
    assert "already_abandoned" in src
    assert "A07" in src, "the repeat must be shown to release nothing further"
    assert "A08" in src and "exactly 1 expected" in src


def test_abandon_readers_are_read_only():
    import inspect

    p5 = _driver()
    for fn in (p5._candidate_row, p5._abandon_events):
        lowered = inspect.getsource(fn).lower()
        for forbidden in ("update ", "insert ", "delete ", "drop "):
            assert forbidden not in lowered, f"{fn.__name__} must stay read-only: {forbidden!r}"
