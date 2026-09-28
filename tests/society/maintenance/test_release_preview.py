"""The live release preview: exact-SHA parity on a non-production environment,
then a structural surface check (ADR-0010 D12). Without it every live release
refuses (fail closed); these tests pin what "passed" requires."""

from __future__ import annotations

import json

import httpx
import pytest

from services.registry.app.maintenance import release as rel_mod
from services.registry.app.maintenance import release_providers as rp

SHA = "c" * 40
OLD = "a" * 40
PREVIEW_ENV, PROD_ENV = "env-staging", "env-prod"


class World:
    def __init__(self):
        self.active = {"svc-dash": SHA, "svc-reg": OLD}
        self.building = {}
        self.metrics_status, self.acao, self.ready = 404, "", 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "backboard.railway.com":
            v = json.loads(request.content).get("variables") or {}
            sid = v["input"]["serviceId"]
            nodes = []
            if sid in self.building:
                nodes.append({"id": "d-new", "status": "BUILDING", "createdAt": "2026-09-28T01:00:00Z", "meta": {"commitHash": self.building[sid]}, "canRollback": False})
            nodes.append({"id": "d-cur", "status": "SUCCESS", "createdAt": "2026-09-27T01:00:00Z", "meta": {"commitHash": self.active[sid]}, "canRollback": True})
            return httpx.Response(200, json={"data": {"deployments": {"edges": [{"node": n} for n in nodes]}}})
        path = request.url.path
        if request.method == "OPTIONS":
            return httpx.Response(200, headers={"access-control-allow-origin": self.acao} if self.acao else {})
        if path == "/readyz":
            return httpx.Response(self.ready, json={"status": "ready"})
        if path == "/.well-known/agent-card.json":
            return httpx.Response(200, json={"name": "AgentNet"})
        if path == "/metrics":
            return httpx.Response(self.metrics_status, text="")
        return httpx.Response(404)


class Contract:
    def __init__(self, ok=True, login=True):
        self.ok, self.login = ok, login

    def check(self):
        return {"ui_root": self.ok, "login": self.login}


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("MAINTENANCE_PREVIEW_RAILWAY_TOKEN", "preview-token")
    return World()


def _preview(world, contract=None, env=PREVIEW_ENV):
    t = httpx.MockTransport(world.handler)
    railway = rp.LiveRailway(project_id="p", environment_id=env, service_ids={"dashboard": "svc-dash", "registry": "svc-reg"}, transport=t,
                             token_env="MAINTENANCE_PREVIEW_RAILWAY_TOKEN", auth_mode_env="MAINTENANCE_PREVIEW_RAILWAY_AUTH_MODE")
    return rp.LivePreview(railway, ui_origin="https://ui.preview.test", api_origin="https://api.preview.test", production_environment_id=PROD_ENV,
                          transport=t, contract=contract or Contract())


def test_passes_only_when_the_changed_service_runs_the_exact_sha_and_the_surface_answers(world):
    res = _preview(world).validate(SHA, services=["dashboard"])
    assert res["state"] == "passed", res
    assert res["parity"] == {"dashboard": True}
    assert all(res["checks"].values())


def test_an_unchanged_service_on_an_older_sha_is_not_required(world):
    assert _preview(world).validate(SHA, services=["dashboard"])["state"] == "passed"


def test_a_changed_service_not_yet_on_the_sha_is_pending_never_passed(world):
    world.active["svc-reg"], world.building["svc-reg"] = OLD, SHA
    res = _preview(world).validate(SHA, services=["dashboard", "registry"])
    assert res["state"] == "pending" and res["parity"] == {"dashboard": True, "registry": False} and res["building"] is True


def test_the_production_environment_is_never_a_preview(world):
    with pytest.raises(ValueError):
        _preview(world, env=PROD_ENV)


@pytest.mark.parametrize("breakage,check", [
    (lambda w: setattr(w, "metrics_status", 200), "metrics_not_public"),
    (lambda w: setattr(w, "acao", "*"), "cors_foreign_origin_refused"),
    (lambda w: setattr(w, "ready", 503), "readiness"),
])
def test_a_security_or_readiness_regression_fails_the_preview(world, breakage, check):
    breakage(world)
    res = _preview(world).validate(SHA, services=["dashboard"])
    assert res["state"] == "failed" and res["checks"][check] is False


def test_a_public_surface_regression_fails_the_preview(world):
    res = _preview(world, contract=Contract(ok=False)).validate(SHA, services=["dashboard"], baseline={"ui_root": True, "login": True})
    assert res["state"] == "failed" and res["checks"]["public_surface_no_regression"] is False


def test_another_defect_still_open_in_production_does_not_block_the_repair_of_this_one(world):
    # login is broken in production too (baseline False) and stays broken: not a regression
    res = _preview(world, contract=Contract(ok=True, login=False)).validate(SHA, services=["dashboard"], baseline={"ui_root": False, "login": False},
                                                                          required=["ui_root"])
    assert res["state"] == "passed", res
    assert res["checks"]["public_surface_no_regression"] and res["checks"]["repaired_item_healthy"]


def test_the_repaired_item_must_be_healthy_on_the_preview(world):
    res = _preview(world, contract=Contract(ok=True, login=False)).validate(SHA, services=["dashboard"], baseline={"ui_root": False, "login": False},
                                                                          required=["login"])
    assert res["state"] == "failed" and res["checks"]["repaired_item_healthy"] is False


def test_an_unmapped_changed_service_fails_closed(world):
    res = _preview(world).validate(SHA, services=["payment"])
    assert res["state"] == "failed" and "payment" in res["reason"]


def test_live_preview_is_none_unless_fully_configured_and_non_production(monkeypatch):
    base = {"MAINTENANCE_RELEASE_PROVIDER": "live", "MAINTENANCE_RAILWAY_PROJECT_ID": "p", "MAINTENANCE_RAILWAY_ENVIRONMENT_ID": PROD_ENV,
            "MAINTENANCE_RAILWAY_SERVICE_IDS": json.dumps({"dashboard": "prod-dash"})}
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    assert rel_mod.live_preview(rel_mod.ReleaseSettings()) is None  # MAINTENANCE_RELEASE_PREVIEW defaults to none
    monkeypatch.setenv("MAINTENANCE_RELEASE_PREVIEW", "staging_parity")
    assert rel_mod.live_preview(rel_mod.ReleaseSettings()) is None  # incomplete
    for k, v in {"MAINTENANCE_PREVIEW_RAILWAY_SERVICE_IDS": json.dumps({"dashboard": "stg-dash"}), "MAINTENANCE_PREVIEW_UI_ORIGIN": "https://ui.s",
                 "MAINTENANCE_PREVIEW_API_ORIGIN": "https://api.s", "MAINTENANCE_PREVIEW_RAILWAY_ENVIRONMENT_ID": PROD_ENV}.items():
        monkeypatch.setenv(k, v)
    assert rel_mod.live_preview(rel_mod.ReleaseSettings()) is None  # production is refused as a preview
    monkeypatch.setenv("MAINTENANCE_PREVIEW_RAILWAY_ENVIRONMENT_ID", PREVIEW_ENV)
    assert isinstance(rel_mod.live_preview(rel_mod.ReleaseSettings()), rp.LivePreview)
