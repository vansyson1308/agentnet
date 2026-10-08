"""The bounded activity tool loop reserves its last turn for the answer.

Live 2026-09-28 (staging, real DeepSeek): every DiagnoseIncident try spent all
8 turns on paged repository reads and never returned a result (turn_budget),
so three real cases escalated without a diagnosis. The model now sees how many
turns remain and is told on the last one to submit; a tool call on the final
turn fails the try instead of being executed."""

from __future__ import annotations

import asyncio
import json

from services.registry.app.maintenance import activities as act
from services.registry.app.maintenance.taxonomy import ActivityKind

SPEC = act.SPECS[ActivityKind.DIAGNOSE_INCIDENT]
DIAGNOSIS = {"root_cause": "the landing template links to '#' instead of a route", "suspected_files": ["services/dashboard/app/templates/landing.html"],
             "evidence": ["services/dashboard/app/templates/landing.html:12"], "confidence": "medium"}


def _tools():
    return {name: (lambda args: {"ok": True, "lines": ["x"]}) for name in SPEC.tools}


def _reader_until_final(messages):
    """A diligent reader: always reads, but obeys the final-turn directive."""
    if messages[-1]["content"] == act.FINAL_TURN_DIRECTIVE:
        return {"action": "submit", "result": DIAGNOSIS}
    return {"action": "read_range", "args": {"path": "services/dashboard/app/templates/landing.html", "start_line": 1}}


def test_a_reader_that_obeys_the_final_turn_directive_submits_within_budget():
    model = act.ScriptedActivityModel(_reader_until_final)
    res = asyncio.run(act.run_activity(SPEC, {"incident": "x"}, model=model, tools=_tools()))
    assert res.ok, (res.error_class, res.error)
    assert res.turns == SPEC.max_turns
    assert len(res.tool_calls) == SPEC.max_turns - 1


def test_every_tool_result_states_the_turns_left():
    model = act.ScriptedActivityModel(_reader_until_final)
    asyncio.run(act.run_activity(SPEC, {"incident": "x"}, model=model, tools=_tools()))
    results = [m["content"] for m in model.calls[-1] if m["role"] == "user" and m["content"].startswith("TOOL_RESULT")]
    assert results and all("turns left after this:" in r for r in results)
    assert "turns left after this: 1" in results[-1]


def test_a_tool_call_on_the_final_turn_fails_the_try_and_is_not_executed():
    executed = []

    def tool(args):
        executed.append(args)
        return {"ok": True}

    model = act.ScriptedActivityModel(lambda m: {"action": "read_range", "args": {"path": "a", "start_line": len(executed) + 1}})
    res = asyncio.run(act.run_activity(SPEC, {"incident": "x"}, model=model, tools={n: tool for n in SPEC.tools}))
    assert not res.ok and res.error_class == "turn_budget" and "final turn" in res.error
    assert len(executed) == SPEC.max_turns - 1  # the final-turn call never ran
    assert json.dumps(model.calls[-1][-1]) and model.calls[-1][-1]["content"] == act.FINAL_TURN_DIRECTIVE
