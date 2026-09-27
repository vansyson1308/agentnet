"""Fixtures for the Maintenance OS tests (ADR-0010).

They reuse the Society's real-PostgreSQL fixtures (row locks, partial unique
indexes, CHECK constraints and triggers are the point -- a mock proves none of
them) and add:

* ``product_repo`` -- a throw-away git repository that mirrors the REAL
  layout of the parts a repair touches (``services/dashboard/...``) with one
  planted presentation defect and the trusted verification test that judges
  it, so the real contract registry and the real risk classifier apply;
* ``mset`` / ``sset`` -- maintenance + society settings pointed at it;
* ``kernel`` -- a MaintenanceKernel with an injectable scripted model;
* helpers to raise incidents from structural observations.

No real model is ever called here. Scripted activity output is labelled
``scripted`` on every row it produces (NO FAKE AUTONOMY).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

# The real-Postgres fixtures (db, db_factory, SessionLocal, ...) come from
# tests/society/conftest.py, which this package inherits.

PAGE = "services/dashboard/app/templates/page.html"
VERIFY = "services/dashboard/tests/test_public_surface.py"
MAIN = "services/dashboard/app/main.py"

PAGE_BROKEN = """<html><body>
<h1>AgentNet Marketplace</h1>
<ul class="caps">
  <li class="cap">{'name': 'echo', 'price': 1}</li>
</ul>
<p class="footer">Welcome</p>
</body></html>
"""

VERIFY_TEST = '''"""Trusted verification test (fixture): capability badges are human readable."""
import pathlib

PAGE = pathlib.Path(__file__).resolve().parents[1] / "app" / "templates" / "page.html"


def test_page_has_heading():
    assert "<h1>" in PAGE.read_text()


def test_capabilities_are_human_readable():
    text = PAGE.read_text()
    assert "{'" not in text and "':" not in text, "raw structured value rendered"
'''

MAIN_PY = '''"""Fixture dashboard app (routes only)."""


def marketplace():
    return "page.html"
'''


def _git(args, cwd):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=True).stdout


def make_product_repo(root: pathlib.Path) -> pathlib.Path:
    repo = root / "product"
    for rel, body in {
        PAGE: PAGE_BROKEN,
        VERIFY: VERIFY_TEST,
        MAIN: MAIN_PY,
        "services/dashboard/app/__init__.py": "",
        "services/dashboard/app/static/site.css": "body { color: #111; }\n",
        "tests/test_public_surface_contract.py": "def test_contract_file_is_json():\n    assert True\n",
        "services/payment/app/wallet.py": "BALANCE = 0\n",
        "pytest.ini": "[pytest]\naddopts = -q -p no:cacheprovider\n",
        "README.md": "# product fixture\n",
    }.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    _git(["init", "-q", "-b", "main"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "product fixture with one planted presentation defect"], repo)
    return repo


@pytest.fixture
def product_repo(tmp_path):
    return make_product_repo(tmp_path)


@pytest.fixture
def sset(tmp_path, product_repo, monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    for k, v in {
        "SOCIETY_RUNTIME_ENABLED": "true",
        "SOCIETY_AUTONOMOUS_CODE_ENABLED": "true",
        "SOCIETY_MODEL_PROVIDER": "scripted",
        "SOCIETY_PROMOTION_PROVIDER": "fake",
        "SOCIETY_REPO_ROOT": str(product_repo),
        "SOCIETY_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "SOCIETY_QA_TEST_TIMEOUT_SECONDS": "120",
        "SOCIETY_FITNESS_TEST_TIMEOUT_SECONDS": "120",
        "SOCIETY_PROMOTION_POLL_INTERVAL_SECONDS": "0",
        "SOCIETY_AUTO_MERGE_ENABLED": "true",
        "SOCIETY_MAX_AUTONOMOUS_MERGES_PER_DAY": "5",
    }.items():
        monkeypatch.setenv(k, v)
    reset_settings_cache()
    yield SocietySettings()
    reset_settings_cache()


@pytest.fixture
def mset(monkeypatch):
    from services.registry.app.maintenance.config import MaintenanceSettings

    for k, v in {
        "MAINTENANCE_AUTONOMY_ENABLED": "true",
        "MAINTENANCE_MONITORING_ENABLED": "true",
        "MAINTENANCE_COGNITION_ENABLED": "true",
        "MAINTENANCE_GREEN_PROMOTION_ENABLED": "true",
        "MAINTENANCE_GREEN_RELEASE_ENABLED": "true",
        "MAINTENANCE_ATTESTATION_KEY": "test-attestation-hmac-key-not-a-secret",
    }.items():
        monkeypatch.setenv(k, v)
    return MaintenanceSettings()


def t0() -> datetime:
    return datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return t0() + timedelta(minutes=minutes)


def violation(**over):
    from services.registry.app.maintenance.incidents import Violation
    from services.registry.app.maintenance.taxonomy import IncidentClass, Priority, Severity

    base = dict(
        target="production",
        incident_class=IncidentClass.UI_RENDERING,
        desired_state_ref="marketplace",
        failure="raw_structured_value",
        severity=Severity.MAJOR,
        base_priority=Priority.P2,
        source="browser_probe",
        collector_version="browser/1",
        sli="browser_journey",
        path="/marketplace",
        payload={"rule": "raw_structured_value", "count": 1, "selector_class": "li.cap"},
    )
    base.update(over)
    return Violation(**base)


def raise_incident(db, mset, *, times: int = 2, start: float = 0.0, **over):
    from services.registry.app.maintenance import incidents as inc

    v = violation(**over)
    out = None
    for i in range(times):
        out, _ = inc.ingest_violation(db, mset, v, now=at(start + i))
    out.provenance = {**(out.provenance or {}), "contract": over.get("contract", "public_surface")}
    db.commit()
    return out


# ── scripted cognitive workers (deterministic; never live evidence) ─────────


def green_repair_script(*, review="pass", security="pass", files=None):
    """Diagnose -> design -> patch (apply, test, submit) -> review -> security."""
    files = files or [PAGE]

    def script(messages):
        system = messages[0]["content"]
        turns = sum(1 for m in messages if m["role"] == "assistant")
        if "DiagnoseIncident" in system:
            if turns == 0:
                return {"action": "read_range", "args": {"path": PAGE}}
            return {"action": "submit", "result": {"root_cause": "the template prints the capability dict instead of its name", "suspected_files": [PAGE], "evidence": ["li.cap renders a dict"], "confidence": "high"}}
        if "DesignRepair" in system:
            return {"action": "submit", "result": {"root_cause": "template renders the raw capability object", "approach": "render the capability name in the badge", "files_allowed": files, "acceptance_tests": [VERIFY], "contract_refs": ["public_surface"]}}
        if "AuthorPatch" in system:
            if turns == 0:
                return {"action": "apply_patch", "args": {"files": [{"path": PAGE, "operations": [{"op": "replace_exact", "old": "{'name': 'echo', 'price': 1}", "new": "echo"}]}]}}
            if turns == 1:
                return {"action": "run_tests", "args": {"targets": [VERIFY]}}
            return {"action": "submit", "result": {"summary": "render the capability name"}}
        if "ReviewPatch" in system:
            return {"action": "submit", "result": {"verdict": review, "findings": [], "summary": "fixes the cause"}}
        if "SecurityReview" in system:
            return {"action": "submit", "result": {"verdict": security, "findings": [], "summary": "template text only"}}
        if "ExplainEscalation" in system:
            return {"action": "submit", "result": {"summary": "prepared a repair", "owner_decision": "approve the PR", "risk_explanation": "touches product code"}}
        return {"action": "submit", "result": {}}

    return script


@pytest.fixture
def kernel_factory(SessionLocal, mset, sset):
    from services.registry.app.maintenance.activities import ScriptedActivityModel
    from services.registry.app.maintenance.reconciler import MaintenanceKernel

    def _make(script=None, *, worker_id="k1", settings=None, model=None):
        m = model if model is not None else ScriptedActivityModel(script or green_repair_script())
        return MaintenanceKernel(SessionLocal, settings=settings or mset, society_settings=sset, model=m, worker_id=worker_id, activity_timeout=120)

    return _make


def drive_promotion(SessionLocal, sset, provider, *, rounds: int = 6):
    """Run the (unchanged) promotion controller and fitness engine."""
    from services.registry.app.society import fitness as fit
    from services.registry.app.society import promotion as pm

    for _ in range(rounds):
        pm.process_promotions(SessionLocal, settings=sset, provider=provider, worker_id="promo-test")
        fit.process_experiments(SessionLocal, settings=sset, worker_id="fit-test")
