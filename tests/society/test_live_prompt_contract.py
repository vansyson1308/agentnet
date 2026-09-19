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
    assert doc["SUBMIT_CODE_CANDIDATE"]["edits"] == [{"path": "string", "content": "string"}]
    evidence = doc["CREATE_IMPROVEMENT"]["evidence"]
    assert set(evidence) >= {"signal", "baseline", "observed", "window", "sample_size", "actionable_reason"}
    assert doc["SEND_MESSAGE"]["to_agent"] == "string|null", "Optional[str] says it may be null"
    assert doc["WRITE_MEMORY"]["source_task_id"] == "uuid|null" and doc["SEND_MESSAGE"]["thread_id"] == "uuid|null", "optional uuid references are documented as uuid-or-null, never a free string"
    assert doc["REQUEST_CODE_CHANGE"]["proposal_id"] == "uuid|null" and doc["READ_REPO_FILE"]["candidate_id"] == "uuid|null"
    assert doc["REVIEW_IMPROVEMENT"]["proposal_id"] == "uuid", "required uuid references are documented as uuid"
    assert doc["WRITE_MEMORY"]["title"] == "string" and doc["WRITE_MEMORY"]["importance"] == "integer", "required scalars stay bare"
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
