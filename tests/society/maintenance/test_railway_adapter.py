"""The live Railway adapter against the LIVE schema's shapes (ADR-0010 D12).

Live introspection of https://backboard.railway.com/graphql/v2 (2026-09-28):

* ``serviceInstanceDeployV2(commitSha, environmentId, serviceId) -> String!`` (the new deployment id)
* ``deploymentRollback(id) -> Boolean!`` (NOT a Deployment: no selection set is valid on it)
* ``deployment(id) -> Deployment!`` with ``serviceId environmentId canRollback createdAt meta status``

The first live-activation schema check found the adapter selecting
``{ id status }`` on ``deploymentRollback``, which Railway rejects: every live
rollback would have failed. These tests pin the adapter to the live shapes on
an ``httpx.MockTransport`` that validates the queries the same way.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from services.registry.app.maintenance import release_providers as rp

ENV = "env-prod"
SVC = "svc-dashboard"

LIVE_MUTATIONS = [
    {"name": "serviceInstanceDeployV2", "args": [{"name": "commitSha"}, {"name": "environmentId"}, {"name": "serviceId"}],
     "type": {"name": None, "kind": "NON_NULL", "ofType": {"name": "String", "kind": "SCALAR"}}},
    {"name": "deploymentRollback", "args": [{"name": "id"}],
     "type": {"name": None, "kind": "NON_NULL", "ofType": {"name": "Boolean", "kind": "SCALAR"}}},
    {"name": "deploymentRedeploy", "args": [{"name": "id"}, {"name": "usePreviousImageTag"}],
     "type": {"name": None, "kind": "NON_NULL", "ofType": {"name": "Deployment", "kind": "OBJECT"}}},
]


class FakeBackboard:
    """A tiny Railway: validates that scalar-returning mutations carry no selection set."""

    def __init__(self, mutations=None, *, can_rollback=True, list_lag=0, target_env=ENV):
        self.mutations = mutations if mutations is not None else LIVE_MUTATIONS
        self.can_rollback, self.list_lag, self.target_env = can_rollback, list_lag, target_env
        self.calls, self.rollbacks, self.list_calls = [], 0, 0
        self.deps = [{"id": "dep-good", "status": "REMOVED", "createdAt": "2026-09-20T00:00:00Z", "meta": {"commitHash": "a" * 40}, "canRollback": True}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        q, v = body["query"], body.get("variables") or {}
        self.calls.append(q)
        if "__schema" in q:
            return httpx.Response(200, json={"data": {"__schema": {"mutationType": {"fields": self.mutations}}}})
        if "deploymentRollback" in q:
            if re.search(r"deploymentRollback\([^)]*\)\s*\{", q):
                return httpx.Response(200, json={"errors": [{"message": 'Field "deploymentRollback" must not have a selection since type "Boolean!" has no subfields.'}]})
            self.rollbacks += 1
            self.deps.insert(0, {"id": f"dep-rb-{self.rollbacks}", "status": "DEPLOYING", "createdAt": _now_iso(), "meta": {"commitHash": "a" * 40}, "canRollback": False})
            return httpx.Response(200, json={"data": {"deploymentRollback": True}})
        if "deployments(" in q:
            self.list_calls += 1
            visible = self.deps if self.list_calls > self.list_lag else self.deps[self.rollbacks:]
            return httpx.Response(200, json={"data": {"deployments": {"edges": [{"node": d} for d in visible]}}})
        if "deployment(" in q:
            d = next(x for x in self.deps if x["id"] == v["id"])
            return httpx.Response(200, json={"data": {"deployment": {**d, "serviceId": SVC, "environmentId": self.target_env, "canRollback": self.can_rollback if d["id"] == "dep-good" else d["canRollback"]}}})
        if "serviceInstanceDeployV2" in q:
            if re.search(r"serviceInstanceDeployV2\([^)]*\)\s*\{", q):
                return httpx.Response(200, json={"errors": [{"message": "String! has no subfields"}]})
            return httpx.Response(200, json={"data": {"serviceInstanceDeployV2": "dep-new"}})
        return httpx.Response(200, json={"data": {"__typename": "Query"}})


def _now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("MAINTENANCE_RAILWAY_TOKEN", "test-railway-token")
    monkeypatch.setenv("MAINTENANCE_RAILWAY_AUTH_MODE", "project")
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)


def _adapter(bb: FakeBackboard) -> rp.LiveRailway:
    return rp.LiveRailway(project_id="proj", environment_id=ENV, service_ids={"dashboard": SVC}, transport=httpx.MockTransport(bb.handler))


def test_discovery_accepts_the_live_schema_shapes(token):
    assert _adapter(FakeBackboard()).discover() == {"serviceInstanceDeployV2.commitSha": True, "deploymentRollback": True}


def test_discovery_refuses_a_rollback_whose_result_shape_changed(token):
    changed = [m if m["name"] != "deploymentRollback" else {**m, "type": {"name": None, "kind": "NON_NULL", "ofType": {"name": "Deployment", "kind": "OBJECT"}}} for m in LIVE_MUTATIONS]
    ad = _adapter(FakeBackboard(changed))
    assert ad.discover()["deploymentRollback"] is False
    with pytest.raises(rp.ProviderRefused):
        ad.rollback("dep-good")


def test_rollback_sends_a_bare_boolean_mutation_and_returns_the_new_deployment(token):
    bb = FakeBackboard()
    new_id = _adapter(bb).rollback("dep-good")
    assert new_id == "dep-rb-1" and bb.rollbacks == 1
    sent = [q for q in bb.calls if "deploymentRollback(" in q]
    assert sent and not re.search(r"deploymentRollback\([^)]*\)\s*\{", sent[0])


def test_a_rollback_not_yet_listed_is_resolved_later_and_never_issued_twice(token):
    bb = FakeBackboard(list_lag=10)  # the new deployment is invisible for the first listings
    ad = _adapter(bb)
    handle = ad.rollback("dep-good")
    assert handle.startswith(rp.LiveRailway.PENDING) and bb.rollbacks == 1
    bb.list_lag = 0
    dep = ad.get(handle)
    assert dep.id == "dep-rb-1" and dep.status == "DEPLOYING" and bb.rollbacks == 1


def test_a_pending_handle_reads_as_deploying_until_listed(token):
    bb = FakeBackboard(list_lag=10)
    ad = _adapter(bb)
    handle = ad.rollback("dep-good")
    assert ad.get(handle).status == "DEPLOYING"


def test_can_rollback_false_is_refused_so_the_controller_redeploys_the_known_good_sha(token):
    bb = FakeBackboard(can_rollback=False)
    with pytest.raises(rp.ProviderRefused):
        _adapter(bb).rollback("dep-good")
    assert bb.rollbacks == 0


def test_a_deployment_from_another_environment_is_refused(token):
    bb = FakeBackboard(target_env="env-staging")
    with pytest.raises(rp.ProviderRefused):
        _adapter(bb).rollback("dep-good")
    assert bb.rollbacks == 0


def test_deploy_sha_returns_the_id_string_without_a_selection_set(token):
    bb = FakeBackboard()
    assert _adapter(bb).deploy_sha("dashboard", "b" * 40) == "dep-new"
