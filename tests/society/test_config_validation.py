"""Society settings fail fast on dangerous or impossible configuration."""

from __future__ import annotations

import pytest

from services.registry.app.society import config as cfg


def _settings(monkeypatch, **env):
    for k in list(env):
        monkeypatch.setenv(k, env[k])
    cfg.reset_settings_cache()
    return cfg.SocietySettings()


def test_defaults_are_valid(monkeypatch):
    monkeypatch.delenv("SOCIETY_RUNTIME_ENABLED", raising=False)
    assert cfg.validate_settings(_settings(monkeypatch, ENVIRONMENT="development")) == []


@pytest.mark.parametrize("flag", ["SOCIETY_RUNTIME_ENABLED", "SOCIETY_AUTONOMOUS_CODE_ENABLED", "SOCIETY_STAGING_DEPLOY_ENABLED"])
def test_production_refuses_society_activation(monkeypatch, flag):
    with pytest.raises(cfg.SocietyConfigError):
        _settings(monkeypatch, ENVIRONMENT="production", **{flag: "true"})
    # the same flags are fine in staging
    s = _settings(monkeypatch, ENVIRONMENT="staging", **{flag: "true", "SOCIETY_MODEL_PROVIDER": "scripted"})
    assert cfg.validate_settings(s) == []


def test_negative_budget_and_short_lease_are_refused_outside_development(monkeypatch):
    # development clamps with a warning so `docker compose up` never dies on a typo...
    s = _settings(monkeypatch, ENVIRONMENT="development", SOCIETY_DAILY_MODEL_BUDGET="-1")
    assert s.daily_model_budget_usd == 0
    # ...staging/production refuse to start with money or timing limits they did not set
    with pytest.raises(cfg.SocietyConfigError):
        _settings(monkeypatch, ENVIRONMENT="staging", SOCIETY_DAILY_MODEL_BUDGET="-1")
    with pytest.raises(cfg.SocietyConfigError):
        _settings(monkeypatch, ENVIRONMENT="staging", SOCIETY_MODEL_TIMEOUT_SECONDS="abc")
    with pytest.raises(cfg.SocietyConfigError):
        _settings(monkeypatch, ENVIRONMENT="staging", SOCIETY_RUN_LEASE_SECONDS="1")
    with pytest.raises(cfg.SocietyConfigError):
        _settings(monkeypatch, ENVIRONMENT="development", SOCIETY_RUN_LEASE_SECONDS="5", SOCIETY_MODEL_TIMEOUT_SECONDS="45")


def test_live_provider_outside_development_needs_https_and_a_key(monkeypatch):
    with pytest.raises(cfg.SocietyConfigError) as exc:
        _settings(monkeypatch, ENVIRONMENT="staging", SOCIETY_MODEL_PROVIDER="openai_compatible", SOCIETY_MODEL_BASE_URL="http://model.internal/v1", SOCIETY_MODEL_API_KEY="")
    assert "https" in str(exc.value)
    s = _settings(monkeypatch, ENVIRONMENT="staging", SOCIETY_MODEL_PROVIDER="openai_compatible", SOCIETY_MODEL_BASE_URL="https://model.example/v1", SOCIETY_MODEL_API_KEY="placeholder-for-test")
    assert cfg.validate_settings(s) == []
    cfg.reset_settings_cache()


def test_credential_is_required_only_in_the_model_calling_process(monkeypatch):
    """Split deployments (Railway): the registry API mirrors the non-secret live
    flags so /v1/society/status tells the truth; only the society worker — the
    process that calls the model — must hold and fail fast on the credential."""
    from services.registry.app.society import worker as worker_mod

    s = _settings(monkeypatch, ENVIRONMENT="staging", SOCIETY_RUNTIME_ENABLED="true", SOCIETY_MODEL_PROVIDER="openai_compatible", SOCIETY_MODEL_BASE_URL="https://model.example/v1", SOCIETY_MODEL_API_KEY="")
    assert cfg.validate_settings(s) == [], "the API process mirrors the flags without a credential"
    assert s.public_flags()["runtime_enabled"] is True and s.public_flags()["model_provider"] == "openai_compatible"
    assert s.public_dict()["model_api_key"] == ""
    worker_problems = worker_mod.startup_problems(s)
    assert len(worker_problems) == 1 and "SOCIETY_MODEL_API_KEY" in worker_problems[0] and "worker" in worker_problems[0]
    assert cfg.validate_settings(s, model_caller=True) == worker_problems
    with_key = _settings(monkeypatch, ENVIRONMENT="staging", SOCIETY_MODEL_PROVIDER="openai_compatible", SOCIETY_MODEL_BASE_URL="https://model.example/v1", SOCIETY_MODEL_API_KEY="placeholder-for-test")
    assert worker_mod.startup_problems(with_key) == []
    # an http endpoint is refused everywhere, key or not
    with pytest.raises(cfg.SocietyConfigError):
        _settings(monkeypatch, ENVIRONMENT="staging", SOCIETY_MODEL_PROVIDER="openai_compatible", SOCIETY_MODEL_BASE_URL="http://model.internal/v1", SOCIETY_MODEL_API_KEY="placeholder-for-test")
    cfg.reset_settings_cache()

