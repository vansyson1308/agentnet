"""Harness² for the coding harness: one Propose-Probe-Compose step and the
improvement library (``maintenance/harness2.py``).

The executor runs are faked at ``harness.author_patch`` (their outcome is the
input under test); the editor is a scripted model -- never live evidence.
Delivery is always the harness's own gate: nothing here lets the editor or a
contrast declare a task done.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from services.registry.app.maintenance import activities as act
from services.registry.app.maintenance import harness as h
from services.registry.app.maintenance import harness2 as h2
from services.registry.app.maintenance.config import MaintenanceSettings
from services.registry.app.maintenance.taxonomy import ActivityKind

EDIT = {"invariant": "Run the single failing test first, read its assertion, then patch the smallest span.",
        "domain": "Dashboard routes live in services/dashboard/app/main.py; templates must keep their block names.",
        "reused_lessons": []}
PAYLOAD = {"task": {"trust": "untrusted_spec_text", "text": "fix it"}, "plan": {"files_allowed": ["services/dashboard/app/main.py"], "acceptance_tests": ["t.py"]}}


def _run(outcome: str, cost: str = "0.01") -> h.PatchRun:
    """outcome: delivered | red (patched, tests red) | idle (no patch) | invalid"""
    res = act.ActivityResult(ok=outcome == "delivered", kind=ActivityKind.AUTHOR_PATCH, turns=3, tokens_in=100, tokens_out=10, cost_usd=Decimal(cost),
                             error_class=None if outcome == "delivered" else {"red": "turn_budget", "idle": "turn_budget", "invalid": "invalid_output"}[outcome])
    st = h.AttemptState(ws=None, files_allowed=[], test_targets=[], max_test_runs=8, test_timeout=10, page_bytes=1000)
    if outcome in ("delivered", "red"):
        st.patches_applied, st.test_runs = 2, 2
        st.last_test = h.TargetedTestRun(outcome == "delivered", 0 if outcome == "delivered" else 1, [] if outcome == "delivered" else [{"test": "t.py::a", "reason": "x"}])
    return h.PatchRun(res, st, [{"sample": 1}])


def _patch(monkeypatch, outcomes):
    calls = []

    async def fake(ws, payload, **kw):
        assert kw["settings"].builder_samples == 1
        calls.append({"payload": payload, "cost_cap": kw["cost_cap"]})
        return _run(outcomes.pop(0))

    monkeypatch.setattr(h, "author_patch", fake)
    return calls


def _editor(*replies):
    return act.ScriptedActivityModel([r if isinstance(r, str) else json.dumps(r) for r in replies], cost_usd="0.002")


def _go(model, cap="1.00", lessons=()):
    return asyncio.run(h2.author_patch_recursive(None, PAYLOAD, files_allowed=["services/dashboard/app/main.py"], tests=["t.py"], model=model,
                                                 settings=MaintenanceSettings(), test_timeout=10, cost_cap=Decimal(cap), lessons=lessons))


def test_contrast_classes():
    ref = {"delivered": False, "patches": 0, "test_runs": 0, "error_class": "turn_budget"}
    assert h2.contrast(ref, {**ref, "delivered": True}) == "fixed"
    assert h2.contrast(ref, {**ref, "patches": 2, "test_runs": 1}) == "improved"
    assert h2.contrast(ref, dict(ref)) == "unchanged"
    assert h2.contrast(ref, {**ref, "error_class": "invalid_output"}) == "regressed"


def test_a_delivered_reference_needs_no_recursion(monkeypatch):
    calls = _patch(monkeypatch, ["delivered"])
    model = _editor(EDIT)
    out = _go(model)
    assert h2.delivered(out) and len(calls) == 1 and not model.calls
    assert "harness_guidance" not in calls[0]["payload"] and out.record["probes"] == []


def test_a_probe_that_delivers_wins_and_its_edit_is_the_evidence(monkeypatch):
    calls = _patch(monkeypatch, ["red", "delivered"])
    out = _go(_editor(EDIT))
    assert h2.delivered(out) and len(calls) == 2
    guidance = calls[1]["payload"]["harness_guidance"]
    assert guidance["trust"] == "harness_editor_proposal" and guidance["edits"] == [{"scope": "invariant", "guidance": EDIT["invariant"]}]
    assert calls[1]["payload"]["plan"] == PAYLOAD["plan"]  # guidance never touches the plan (files, tests)
    assert out.record["probes"][0]["contrast"] == "fixed"
    assert out.result.cost_usd == Decimal("0.022")  # two runs + the editor, one shared cap
    assert calls[1]["cost_cap"] == Decimal("1.00") - Decimal("0.01") - Decimal("0.002")
    assert [s["stage"] for s in out.samples] == ["reference", "probe:invariant"]


def test_improved_edits_are_composed_and_unsupported_ones_dropped(monkeypatch):
    calls = _patch(monkeypatch, ["idle", "red", "invalid", "delivered"])
    out = _go(_editor(EDIT))
    assert [p["contrast"] for p in out.record["probes"]] == ["improved", "regressed"]
    assert calls[3]["payload"]["harness_guidance"]["edits"] == [{"scope": "invariant", "guidance": EDIT["invariant"]}]
    assert out.record["composed"]["contrast"] == "fixed" and h2.delivered(out)


def test_no_supported_edit_keeps_the_current_harness(monkeypatch):
    calls = _patch(monkeypatch, ["red", "red", "red"])
    out = _go(_editor(EDIT))
    assert len(calls) == 3 and out.record["composed"] is None and not h2.delivered(out)


def test_an_invalid_editor_answer_means_no_probe(monkeypatch):
    calls = _patch(monkeypatch, ["red"])
    out = _go(_editor({"invariant": "too short", "domain": "x"}))
    assert len(calls) == 1 and out.record["editor_error"].startswith("editor output invalid")
    assert out.result.cost_usd == Decimal("0.012")  # the editor call still cost money


def test_the_cost_cap_stops_probing(monkeypatch):
    calls = _patch(monkeypatch, ["red", "red", "red"])
    _go(_editor(EDIT), cap="0.012")
    assert len(calls) == 1


def test_build_engine_uses_recursion_only_behind_its_flag(monkeypatch):
    from services.registry.app.society.engineering import build_engine

    seen = []

    async def recursive(ws, payload, **kw):
        seen.append(kw["lessons"])
        out = _run("delivered")
        out.record = {"probes": []}
        return out

    monkeypatch.setattr(h2, "author_patch_recursive", recursive)
    monkeypatch.setattr(build_engine, "reset_to_base", lambda ws: None)
    monkeypatch.setattr(build_engine, "spec_input", lambda *a, **k: dict(PAYLOAD))
    plain = []

    async def best_of_n(ws, payload, **kw):
        plain.append(kw["settings"].builder_samples)
        return _run("delivered")

    monkeypatch.setattr(h, "author_patch", best_of_n)
    spec = {"files_allowed": ["a.py"], "acceptance_tests": ["t.py"]}
    from services.registry.app.society.config import SocietySettings

    off = asyncio.run(build_engine.build(None, spec, title="t", model=None, ms=MaintenanceSettings(), ss=SocietySettings(), cost_cap=Decimal("1")))
    assert off.record is None and not seen and plain == [3]
    monkeypatch.setenv("MAINTENANCE_HARNESS2_ENABLED", "true")
    on = asyncio.run(build_engine.build(None, spec, title="t", model=None, ms=MaintenanceSettings(), ss=SocietySettings(), cost_cap=Decimal("1"), lessons=[{"id": "x"}]))
    assert on.record == {"probes": []} and seen == [[{"id": "x"}]] and on.stats()["harness2"] == {"probes": []}


# ── the improvement library (needs Postgres) ───────────────────────────


def _record(probes, composed=None, reused=()):
    return {"domain": "services/dashboard", "reference": {"delivered": False, "patches": 0, "test_runs": 0, "error_class": "turn_budget"},
            "probes": probes, "composed": composed, "reused_lessons": list(reused)}


def _probe(scope, contrast, delivered=False):
    return {"scope": scope, "guidance": EDIT[scope], "contrast": contrast,
            "signals": {"delivered": delivered, "patches": 1, "test_runs": 1, "error_class": None if delivered else "turn_budget"}}


def test_only_an_execution_contrast_admits_a_lesson(db):
    assert h2.reflect(db, _record([_probe("invariant", "improved"), _probe("domain", "unchanged")]), source="candidate:a")["admitted"] == 0
    assert h2.reflect(db, _record([_probe("invariant", "fixed", delivered=True)]), source="candidate:b")["admitted"] == 1
    assert h2.reflect(db, _record([_probe("invariant", "fixed", delivered=True)]), source="candidate:b")["admitted"] == 0  # idempotent
    lessons = h2.load_lessons(db, "services/other")
    assert [x["scope"] for x in lessons] == ["invariant"] and lessons[0]["guidance"] == EDIT["invariant"] and lessons[0]["support"] == 1
    composed = {"edits": [{"scope": "domain", "guidance": EDIT["domain"]}], "signals": {"delivered": True, "patches": 1, "test_runs": 1, "error_class": None}, "contrast": "fixed"}
    assert h2.reflect(db, _record([_probe("domain", "improved")], composed), source="candidate:c")["admitted"] == 1
    assert {x["scope"] for x in h2.load_lessons(db, "services/dashboard")} == {"invariant", "domain"}
    assert [x["scope"] for x in h2.load_lessons(db, "services/other")] == ["invariant"]  # a domain lesson stays in its domain


def test_reused_lessons_are_supported_or_contradicted_and_retire(db):
    h2.reflect(db, _record([_probe("invariant", "fixed", delivered=True)]), source="candidate:a")
    lid = h2.load_lessons(db, "services/dashboard")[0]["id"]
    assert h2.reflect(db, _record([_probe("domain", "fixed", delivered=True)], reused=[lid]), source="candidate:b")["supported"] == 1
    assert h2.load_lessons(db, "x")[0]["support"] == 2
    for i in range(4):
        h2.reflect(db, _record([_probe("domain", "regressed")], reused=[lid]), source=f"candidate:r{i}")
    assert lid not in {x["id"] for x in h2.load_lessons(db, "services/dashboard")}  # contradicted more than supported -> retired
