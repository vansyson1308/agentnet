"""Deployment provider interface (no host) + operator JARVIS answers from persisted rows."""

from __future__ import annotations

import uuid

import pytest

from services.registry.app.models import (
    ChangeExperiment,
    CodeCandidate,
    CodePromotion,
    DeploymentRequest,
    DeploymentRequestStatus,
    PromotionStatus,
    SocietyEvent,
)
from services.registry.app.society import deployment as dep
from services.registry.app.society.seed import seed_society

from .conftest import auth


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _merged_promotion(db, report):
    cand = CodeCandidate(id=uuid.uuid4(), correlation_id=uuid.uuid4(), title="deployable", spec={"files_allowed": ["docs/x.md"], "acceptance_tests": ["t"], "expected_effect": "fewer failures", "signal": "task_failure_rate"}, status="ready", head_sha="a" * 40, base_sha="b" * 40, risk_tier="green", changed_files=["docs/x.md"], qa_report={"verdict": "pass"}, security_report={})
    db.add(cand)
    db.flush()
    promo = CodePromotion(id=uuid.uuid4(), candidate_id=cand.id, correlation_id=cand.correlation_id, risk_tier="green", provider="fake", status=PromotionStatus.MERGED, base_sha=cand.base_sha, candidate_sha=cand.head_sha, merged_sha="m" * 40, previous_good_sha=cand.base_sha, external_pr_number=7, external_pr_url="fake://pr/7", ci_state="passed", eligibility={"blocking": [], "human_approval_required": True, "auto_merge_enabled": False})
    db.add(promo)
    db.commit()
    return cand, promo


def test_disabled_provider_blocks_externally_never_fake_success(db, society_settings):
    report = seed_society(db)
    cand, promo = _merged_promotion(db, report)
    req = dep.request_deployment(db, settings=society_settings, provider=dep.DisabledDeploymentProvider(), environment=dep.STAGING, candidate=cand, promotion=promo, correlation_id=cand.correlation_id, target_sha=promo.merged_sha, requested_by_agent_id=None)
    db.commit()
    assert _ev(req.status) == "blocked_external" and "no deployment provider" in req.note
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "deployment.blocked").count() == 1
    again = dep.request_deployment(db, settings=society_settings, provider=dep.DisabledDeploymentProvider(), environment=dep.STAGING, candidate=cand, promotion=promo, correlation_id=cand.correlation_id, target_sha=promo.merged_sha, requested_by_agent_id=None)
    assert again.id == req.id, "idempotent per (environment, sha, promotion)"


def test_fake_provider_lifecycle_and_rollback(db, society_settings):
    report = seed_society(db)
    cand, promo = _merged_promotion(db, report)
    fake = dep.FakeDeploymentProvider()
    req = dep.request_deployment(db, settings=society_settings, provider=fake, environment=dep.STAGING, candidate=cand, promotion=promo, correlation_id=cand.correlation_id, target_sha=promo.merged_sha, requested_by_agent_id=None)
    db.commit()
    assert _ev(req.status) == "succeeded" and req.external_ref.startswith("fake-deploy-")
    rb = dep.request_deployment(db, settings=society_settings, provider=fake, environment=dep.STAGING, candidate=cand, promotion=promo, correlation_id=cand.correlation_id, target_sha=promo.previous_good_sha, requested_by_agent_id=None, kind="rollback", rollback_of=req)
    db.commit()
    assert _ev(rb.status) == "rolled_back" and rb.rollback_of == req.id
    assert dep.observe_requests(db, fake) == 0
    assert [c[0] for c in fake.calls] == ["request_staging", "request_rollback"]


def test_production_is_recorded_as_refused_and_never_executed(db, society_settings):
    report = seed_society(db)
    cand, promo = _merged_promotion(db, report)
    fake = dep.FakeDeploymentProvider()
    req = dep.request_deployment(db, settings=society_settings, provider=fake, environment=dep.PRODUCTION, candidate=cand, promotion=promo, correlation_id=cand.correlation_id, target_sha=promo.merged_sha, requested_by_agent_id=None)
    db.commit()
    assert _ev(req.status) == "refused" and "hard OFF" in req.note
    assert fake.calls == [], "no provider call is ever made for production"
    assert db.query(SocietyEvent).filter(SocietyEvent.event_type == "deployment.refused").count() == 1
    assert society_settings.production_deploy_enabled is False


def test_staging_intents_are_gated_and_need_a_merged_promotion(db, SessionLocal, society_settings, grants_with_no_cooldown, monkeypatch):
    """REQUEST_STAGING_DEPLOY/EVALUATION: denied while the flag is off; with the
    flag on, a non-merged promotion is refused; a merged one yields BLOCKED_EXTERNAL."""
    import asyncio

    from services.registry.app.models import AgentIntent
    from services.registry.app.society.cognition import FakeModel
    from services.registry.app.society.config import SocietySettings, reset_settings_cache
    from services.registry.app.society.events import emit_event
    from services.registry.app.society.worker import SocietyWorker

    report = seed_society(db)
    grants_with_no_cooldown()
    cand, promo = _merged_promotion(db, report)
    script = {"governor": [
        {"decision_summary": "stage", "intents": [{"type": "REQUEST_STAGING_EVALUATION", "payload": {"promotion_id": str(promo.id)}}], "sleep_for_seconds": 1},
        {"decision_summary": "stage", "intents": [{"type": "REQUEST_STAGING_EVALUATION", "payload": {"promotion_id": str(promo.id)}}], "sleep_for_seconds": 1},
    ]}
    model = FakeModel(script)
    emit_event(db, event_type="t.stage", idempotency_key="t-stage-1")
    db.commit()
    w = SocietyWorker(SessionLocal, settings=society_settings, model=model, worker_id="w", telemetry_enabled=False)
    w.routing = {"t.stage": ["governor"]}
    asyncio.run(w.run_until_idle(max_cycles=3))
    row = db.query(AgentIntent).filter(AgentIntent.intent_type == "REQUEST_STAGING_EVALUATION").one()
    assert _ev(row.execution_status) == "denied" and "STAGING_DEPLOY_ENABLED" in row.policy_reason
    monkeypatch.setenv("SOCIETY_STAGING_DEPLOY_ENABLED", "true")
    reset_settings_cache()
    s2 = SocietySettings()
    emit_event(db, event_type="t.stage", idempotency_key="t-stage-2")
    db.commit()
    w = SocietyWorker(SessionLocal, settings=s2, model=model, worker_id="w", telemetry_enabled=False)
    w.routing = {"t.stage": ["governor"]}
    asyncio.run(w.run_until_idle(max_cycles=3))
    rows = db.query(AgentIntent).filter(AgentIntent.intent_type == "REQUEST_STAGING_EVALUATION").order_by(AgentIntent.created_at).all()
    assert _ev(rows[-1].execution_status) == "executed" and rows[-1].result["result"]["status"] == "blocked_external"
    assert db.query(DeploymentRequest).count() == 1
    reset_settings_cache()


def test_jarvis_answers_self_development_questions_from_rows(api_client, db, society_settings, user_token):
    report = seed_society(db)
    cand, promo = _merged_promotion(db, report)
    exp = ChangeExperiment(id=uuid.uuid4(), candidate_id=cand.id, promotion_id=promo.id, correlation_id=cand.correlation_id, status="fail", decision="fail", confidence="high", rollback_recommended=True, hard_gate_results=[{"gate": "no_test_regression", "passed": False, "detail": "x"}], criteria_snapshot={"version": "fitness-v1"}, recommendation={"recommendation": "rollback"})
    db.add(exp)
    db.commit()
    _, token = user_token(role="operator")
    r = api_client.get("/v1/society/ask", params={"q": "what is AgentNet trying to improve"}, headers=auth(token))
    assert r.status_code == 200 and "improving" in r.json()["answers"]
    r = api_client.get("/v1/society/ask", params={"q": f"why was candidate {cand.id} created and what files did the builder inspect"}, headers=auth(token))
    body = r.json()["answers"]["why_candidate"]["candidates"][0]
    assert body["because"]["expected_effect"] == "fewer failures" and body["repository_reads"] == []
    r = api_client.get("/v1/society/ask", params={"q": f"why can PR {promo.id} not merge"}, headers=auth(token))
    p = r.json()["answers"]["promotions"]["promotions"][0]
    assert p["status"] == "merged" and p["pr"]["number"] == 7
    r = api_client.get("/v1/society/ask", params={"q": f"what is the fitness result for {exp.id} and what should be rolled back"}, headers=auth(token))
    f = r.json()["answers"]["fitness"]
    assert f["experiments"][0]["decision"] == "fail" and f["rollback_recommended"][0]["failed_gates"] == ["no_test_regression"]
    r = api_client.get("/v1/society/ask", params={"q": "how much autonomous engineering budget remains"}, headers=auth(token))
    b = r.json()["answers"]["engineering_budget"]["engineering_budget"]
    assert b["max_open_autonomous_prs"] == 3 and b["auto_merge_enabled"] is False
    # non-operators get nothing
    _, user_tok = user_token(role=None)
    assert api_client.get("/v1/society/ask", params={"q": "fitness"}, headers=auth(user_tok)).status_code in (401, 403)
    assert api_client.get("/v1/society/ask", params={"q": "fitness"}).status_code in (401, 403)
