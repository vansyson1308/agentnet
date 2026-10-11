"""One coding engine: the Society Builder builds a code candidate through the
maintenance repair harness (``maintenance/harness.py``).

The candidate's worktree is the attempt worktree; ``files_allowed`` is the
spec's and the tests are the spec's acceptance tests. The AuthorPatch loop
gets the target files front-loaded, exact-text patch tools bound to the
worktree, targeted test runs, a submit that is refused until the acceptance
tests pass on the current worktree, and a deterministic submit of a green
worktree when the turns run out. The model never writes whole files blind.

What judges the result is unchanged: QA (``qa.py``), Security, the risk
tiers and promotion see a committed head exactly as before. Every build
starts from the candidate's base (replay-safe: a re-build after a QA failure
or a crash never stacks patches). ``scripts/bench/run.py --path society``
runs this same function, so the bench scores the engine the Society uses.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence

from ...maintenance import activities as act
from ...maintenance import harness as h
from ...maintenance.config import MaintenanceSettings
from ...maintenance.patchset import reset_to_base
from ..config import SocietySettings
from . import workspace as ws_mod

PATCH_PROTOCOL = ("apply_patch args: {files: [{path, operations: [{op: replace_exact|insert_after|insert_before|create|delete, "
                  "old/new | anchor/text | text}]}]} -- exact text, each old/anchor unique")


@dataclass
class BuildOutcome:
    result: act.ActivityResult
    state: h.AttemptState
    samples: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def delivered(self) -> bool:
        return bool(self.result.ok and self.result.rescope is None)

    def stats(self) -> Dict[str, Any]:
        """Structural only (no model text): what the engine did and what it cost."""
        r, out = self.result, self.result.output or {}
        return {"engine": "harness", "ok": self.delivered, "error_class": r.error_class, "turns": r.turns, "test_runs": self.state.test_runs,
                "patches": self.state.patches_applied, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "cost_usd": str(r.cost_usd),
                "auto_submitted": bool(out.get("auto_submitted")), "tests_unverified": bool(out.get("tests_unverified")),
                "rescope": ({"reason": r.rescope.get("reason"), "required_files": list(r.rescope.get("required_files") or [])[:12]} if r.rescope else None),
                "samples": self.samples, "actions": [str(t.get("refused") and f"submit!{t['refused']}" or t.get("action")) for t in r.turn_log][:60]}


def pytest_targets(spec: Dict[str, Any]) -> List[str]:
    """The acceptance tests the harness runs while building: pytest targets only. A ``bench:<task>``
    proof is QA's (engineering/bench_proof.py: the bench runner on the candidate harness)."""
    return [t for t in spec.get("acceptance_tests") or [] if isinstance(t, str) and not t.startswith("bench:")]


def spec_input(spec: Dict[str, Any], *, title: str, ws: ws_mod.Workspace, ms: MaintenanceSettings, feedback: Sequence[str] = ()) -> Dict[str, Any]:
    """The AuthorPatch input for a candidate spec (the Architect's text is a model artifact, marked untrusted)."""
    files, tests = list(spec.get("files_allowed") or []), pytest_targets(spec)
    text = "\n".join(str(x) for x in (title, spec.get("description"), spec.get("expected_effect")) if x)
    out = {
        "task": {"trust": "untrusted_spec_text", "text": text[:6000]},
        "plan": {"files_allowed": files, "acceptance_tests": tests, "base_sha": ws.base_sha},
        "patch_protocol": PATCH_PROTOCOL,
        "target_files": {"trust": "untrusted_repository_data", "files": h.target_file_context(ws.path, files, [text], tests=tests)},
        "read_budget": f"at most {ms.builder_max_read_calls} read-tool calls this try; the target files are above",
    }
    if feedback:
        out["feedback_from_previous_attempts"] = {"trust": "qa_report", "failures": [str(f)[:500] for f in feedback][:10]}
    return out


async def build(ws: ws_mod.Workspace, spec: Dict[str, Any], *, title: str, model: act.ActivityModel, ms: MaintenanceSettings, ss: SocietySettings,
                cost_cap: Decimal, feedback: Sequence[str] = (), wrap_tools: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None) -> BuildOutcome:
    """Best-of-N AuthorPatch (``harness.author_patch``) on ``ws``, each sample
    from its base. The worktree is left as the delivered (else last) sample
    ended; nothing is committed here."""
    reset_to_base(ws)
    files, tests = list(spec.get("files_allowed") or []), pytest_targets(spec)
    run = await h.author_patch(ws, spec_input(spec, title=title, ws=ws, ms=ms, feedback=feedback), files_allowed=files, tests=tests, model=model,
                               settings=ms, test_timeout=ss.qa_test_timeout_seconds, cost_cap=cost_cap, wrap_tools=wrap_tools)
    return BuildOutcome(run.result, run.state, run.samples)


def run_blocking(coro, *, heartbeat: Callable[[], None], every: float) -> Any:
    """Run ``coro`` on its own event loop in a helper thread (the caller may be
    inside a running loop) while the calling thread keeps its lease alive."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="society-build") as pool:
        fut = pool.submit(asyncio.run, coro)
        while True:
            try:
                return fut.result(timeout=max(1.0, every))
            except concurrent.futures.TimeoutError:
                heartbeat()


def qa_feedback(qa_report: Optional[Dict[str, Any]]) -> List[str]:
    """The previous QA verdict's failures (structural strings), for a re-build."""
    report = qa_report or {}
    return [str(f) for f in (report.get("failures") or [])][:10] if report.get("verdict") == "fail" else []


__all__ = ["PATCH_PROTOCOL", "BuildOutcome", "spec_input", "build", "run_blocking", "qa_feedback"]
