"""World telemetry producers, Scout evidence quality under an anomaly storm,
change budgets and anti-busywork."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from services.registry.app.models import AgentIntent, AgentRun, AgentRunStatus, CodeCandidate, ImprovementProposal, ProposalSource, SocietyEvent
from services.registry.app.society import telemetry
from services.registry.app.society.cognition import FakeModel, ScriptedRoleModel
from services.registry.app.society.events import EventType, emit_event
from services.registry.app.society.seed import seed_society
from services.registry.app.society.worker import SocietyWorker


def _ev(v):
    return v.value if hasattr(v, "value") else v


# ── telemetry ──────────────────────────────────────────────────────────


def test_quiet_system_produces_no_telemetry(db, society_settings):
    seed_society(db)
    assert telemetry.produce_anomalies(db, society_settings) == 0
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.PLATFORM_METRIC_ANOMALY).count() == 0


def test_dead_run_anomaly_has_threshold_cooldown_and_evidence(db, society_settings):
    report = seed_society(db)
    ev = emit_event(db, event_type="t.dead")
    db.commit()
    for i in range(6):
        e = emit_event(db, event_type=f"t.dead.{i}")
        db.flush()
        db.add(AgentRun(id=uuid.uuid4(), agent_id=report.agents["scout"], event_id=e.id, role="scout", status=AgentRunStatus.DEAD if i < 4 else AgentRunStatus.COMPLETED, correlation_id=e.correlation_id, context_summary={}, intents_count=0, cost_usd=0, attempt=3))
    db.commit()
    now = datetime.now(timezone.utc)
    assert telemetry.produce_anomalies(db, society_settings, now=now, only=["run_dead_rate"]) == 1
    row = db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.PLATFORM_METRIC_ANOMALY).one()
    p = row.payload
    assert p["metric"] == "run_dead_rate" and p["sample_size"] == 6 and p["threshold"] == 0.2 and p["window_seconds"] == 3600 and p["source"] == "telemetry"
    # cooldown: the same anomaly in the same bucket is not re-emitted; a later bucket is
    assert telemetry.produce_anomalies(db, society_settings, now=now, only=["run_dead_rate"]) == 0
    assert telemetry.produce_anomalies(db, society_settings, now=now + timedelta(hours=2), only=["run_dead_rate"]) == 0, "runs are outside the window now"
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.PLATFORM_METRIC_ANOMALY).count() == 1


def test_repeated_proposals_and_candidate_rejections_are_signals(db, society_settings, make_agent):
    seed_society(db)
    a = make_agent("Spammer")
    for i in range(3):
        db.add(ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=a.id, source=ProposalSource.AUDIT, title="Improve: same thing", problem="p", proposed_change="c", status="PROPOSED", target_scope="platform", importance=50))
    for i in range(3):
        db.add(CodeCandidate(id=uuid.uuid4(), correlation_id=uuid.uuid4(), title=f"c{i}", spec={}, status="rejected"))
    db.commit()
    n = telemetry.produce_anomalies(db, society_settings, only=["repeated_proposals", "candidate_rejection_rate"])
    metrics = {e.payload["metric"] for e in db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.PLATFORM_METRIC_ANOMALY).all()}
    assert n == 2 and metrics == {"repeated_proposals", "candidate_rejection_rate"}


# ── Scout quality under a storm ────────────────────────────────────────


@pytest.mark.timeout(300)
def test_anomaly_storm_yields_one_workstream(db, SessionLocal, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    for i in range(12):
        emit_event(db, event_type=EventType.PLATFORM_METRIC_ANOMALY, payload={"metric": "latency_p99", "value": 900 + i, "threshold": 500, "baseline": 200, "window_seconds": 3600, "sample_size": 30, "description": "p99 latency above threshold", "severity_score": 65}, idempotency_key=f"storm-{i}")
    db.commit()
    w = SocietyWorker(SessionLocal, settings=society_settings, model=ScriptedRoleModel(), worker_id="w", telemetry_enabled=False)
    w.routing = {EventType.PLATFORM_METRIC_ANOMALY: ["scout"]}
    asyncio.run(w.run_until_idle(max_cycles=20))
    props = db.query(ImprovementProposal).all()
    assert len(props) == 1 and props[0].title == "Improve: latency_p99"
    created = db.query(AgentIntent).filter(AgentIntent.intent_type == "CREATE_IMPROVEMENT").all()
    assert len(created) == 1 and created[0].payload["evidence"]["observed"] == "900" and created[0].payload["evidence"]["sample_size"] == 30
    assert db.query(AgentRun).filter(AgentRun.role == "scout").count() == 12


def test_signal_driven_proposal_without_evidence_is_refused(db, SessionLocal, society_settings, grants_with_no_cooldown):
    seed_society(db)
    grants_with_no_cooldown()
    script = {"scout": [{"decision_summary": "lazy", "intents": [{"type": "CREATE_IMPROVEMENT", "payload": {"title": "Improve: x", "problem": "p", "proposed_change": "c"}}], "sleep_for_seconds": 1}]}
    emit_event(db, event_type=EventType.PLATFORM_METRIC_ANOMALY, payload={"metric": "x"}, idempotency_key="lazy-1")
    db.commit()
    w = SocietyWorker(SessionLocal, settings=society_settings, model=FakeModel(script), worker_id="w", telemetry_enabled=False)
    w.routing = {EventType.PLATFORM_METRIC_ANOMALY: ["scout"]}
    asyncio.run(w.run_until_idle(max_cycles=3))
    row = db.query(AgentIntent).filter(AgentIntent.intent_type == "CREATE_IMPROVEMENT").one()
    assert _ev(row.execution_status) == "failed" and "evidence" in row.error
    assert db.query(ImprovementProposal).count() == 0


# ── change budget + anti-busywork ──────────────────────────────────────


def _request_change(db, SessionLocal, settings, spec, *, proposal_id=None, title="t"):
    report = seed_society(db)
    for g in db.query(__import__("services.registry.app.models", fromlist=["AgentCapabilityGrant"]).AgentCapabilityGrant).all():
        g.wake_cooldown_seconds = 0
    if proposal_id is None:
        prop = ImprovementProposal(id=uuid.uuid4(), proposed_by_agent_id=report.agents["scout"], source=ProposalSource.AUDIT, title="Improve: thing", problem="p", proposed_change="c", status="APPROVED", target_scope="platform", importance=60)
        db.add(prop)
        db.commit()
        proposal_id = prop.id
    payload = {"title": title, "spec": spec}
    if proposal_id != "none":
        payload["proposal_id"] = str(proposal_id)
    script = {"architect": [{"decision_summary": "design", "intents": [{"type": "REQUEST_CODE_CHANGE", "payload": payload}], "sleep_for_seconds": 1}]}
    emit_event(db, event_type="t.design", idempotency_key=f"t-design-{uuid.uuid4()}")
    db.commit()
    w = SocietyWorker(SessionLocal, settings=settings, model=FakeModel(script), worker_id="w", telemetry_enabled=False)
    w.routing = {"t.design": ["architect"]}
    asyncio.run(w.run_until_idle(max_cycles=3))
    return db.query(AgentIntent).filter(AgentIntent.intent_type == "REQUEST_CODE_CHANGE").order_by(AgentIntent.created_at.desc()).first()


def test_unlinked_or_effectless_code_requests_are_refused(db, SessionLocal, society_settings):
    row = _request_change(db, SessionLocal, society_settings, {"description": "d", "files_allowed": ["docs/x.md"], "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"]}, proposal_id="none")
    assert _ev(row.execution_status) == "failed" and "proposal" in row.error
    row = _request_change(db, SessionLocal, society_settings, {"description": "d", "files_allowed": ["services/x.py"], "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"], "kind": "code"})
    assert _ev(row.execution_status) == "failed" and "expected_effect" in row.error
    row = _request_change(db, SessionLocal, society_settings, {"description": "d", "files_allowed": ["docs/x.md"], "acceptance_tests": []})
    assert _ev(row.execution_status) == "failed" and "acceptance" in row.error
    assert db.query(CodeCandidate).count() == 0


def test_files_budget_and_daily_candidate_caps(db, SessionLocal, temp_repo, tmp_path, monkeypatch):
    from services.registry.app.society.config import SocietySettings, reset_settings_cache

    monkeypatch.setenv("SOCIETY_RUNTIME_ENABLED", "true")
    monkeypatch.setenv("SOCIETY_AUTONOMOUS_CODE_ENABLED", "true")
    monkeypatch.setenv("SOCIETY_REPO_ROOT", str(temp_repo))
    monkeypatch.setenv("SOCIETY_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("SOCIETY_MAX_FILES_PER_CANDIDATE", "2")
    monkeypatch.setenv("SOCIETY_MAX_AUTONOMOUS_CANDIDATES_PER_DAY", "1")
    reset_settings_cache()
    s = SocietySettings()
    row = _request_change(db, SessionLocal, s, {"description": "d", "files_allowed": ["docs/a.md", "docs/b.md", "docs/c.md"], "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"]})
    assert _ev(row.execution_status) == "failed" and "change budget" in row.error
    # These two are CONFORMING docs specs, so the only thing that can refuse the
    # second one is the daily candidate cap this test is about.
    row = _request_change(db, SessionLocal, s, {"description": "d", "files_allowed": ["docs/society/candidates/a.md"], "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"], "expected_effect": "records the finding"}, title="first")
    assert _ev(row.execution_status) == "executed"
    row = _request_change(db, SessionLocal, s, {"description": "d", "files_allowed": ["docs/society/candidates/b.md"], "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"], "expected_effect": "records the finding"}, title="second")
    assert _ev(row.execution_status) == "failed" and "autonomous candidates today" in row.error
    reset_settings_cache()


def test_noop_format_only_and_duplicate_candidates_are_rejected(db, SessionLocal, society_settings, temp_repo, grants_with_no_cooldown):
    from services.registry.app.models import CodeCandidateStatus

    report = seed_society(db)
    grants_with_no_cooldown()
    doc = "docs/society/candidates/x.md"
    good = "# X\n\n## Problem\n\np\n\n## Proposed change\n\nc\n\n## Evidence\n\ne\n\n## Verification\n\nv\n"
    (temp_repo / "docs" / "society" / "candidates" / "x.md").write_text(good)
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=temp_repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "existing"], cwd=temp_repo, check=True, capture_output=True)

    def submit(edits, title):
        cand = CodeCandidate(id=uuid.uuid4(), correlation_id=uuid.uuid4(), title=title, spec={"files_allowed": [doc], "acceptance_tests": ["tests/society/acceptance/test_candidate_docs.py"]}, status=CodeCandidateStatus.REQUESTED, requested_by_agent_id=report.agents["architect"])
        db.add(cand)
        db.commit()
        script = {"builder": [{"decision_summary": "s", "intents": [{"type": "SUBMIT_CODE_CANDIDATE", "payload": {"candidate_id": str(cand.id), "edits": edits, "summary": "s"}}], "sleep_for_seconds": 1}]}
        emit_event(db, event_type="t.build", payload={"candidate_id": str(cand.id)}, idempotency_key=f"t-build-{cand.id}")
        db.commit()
        w = SocietyWorker(SessionLocal, settings=society_settings, model=FakeModel(script), worker_id="w", telemetry_enabled=False)
        w.routing = {"t.build": ["builder"]}
        asyncio.run(w.run_until_idle(max_cycles=3))
        db.refresh(cand)
        return cand

    noop = submit([{"path": doc, "content": good}], "noop")
    assert _ev(noop.status) == "rejected" and "no-op" in noop.error
    fmt = submit([{"path": doc, "content": good.replace("\n\n", "\n\n\n")}], "format")
    assert _ev(fmt.status) == "rejected" and "format-only" in fmt.error
    real = submit([{"path": doc, "content": good.replace("p\n", "problem statement\n")}], "real")
    assert _ev(real.status) == "built"
    dup = submit([{"path": doc, "content": good.replace("p\n", "problem statement\n")}], "dup")
    assert _ev(dup.status) == "rejected" and "duplicate candidate" in dup.error
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == EventType.CODE_CANDIDATE_REJECTED).count() == 3


def test_file_edit_content_is_byte_exact():
    """Regression: the shared strict-payload config strips whitespace, which
    silently dropped the trailing newline of every submitted file and turned
    an identical resubmission into a whitespace-only diff."""
    from services.registry.app.society.intents import FileEdit

    edit = FileEdit(path="  docs/x.md ", content="line\n\n")
    assert edit.path == "docs/x.md"
    assert edit.content == "line\n\n"
    with pytest.raises(ValueError):
        FileEdit(path="   ", content="x")
