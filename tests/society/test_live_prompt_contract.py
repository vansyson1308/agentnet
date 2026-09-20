"""Phase 5 — what a LIVE model is told must be complete and trusted.

* The system prompt says payloads must match the documented schema exactly,
  so nested payload models (CodeChangeSpec, FileEdit, evidence) must be
  documented, not rendered as ``""``/``"array"``.
* Engineering conventions the trusted QA gate enforces (docs candidate path,
  acceptance test, required sections) reach the roles that design/build/verify
  code — from CODE, matching the acceptance test itself — and nobody else.
* The HTTP canary stays importable without the ORM (the Railway validator has
  no service secrets), so ``observe_canary`` can run from outside the worker.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from services.registry.app.society import context as ctx_mod
from services.registry.app.society.cognition import _schemas_doc
from services.registry.app.society.config import SocietySettings

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def _doc_lines() -> dict:
    out = {}
    for line in _schemas_doc().splitlines():
        name, _, body = line[2:].partition(": ")
        out[name] = json.loads(body)
    return out


def test_schema_doc_documents_nested_payload_models():
    doc = _doc_lines()
    spec = doc["REQUEST_CODE_CHANGE"]["spec"]
    assert isinstance(spec, dict), "CodeChangeSpec must be inlined, not an empty string"
    assert spec["files_allowed"] == ["string"] and spec["acceptance_tests"] == ["string"]
    assert spec["kind"] == "docs|test_fixture|code" and spec["must_compile"] == "boolean"
    edits = doc["SUBMIT_CODE_CANDIDATE"]["edits"]
    assert isinstance(edits, list) and len(edits) == 1 and isinstance(edits[0], dict), "FileEdit must be inlined inside the array"
    assert set(edits[0]) == {"path", "content"} and all(v.startswith("string") for v in edits[0].values())
    evidence = doc["CREATE_IMPROVEMENT"]["evidence"]
    assert set(evidence) >= {"signal", "baseline", "observed", "window", "sample_size", "actionable_reason"}
    assert doc["SEND_MESSAGE"]["to_agent"] == "string(<=255 chars)|null", "Optional[str] says it may be null, and its bound"
    assert doc["WRITE_MEMORY"]["source_task_id"] == "uuid|null" and doc["SEND_MESSAGE"]["thread_id"] == "uuid|null", "optional uuid references are documented as uuid-or-null, never a free string"
    assert doc["REQUEST_CODE_CHANGE"]["proposal_id"] == "uuid|null" and doc["READ_REPO_FILE"]["candidate_id"] == "uuid|null"
    assert doc["REVIEW_IMPROVEMENT"]["proposal_id"] == "uuid", "required uuid references are documented as uuid"
    # Scalars carry the bounds the typed parser enforces; see
    # test_every_enforced_scalar_bound_is_stated_in_the_prompt for why.
    assert doc["WRITE_MEMORY"]["title"] == "string(1..255 chars)" and doc["WRITE_MEMORY"]["importance"] == "integer(0..100)"
    assert len(_schemas_doc()) < 6000, "the schema block stays a bounded part of every prompt"


def test_engineering_conventions_match_the_trusted_acceptance_test_and_reach_only_engineering_roles():
    settings = SocietySettings()
    conv = ctx_mod.engineering_conventions(settings)
    acceptance = (REPO / "tests" / "society" / "acceptance" / "test_candidate_docs.py").read_text(encoding="utf-8")
    for section in ctx_mod.DOCS_REQUIRED_SECTIONS:
        assert section in acceptance and section in conv["docs_candidate"]
    assert ctx_mod.DOCS_ACCEPTANCE_TEST in conv["docs_candidate"] and (REPO / ctx_mod.DOCS_ACCEPTANCE_TEST).exists()
    assert ctx_mod.DOCS_CANDIDATE_DIR in conv["docs_candidate"] and (REPO / ctx_mod.DOCS_CANDIDATE_DIR).is_dir()
    assert conv["branch_prefix"] == settings.branch_prefix
    assert set(ctx_mod.ENGINEERING_ROLES) == {"architect", "builder", "qa", "security", "evaluator"}
    assert "scout" not in ctx_mod.ENGINEERING_ROLES and "governor" not in ctx_mod.ENGINEERING_ROLES
    assert "sk-" not in json.dumps(conv) and "token" not in json.dumps(conv).lower()


def test_http_canary_is_importable_without_the_orm():
    code = (
        "import sys; sys.modules['app.models'] = None; sys.modules['app.database'] = None; sys.modules['app.config'] = None\n"
        "from app.society.canary import observe_canary, CanaryRefused, _now_iso, evaluate, SCENARIOS\n"
        "assert _now_iso().endswith('+00:00') or 'T' in _now_iso()\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO / "services" / "registry"), capture_output=True, text=True, timeout=120, env={"PATH": "", "PYTHONPATH": str(REPO / "services" / "registry"), "HOME": "/tmp", "ENVIRONMENT": "staging"})
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr[-800:]


# ── Gate A run 9 (Railway staging, real DeepSeek): both Scout intents were
# rejected with ``uuid_parsing … input: ''`` on ``source_task_id``. The model
# sent an empty string for an optional reference. The schema doc now says
# ``uuid|null`` and the strict base reads "" as absent for optional non-text
# fields — nothing else is coerced.


def test_empty_string_for_an_optional_reference_means_absent():
    from services.registry.app.society.intents import CreateImprovementPayload, WriteMemoryPayload

    memory = WriteMemoryPayload(title="t", content="c", source_task_id="")
    assert memory.source_task_id is None
    proposal = CreateImprovementPayload(
        title="t",
        problem="p",
        proposed_change="c",
        source_task_id="",
        evidence={"signal": "task_failure_rate", "actionable_reason": "above threshold", "sample_size": ""},
    )
    assert proposal.source_task_id is None
    assert proposal.evidence is not None and proposal.evidence.sample_size is None


def test_only_empty_strings_on_optional_non_text_fields_are_normalised():
    import pytest
    from pydantic import ValidationError

    from services.registry.app.society.intents import CreateGoalPayload, ReviewImprovementPayload, WriteMemoryPayload

    with pytest.raises(ValidationError):  # a NON-empty invalid id is still invalid
        WriteMemoryPayload(title="t", content="c", source_task_id="not-a-uuid")
    with pytest.raises(ValidationError):  # a REQUIRED id stays required
        ReviewImprovementPayload(proposal_id="", decision="approve", reason="r")
    with pytest.raises(ValidationError):  # unknown keys are still rejected
        WriteMemoryPayload(title="t", content="c", source_task_id="", shell="rm -rf /")
    with pytest.raises(ValidationError):  # required text is still required (min_length)
        WriteMemoryPayload(title="", content="c")
    goal = CreateGoalPayload(title="t", description="")
    assert goal.description == ""  # optional TEXT keeps its value; only non-text optionals are normalised


def test_validate_intents_records_the_normalised_payload_as_valid():
    import uuid

    from services.registry.app.society.intents import IntentType, parse_decision, validate_intents

    decision = parse_decision(
        {
            "decision_summary": "record the observation",
            "intents": [{"type": IntentType.WRITE_MEMORY.value, "payload": {"title": "t", "content": "c", "source_task_id": ""}}],
            "sleep_for_seconds": 600,
        },
        max_intents=5,
    )
    validated = validate_intents(decision, uuid.uuid4())
    assert len(validated) == 1 and validated[0].valid, validated[0].error
    assert validated[0].payload.source_task_id is None
    assert validated[0].raw_payload["source_task_id"] == ""  # the audit trail keeps what the model actually sent


def _constrained_scalar_fields() -> dict:
    """Every scalar field in every intent payload schema that carries a bound
    the typed parser enforces, as ``"INTENT.path" -> {constraint: value}``."""
    from services.registry.app.society.intents import ALLOWED_INTENT_TYPES, PAYLOAD_MODELS

    scalar = {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}
    found: dict = {}

    def walk(node, defs, path):
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            walk(defs.get(str(node["$ref"]).rsplit("/", 1)[-1]) or {}, defs, path)
            return
        hit = {k: node[k] for k in scalar if k in node}
        if hit:
            found.setdefault(path, {}).update(hit)
        for arm in node.get("anyOf") or []:
            walk(arm, defs, path)
        for name, sub in (node.get("properties") or {}).items():
            walk(sub, defs, f"{path}.{name}")

    for t in ALLOWED_INTENT_TYPES:
        schema = PAYLOAD_MODELS[t].model_json_schema()
        walk(schema, schema.get("$defs") or {}, t.value)
    return found


def _rendered(doc: dict, path: str):
    node = doc
    for part in path.split(".")[1:]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def test_every_enforced_scalar_bound_is_stated_in_the_prompt():
    """A bound the model cannot see is a trap: ``parse_intent`` denies the
    intent outright and never asks again. A live Scout lost a well-formed
    CREATE_IMPROVEMENT to ``evidence.signal``'s undocumented maxLength=128
    while the prompt promised "payloads must match the documented schema".
    """
    doc = _doc_lines()
    constrained = _constrained_scalar_fields()
    assert constrained, "the payload models carry bounds; this test is pointless if not"

    missing = []
    for path, bounds in sorted(constrained.items()):
        intent = path.split(".")[0]
        if intent not in doc:
            continue
        text = _rendered(doc[intent], path)
        if not isinstance(text, str):
            missing.append(f"{path}: not rendered as a scalar ({text!r})")
            continue
        for key in ("maxLength", "maximum", "minLength", "minimum", "exclusiveMinimum", "exclusiveMaximum"):
            if key in bounds and str(bounds[key]) not in text:
                missing.append(f"{path}: {key}={bounds[key]} absent from {text!r}")
    assert not missing, "bounds enforced but never shown to the model:\n" + "\n".join(missing)


def test_the_bound_that_denied_a_live_intent_is_rendered_exactly():
    doc = _doc_lines()
    assert doc["CREATE_IMPROVEMENT"]["evidence"]["signal"] == "string(1..128 chars)"
    assert doc["CREATE_IMPROVEMENT"]["evidence"]["baseline"] == "string(<=200 chars)|null"
    assert doc["CREATE_IMPROVEMENT"]["evidence"]["sample_size"] == "integer(>=0)|null"
    assert doc["CREATE_IMPROVEMENT"]["importance"] == "integer(0..100)"
    # the same field one step later in the chain carried the same silent trap
    assert doc["REQUEST_CODE_CHANGE"]["spec"]["signal"] == "string(<=128 chars)|null"


def test_the_prompt_says_the_bounds_are_hard_and_not_retried():
    from services.registry.app.society.cognition import SYSTEM_PROMPT

    assert "string(1..128 chars)" in SYSTEM_PROMPT
    assert "NOT asked again" in SYSTEM_PROMPT


def test_structure_of_non_scalar_renderings_is_unchanged():
    """The bound annotation must not disturb the shapes the prompt already
    documented: enums, uuids, arrays, booleans and inlined nested models."""
    doc = _doc_lines()
    spec = doc["REQUEST_CODE_CHANGE"]["spec"]
    assert spec["files_allowed"] == ["string"] and spec["acceptance_tests"] == ["string"]
    assert spec["kind"] == "docs|test_fixture|code" and spec["must_compile"] == "boolean"
    assert doc["CREATE_IMPROVEMENT"]["target_scope"] == "agent|platform"
    assert doc["CREATE_IMPROVEMENT"]["source_task_id"] == "uuid|null"
    assert isinstance(doc["CREATE_IMPROVEMENT"]["evidence"], dict)


def test_the_unit_that_stranded_a_live_candidate_is_rendered_for_both_read_primitives():
    """READ_REPO_FILE counts BYTES, READ_REPO_RANGE counts LINES, and the two
    sit side by side in the same prompt block. A live Builder continuing a
    byte-truncated preview asked for "line" 12000 of a 92-line file, got an
    empty range, read again, and tripped the loop breaker with its candidate
    stranded in `requested`. A bound says how big a value may be; only the
    description can say what it MEANS.
    """
    doc = _doc_lines()
    assert "BYTES" in doc["READ_REPO_FILE"]["max_bytes"]
    for field in ("start", "end"):
        assert "LINE" in doc["READ_REPO_RANGE"][field], f"{field} must name its unit"
    assert "not a byte offset" in doc["READ_REPO_RANGE"]["start"]
    # the bound is still there: a description must never displace one
    assert "256..32000" in doc["READ_REPO_FILE"]["max_bytes"]
    assert ">=1" in doc["READ_REPO_RANGE"]["start"]


def test_a_field_without_a_description_renders_exactly_as_before():
    """Descriptions are for genuinely ambiguous contracts; every other field
    must stay as terse as it was, because this block is in every prompt."""
    from services.registry.app.society.cognition import _scalar_note

    assert _scalar_note({}) == ""
    assert _scalar_note({"description": "  "}) == ""
    assert _scalar_note({"description": "in BYTES"}) == " in BYTES"
    doc = _doc_lines()
    assert doc["WRITE_MEMORY"]["title"] == "string(1..255 chars)"
