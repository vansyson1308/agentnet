"""Structural secret boundary: no model context, event, memory, run row or
cognition module can carry a GitHub / model / infrastructure credential."""

from __future__ import annotations

import json
import os
import pathlib
import re
import uuid

import pytest

SOCIETY = pathlib.Path(__file__).resolve().parent.parent.parent / "services" / "registry" / "app" / "society"
SENTINELS = {
    "SOCIETY_GITHUB_TOKEN": "ghs_SENTINEL_github_token_value_000",
    "SOCIETY_MODEL_API_KEY": "sk-SENTINEL-model-key-value-000000",
    "POSTGRES_PASSWORD": "pg-SENTINEL-password",
    "REDIS_PASSWORD": "redis-SENTINEL-password",
    "JWT_SECRET_KEY": "jwt-SENTINEL-secret-key-value-000000",
    "DEPLOY_CREDENTIAL": "deploy-SENTINEL-credential",
}


def test_github_token_env_is_read_only_inside_the_github_provider():
    hits = {}
    for f in SOCIETY.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        if "SOCIETY_GITHUB_TOKEN" in text:
            hits[f.name] = text.count("SOCIETY_GITHUB_TOKEN")
    assert set(hits) <= {"promotion_github.py", "risk.py"}, hits  # risk.py only names it in a NEVER pattern
    src = (SOCIETY / "promotion_github.py").read_text()
    assert "os.getenv(TOKEN_ENV" in src and "self.token" not in src and "self._token =" not in src


def test_cognition_and_context_modules_never_touch_github_or_deploy_credentials():
    for name in ("cognition.py", "context.py", "worker.py", "executor.py", "policy.py", "fitness.py", "promotion.py", "router.py", "telemetry.py", "repo_intel.py"):
        text = (SOCIETY / name).read_text()
        assert "SOCIETY_GITHUB_TOKEN" not in text and "GITHUB_TOKEN" not in text, name
        assert "x-access-token" not in text, name


@pytest.mark.timeout(300)
def test_context_and_persisted_rows_never_contain_secret_values(db, SessionLocal, society_settings, grants_with_no_cooldown, monkeypatch, temp_repo):
    """Set every credential env var to a sentinel, run a loop with reads and a
    promotion request, then scan everything the model saw and everything that
    was persisted."""
    import asyncio

    from sqlalchemy import text

    from services.registry.app.society.cognition import FakeModel
    from services.registry.app.society.events import emit_event
    from services.registry.app.society.promotion import FakePromotionProvider
    from services.registry.app.society.seed import seed_society
    from services.registry.app.society.worker import SocietyWorker

    for k, v in SENTINELS.items():
        monkeypatch.setenv(k, v)
    seed_society(db)
    grants_with_no_cooldown()
    (pathlib.Path(temp_repo) / "notes.md").write_text("ordinary file\n")
    script = {
        "architect": [
            {"decision_summary": "look", "intents": [{"type": "SEARCH_REPO", "payload": {"pattern": "candidate"}}, {"type": "LIST_REPO_TREE", "payload": {"path": "", "depth": 2}}, {"type": "WRITE_MEMORY", "payload": {"title": "m", "content": "c", "scope": "agent"}}], "sleep_for_seconds": 1},
            {"decision_summary": "x", "intents": [], "sleep_for_seconds": 1},
        ]
    }
    model = FakeModel(script)
    emit_event(db, event_type="t.secret", payload={"x": 1}, idempotency_key="t-secret")
    db.commit()
    worker = SocietyWorker(SessionLocal, settings=society_settings, model=model, worker_id="w", promotion_provider=FakePromotionProvider(), telemetry_enabled=False)
    worker.routing = {"t.secret": ["architect"], "repo.read.result": ["architect"]}
    asyncio.run(worker.run_until_idle(max_cycles=6))
    assert model.calls, "the model ran"
    for ctx in model.calls:
        blob = ctx.canonical_json()
        for v in SENTINELS.values():
            assert v not in blob
    dump = []
    for table in ("society_events", "agent_runs", "agent_intents", "memory_items", "code_candidates", "code_promotions", "change_experiments"):
        rows = db.execute(text(f"SELECT row_to_json(t) FROM {table} t")).fetchall()
        dump.extend(json.dumps(r[0], default=str) for r in rows)
    joined = "\n".join(dump)
    for v in SENTINELS.values():
        assert v not in joined


def test_cognition_works_without_the_github_token(monkeypatch):
    """The cognition process must not depend on the promotion credential."""
    monkeypatch.delenv("SOCIETY_GITHUB_TOKEN", raising=False)
    from services.registry.app.society.config import SocietySettings, reset_settings_cache
    from services.registry.app.society.cognition import get_model

    reset_settings_cache()
    s = SocietySettings()
    assert get_model(s).provider == "scripted"
    assert not any("GITHUB" in k for k in vars(s)), "settings hold no GitHub credential field"
    from services.registry.app.society.promotion_github import GitHubPromotionProvider
    from services.registry.app.society.promotion import ProviderUnavailable

    monkeypatch.setenv("SOCIETY_GITHUB_REPOSITORY", "example/repo")
    reset_settings_cache()
    p = GitHubPromotionProvider(SocietySettings())
    with pytest.raises(ProviderUnavailable):
        p.get_pr_state(type("P", (), {"external_pr_number": 1})())
    reset_settings_cache()


def test_no_hardcoded_provider_key_shapes_in_society_package():
    key_shapes = re.compile(r"\bsk-[A-Za-z0-9]{20,}\b|\bghp_[A-Za-z0-9]{20,}\b|\bghs_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")
    for f in SOCIETY.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not key_shapes.search(text), f.name
