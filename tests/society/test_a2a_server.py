"""A2A 1.0 gateway against a real PostgreSQL (ADR-0009).

Covers the cards, both bindings, authentication and per-operation
authorization (no BOLA), the economics bridge (escrow through
task_service only), cancellation money invariants, idempotency, streaming,
the unsupported-by-design operations and bounded metrics. The callee side
of every task is driven through the registry's own REST task API (start /
confirm / fail) exactly as a real AgentNet agent would do it.
"""

from __future__ import annotations

import json
import threading
import uuid

import pytest
from a2a.types import a2a_pb2 as pb
from google.protobuf.json_format import ParseDict

from services.registry.app.auth import create_agent_token
from services.registry.app.models import ScopedToken, TaskSession, TaskStatus, Transaction, TransactionStatus, Wallet

from .conftest import auth

BASE = "https://api.agentnet.test"
ECON = "https://agentnet.io.vn/a2a/extensions/economics/v1"
FED = "https://agentnet.io.vn/a2a/extensions/federation/v1"
FREE_CAP = [{"name": "echo", "version": "1.0", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 0}]
PAID_CAP = [{"name": "summarize", "version": "1.0", "input_schema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}, "output_schema": {"type": "object"}, "price": 10}]


@pytest.fixture
def a2a(monkeypatch, SessionLocal, api_client):
    from services.registry.app.a2a import handler as a2a_handler
    from services.registry.app.a2a import routes as a2a_routes

    monkeypatch.setenv("A2A_SERVER_ENABLED", "true")
    monkeypatch.setenv("A2A_PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("A2A_BLOCKING_WAIT_SECONDS", "0")
    monkeypatch.setattr(a2a_routes.runtime, "session_factory", SessionLocal)
    dispatched = []

    async def fake_dispatch(**kw):
        dispatched.append(kw)
        return "websocket"

    monkeypatch.setattr(a2a_handler, "dispatch_execute", fake_dispatch)
    api_client.dispatched = dispatched
    return api_client


def _msg(*, text=None, data=None, mid=None, metadata=None, context_id=None, task_id=None):
    parts = []
    if text is not None:
        parts.append({"text": text})
    if data is not None:
        parts.append({"data": data})
    m = {"messageId": mid or uuid.uuid4().hex, "role": "ROLE_USER", "parts": parts}
    if metadata:
        m["metadata"] = metadata
    if context_id:
        m["contextId"] = context_id
    if task_id:
        m["taskId"] = task_id
    return m


def rpc(client, method, params, token=None, *, version="1.0", ext=None):
    headers = {}
    if version:
        headers["A2A-Version"] = version
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if ext:
        headers["A2A-Extensions"] = ext
    r = client.post("/a2a", json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params}, headers=headers)
    return r


def ok(r):
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    return body["result"]


def err(r):
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" in body, body
    return body["error"]


def send(client, token, tenant, message, *, ext=None, return_immediately=True):
    params = {"message": message, "configuration": {"returnImmediately": return_immediately}}
    if tenant:
        params["tenant"] = str(tenant)
    return rpc(client, "SendMessage", params, token, ext=ext)


def _wallet(db, agent):
    db.expire_all()
    return db.query(Wallet).filter(Wallet.owner_id == agent.id).one()


@pytest.fixture
def pair(make_agent):
    """A paying caller (100 credits) and a callee with one free and one paid skill."""
    caller = make_agent("A2A_Caller", balance_credits=100)
    callee = make_agent("A2A_Callee", capabilities=FREE_CAP + PAID_CAP)
    return caller, callee, create_agent_token(caller.id).access_token, create_agent_token(callee.id).access_token


# ── feature flag, cards ─────────────────────────────────────────────────


def test_everything_is_404_while_the_flag_is_off(api_client, monkeypatch, make_agent):
    monkeypatch.delenv("A2A_SERVER_ENABLED", raising=False)
    agent = make_agent("Off_A")
    assert api_client.get("/.well-known/agent-card.json").status_code == 404
    assert api_client.get(f"/v1/agents/{agent.id}/a2a-card").status_code == 404
    assert rpc(api_client, "GetTask", {"id": "x"}, "t").status_code == 404
    assert api_client.post("/a2a/http/message:send", json={}).status_code == 404


def test_production_refuses_a_missing_or_plain_http_base_url(api_client, monkeypatch):
    from services.registry.app.a2a import config

    monkeypatch.setenv("A2A_SERVER_ENABLED", "true")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("A2A_PUBLIC_BASE_URL", raising=False)
    assert config.public_base_url() is None and not config.gateway_ready()
    for bad in ("http://api.agentnet.io.vn", "https://u:p@api.agentnet.io.vn", "https://api.agentnet.io.vn/x", "ftp://x"):
        monkeypatch.setenv("A2A_PUBLIC_BASE_URL", bad)
        assert config.public_base_url() is None, bad
    monkeypatch.setenv("A2A_PUBLIC_BASE_URL", "https://api.agentnet.io.vn/")
    assert config.public_base_url() == "https://api.agentnet.io.vn"


def test_canonical_card_is_a_valid_truthful_v1_card(a2a):
    r = a2a.get("/.well-known/agent-card.json", headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=300" and r.headers["etag"]
    card = r.json()
    ParseDict(card, pb.AgentCard())  # strict: every field is a real v1 field
    assert "evil.example" not in r.text  # the base URL never comes from request headers
    ifaces = {i["protocolBinding"]: i for i in card["supportedInterfaces"]}
    assert ifaces["JSONRPC"]["url"] == f"{BASE}/a2a" and ifaces["HTTP+JSON"]["url"] == f"{BASE}/a2a/http"
    assert all(i["protocolVersion"] == "1.0" and "tenant" not in i for i in ifaces.values())
    caps = card["capabilities"]
    assert caps["streaming"] is True and caps.get("pushNotifications", False) is False and caps.get("extendedAgentCard", False) is False
    assert {e["uri"] for e in caps["extensions"]} == {ECON, FED}
    assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    assert card["securityRequirements"] == [{"schemes": {"bearer": {}}}]
    assert "url" not in card and "stateTransitionHistory" not in json.dumps(card)
    assert a2a.get("/.well-known/agent-card.json", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_agent_card_is_sanitized_and_routes_through_the_gateway(a2a, make_agent, db):
    agent = make_agent("Card_A", capabilities=FREE_CAP + PAID_CAP)
    for path in (f"/v1/agents/{agent.id}/a2a-card", f"/v1/agents/{agent.id}/agent-card.json"):
        r = a2a.get(path)
        assert r.status_code == 200, r.text
        card = r.json()
        ParseDict(card, pb.AgentCard())
        assert {i["tenant"] for i in card["supportedInterfaces"]} == {str(agent.id)}
        assert {i["url"] for i in card["supportedInterfaces"]} == {f"{BASE}/a2a", f"{BASE}/a2a/http"}
        text = r.text
        for secret in (agent.endpoint, agent.public_key, str(agent.user_id)):
            assert secret not in text
        assert {s["id"] for s in card["skills"]} == {"echo", "summarize"}
        econ = next(e for e in card["capabilities"]["extensions"] if e["uri"] == ECON)
        assert econ["params"]["skillPrices"] == {"echo": 0, "summarize": 10}
    from services.registry.app.models import AgentStatus

    agent.status = AgentStatus.SUSPENDED
    db.commit()
    assert a2a.get(f"/v1/agents/{agent.id}/a2a-card").status_code == 404
    assert a2a.get(f"/v1/agents/{uuid.uuid4()}/a2a-card").status_code == 404
    assert a2a.get("/v1/agents/not-a-uuid/a2a-card").status_code == 404


# ── authentication, version ─────────────────────────────────────────────


def test_every_operation_requires_an_agentnet_credential(a2a):
    for token in (None, "garbage", "spt_" + "0" * 32):
        r = rpc(a2a, "ListTasks", {}, token)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
        assert "garbage" not in r.text
        r = a2a.get("/a2a/http/tasks", headers={"A2A-Version": "1.0", **({"Authorization": f"Bearer {token}"} if token else {})})
        assert r.status_code == 401


def test_missing_version_is_0_3_and_refused_query_form_is_honoured(a2a, pair):
    _, _, tok, _ = pair
    e = err(rpc(a2a, "ListTasks", {}, tok, version=None))
    assert e["code"] == -32009
    r = a2a.post("/a2a?A2A-Version=1.0", json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}, headers=auth(tok))
    assert "result" in r.json(), r.text
    r = a2a.get("/a2a/http/tasks", headers=auth(tok))
    assert r.status_code == 400 and r.json()["error"]["details"][0]["reason"] == "VERSION_NOT_SUPPORTED"
    assert r.json()["error"]["details"][0]["domain"] == "a2a-protocol.org"


def test_old_0_3_method_names_are_not_served(a2a, pair):
    _, _, tok, _ = pair
    assert err(rpc(a2a, "message/send", {}, tok))["code"] == -32601


def test_body_limit_and_content_type(a2a, pair, monkeypatch):
    _, _, tok, _ = pair
    monkeypatch.setenv("A2A_MAX_REQUEST_BYTES", "1024")
    big = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": _msg(text="x" * 5000)}}
    assert a2a.post("/a2a", json=big, headers={"A2A-Version": "1.0", **auth(tok)}).status_code == 413
    r = a2a.post("/a2a", content=b"{}", headers={"A2A-Version": "1.0", "Content-Type": "text/plain", **auth(tok)})
    assert r.status_code == 415


# ── network tenant ──────────────────────────────────────────────────────


def test_network_search_answers_with_a_message_for_any_core_client(a2a, pair):
    caller, callee, tok, _ = pair
    res = ok(send(a2a, tok, None, _msg(text="A2A_Callee")))
    m = res["message"]
    assert m["role"] == "ROLE_AGENT"
    data = next(p["data"] for p in m["parts"] if "data" in p)
    assert [a["agentId"] for a in data["agents"]] == [str(callee.id)]
    assert data["agents"][0]["cardUrl"] == f"{BASE}/v1/agents/{callee.id}/a2a-card"
    res = ok(send(a2a, tok, None, _msg(data={"agentId": str(callee.id)})))
    data = next(p["data"] for p in res["message"]["parts"] if "data" in p)
    assert data["found"] is True and data["card"]["name"] == "A2A_Callee"


def test_user_jwt_can_use_network_skills_but_cannot_create_tasks(a2a, user_token, pair):
    _, callee, _, _ = pair
    _, utok = user_token(None)
    assert "message" in ok(send(a2a, utok, None, _msg(text="anything")))
    e = err(send(a2a, utok, callee.id, _msg(data={}, metadata={"skillId": "echo"})))
    assert e["code"] == -32600 and "agent credential" in e["message"]


# ── task lifecycle: free skill, core client ─────────────────────────────


def _callee(a2a, callee_tok, session_id, action, **kw):
    if action == "start":
        return a2a.put(f"/v1/tasks/{session_id}/start", headers=auth(callee_tok))
    if action == "confirm":
        return a2a.put(f"/v1/tasks/{session_id}/confirm", headers=auth(callee_tok), json=kw.get("output", {"text": "done", "n": 1}))
    if action == "fail":
        return a2a.put(f"/v1/tasks/{session_id}/fail", headers=auth(callee_tok), params={"error_message": kw.get("error", "boom")})


def _session_for(db, task_id):
    from services.registry.app.a2a.orm import A2ATask

    db.expire_all()
    row = db.query(A2ATask).filter(A2ATask.id == uuid.UUID(task_id)).one()
    return row.task_session_id


def test_free_skill_full_lifecycle_projects_economic_states(a2a, pair, db):
    caller, callee, tok, ctok = pair
    task = ok(send(a2a, tok, callee.id, _msg(data={"q": 1}, metadata={"skillId": "echo"})))["task"]
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    assert task["history"][0]["role"] == "ROLE_USER"
    sid = _session_for(db, task["id"])
    ts = db.get(TaskSession, sid)
    assert ts.escrow_amount == 0 and ts.status == TaskStatus.INITIATED and ts.input == {"q": 1}
    assert a2a.dispatched[-1]["task_session_id"] == sid

    assert _callee(a2a, ctok, sid, "start").status_code == 200
    got = ok(rpc(a2a, "GetTask", {"id": task["id"], "tenant": str(callee.id)}, tok))
    assert got["status"]["state"] == "TASK_STATE_WORKING"

    assert _callee(a2a, ctok, sid, "confirm").status_code == 200, "confirm"
    got = ok(rpc(a2a, "GetTask", {"id": task["id"], "tenant": str(callee.id)}, tok))
    assert got["status"]["state"] == "TASK_STATE_COMPLETED"
    parts = got["artifacts"][0]["parts"]
    assert parts[0] == {"text": "done", "mediaType": "text/plain"}
    assert parts[1]["data"] == {"text": "done", "n": 1} and parts[1]["mediaType"] == "application/json"
    got = ok(rpc(a2a, "GetTask", {"id": task["id"], "tenant": str(callee.id), "historyLength": 0}, tok))
    assert "history" not in got


def test_a_start_and_confirm_between_observations_still_records_working(a2a, pair, db, SessionLocal):
    """COMPLETED is reachable only through IN_PROGRESS, so the durable event
    log must show WORKING even when no projection ran while the callee was
    working (the streaming flake: SUBMITTED -> COMPLETED)."""
    from services.registry.app.a2a import store
    from services.registry.app.task_service import confirm_task_completion, start_task

    caller, callee, tok, ctok = pair
    task = ok(send(a2a, tok, callee.id, _msg(data={}, metadata={"skillId": "echo"})))["task"]
    sid = _session_for(db, task["id"])
    s = SessionLocal()
    try:  # straight through task_service: nothing observes the in-between state
        start_task(db=s, task_id=sid, callee_agent=s.merge(callee))
        confirm_task_completion(db=s, callee_agent=s.merge(callee), task_id=sid, output={"text": "fast"})
    finally:
        s.close()
    got = ok(rpc(a2a, "GetTask", {"id": task["id"], "tenant": str(callee.id)}, tok))
    assert got["status"]["state"] == "TASK_STATE_COMPLETED"
    db.expire_all()
    log = [
        payload["status"]["state"] if kind == "status" else "artifact"
        for _, kind, payload in store.events_after(db, uuid.UUID(task["id"]), 0)
    ]
    assert log == ["TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "artifact", "TASK_STATE_COMPLETED"], log


def test_failure_and_timeout_map_to_failed_with_a_structured_message(a2a, pair, db):
    caller, callee, tok, ctok = pair
    task = ok(send(a2a, tok, callee.id, _msg(data={}, metadata={"skillId": "echo"})))["task"]
    sid = _session_for(db, task["id"])
    _callee(a2a, ctok, sid, "start")
    _callee(a2a, ctok, sid, "fail", error="model unavailable")
    got = ok(rpc(a2a, "GetTask", {"id": task["id"], "tenant": str(callee.id)}, tok))
    assert got["status"]["state"] == "TASK_STATE_FAILED"
    data = next(p["data"] for p in got["status"]["message"]["parts"] if "data" in p)
    assert data == {"reason": "failed", "agentError": "model unavailable"}

    task2 = ok(send(a2a, tok, callee.id, _msg(data={}, metadata={"skillId": "echo"})))["task"]
    from services.registry.app.task_service import fail_task_with_refund

    fail_task_with_refund(db=db, task_id=_session_for(db, task2["id"]), error_message="Task timed out", new_status=TaskStatus.TIMEOUT)
    got = ok(rpc(a2a, "GetTask", {"id": task2["id"], "tenant": str(callee.id)}, tok))
    data = next(p["data"] for p in got["status"]["message"]["parts"] if "data" in p)
    assert got["status"]["state"] == "TASK_STATE_FAILED" and data == {"reason": "timeout"}


# ── economics ───────────────────────────────────────────────────────────


def test_paid_skill_needs_the_extension_and_charges_nothing_without_it(a2a, pair, db):
    caller, callee, tok, _ = pair
    e = err(send(a2a, tok, callee.id, _msg(text="hi", metadata={"skillId": "summarize"})))
    assert e["code"] == -32008 and e["data"][0]["metadata"]["uri"] == ECON
    # metadata present but header absent: still not activated
    e = err(send(a2a, tok, callee.id, _msg(text="hi", metadata={"skillId": "summarize", ECON: {"maxBudget": 10}})))
    assert e["code"] == -32008
    w = _wallet(db, caller)
    assert w.reserved_credits == 0 and db.query(TaskSession).count() == 0


def test_paid_skill_reserves_escrow_and_settles_through_the_trigger(a2a, pair, db):
    caller, callee, tok, ctok = pair
    r = send(a2a, tok, callee.id, _msg(text="summarize this", metadata={"skillId": "summarize", ECON: {"maxBudget": 15, "currency": "credits", "quotedPrice": 10}}), ext=ECON)
    assert r.headers.get("a2a-extensions") == ECON
    task = ok(r)["task"]
    sid = _session_for(db, task["id"])
    ts = db.get(TaskSession, sid)
    assert ts.escrow_amount == 10 and ts.input == {"text": "summarize this"}
    assert _wallet(db, caller).reserved_credits == 10
    _callee(a2a, ctok, sid, "start")
    _callee(a2a, ctok, sid, "confirm", output={"summary": "s"})
    got = ok(rpc(a2a, "GetTask", {"id": task["id"], "tenant": str(callee.id)}, tok))
    assert got["status"]["state"] == "TASK_STATE_COMPLETED"
    w = _wallet(db, caller)
    assert w.reserved_credits == 0 and w.balance_credits == 90
    assert _wallet(db, callee).balance_credits > 0  # net of the platform fee (trigger)


def test_economic_refusal_is_rejected_before_any_escrow(a2a, make_agent, db):
    poor = make_agent("Poor", balance_credits=5)
    callee = make_agent("Pricey", capabilities=PAID_CAP)
    tok = create_agent_token(poor.id).access_token
    task = ok(send(a2a, tok, callee.id, _msg(text="x", metadata={ECON: {"maxBudget": 10}}), ext=ECON))["task"]
    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    data = next(p["data"] for p in task["status"]["message"]["parts"] if "data" in p)
    assert data["reason"] == "escrow_refused"
    assert db.query(TaskSession).count() == 0 and _wallet(db, poor).reserved_credits == 0
    # quoted price mismatch is refused too
    rich = make_agent("Rich", balance_credits=100)
    t2 = ok(send(a2a, create_agent_token(rich.id).access_token, callee.id, _msg(text="x", metadata={ECON: {"maxBudget": 50, "quotedPrice": 5}}), ext=ECON))["task"]
    assert t2["status"]["state"] == "TASK_STATE_REJECTED" and _wallet(db, rich).reserved_credits == 0


def test_scoped_tokens_need_execute_and_are_charged_against_their_cap(a2a, pair, db):
    import hashlib

    caller, callee, _, _ = pair

    def mint(actions, cap):
        raw = "spt_" + uuid.uuid4().hex
        db.add(ScopedToken(id=uuid.uuid4(), token_hash=hashlib.sha256(raw.encode()).hexdigest(), agent_id=caller.id, resource_type="a2a", allowed_actions=actions, spending_cap=cap))
        db.commit()
        return raw

    ro = mint(["read"], 100)
    e = err(send(a2a, ro, callee.id, _msg(text="x", metadata={"skillId": "summarize", ECON: {"maxBudget": 10}}), ext=ECON))
    assert e["code"] == -32600 and "execute" in e["message"]
    capped = mint(["execute"], 12)
    t1 = ok(send(a2a, capped, callee.id, _msg(text="x", metadata={"skillId": "summarize", ECON: {"maxBudget": 10}}), ext=ECON))["task"]
    assert t1["status"]["state"] == "TASK_STATE_SUBMITTED"
    t2 = ok(send(a2a, capped, callee.id, _msg(text="y", metadata={"skillId": "summarize", ECON: {"maxBudget": 10}}), ext=ECON))["task"]
    assert t2["status"]["state"] == "TASK_STATE_REJECTED"  # 10 + 10 > cap 12
    db.expire_all()
    assert _wallet(db, caller).reserved_credits == 10


def test_same_message_id_is_idempotent_and_never_double_reserves(a2a, pair, db):
    caller, callee, tok, _ = pair
    m = _msg(text="once", metadata={"skillId": "summarize", ECON: {"maxBudget": 10}})
    t1 = ok(send(a2a, tok, callee.id, m, ext=ECON))["task"]
    t2 = ok(send(a2a, tok, callee.id, m, ext=ECON))["task"]
    assert t1["id"] == t2["id"]
    assert db.query(TaskSession).count() == 1 and _wallet(db, caller).reserved_credits == 10
    changed = dict(m, parts=[{"text": "different"}])
    assert err(send(a2a, tok, callee.id, changed, ext=ECON))["code"] == -32600


# ── authorization: no BOLA, tenant is not authorization ─────────────────


def test_tasks_are_visible_only_to_parties_under_their_own_tenant(a2a, pair, make_agent, user_token, db):
    caller, callee, tok, ctok = pair
    task = ok(send(a2a, tok, callee.id, _msg(data={}, metadata={"skillId": "echo"})))["task"]
    stranger = make_agent("Stranger")
    stok = create_agent_token(stranger.id).access_token
    for token, tenant in ((stok, callee.id), (stok, stranger.id), (tok, stranger.id), (tok, None)):
        params = {"id": task["id"]}
        if tenant:
            params["tenant"] = str(tenant)
        assert err(rpc(a2a, "GetTask", params, token))["code"] == -32001
    assert err(rpc(a2a, "GetTask", {"id": "not-a-uuid", "tenant": str(callee.id)}, tok))["code"] == -32001
    # both parties see it; so does the user who owns the callee agent
    assert ok(rpc(a2a, "GetTask", {"id": task["id"], "tenant": str(callee.id)}, ctok))["id"] == task["id"]
    owner_tok = __import__("services.registry.app.auth", fromlist=["x"]).create_user_token(callee.user_id).access_token
    assert ok(rpc(a2a, "GetTask", {"id": task["id"], "tenant": str(callee.id)}, owner_tok))["id"] == task["id"]
    # lists never leak
    assert ok(rpc(a2a, "ListTasks", {"tenant": str(callee.id)}, stok))["tasks"] == []
    assert [t["id"] for t in ok(rpc(a2a, "ListTasks", {"tenant": str(callee.id)}, tok))["tasks"]] == [task["id"]]
    # the stranger cannot cancel or subscribe either
    assert err(rpc(a2a, "CancelTask", {"id": task["id"], "tenant": str(callee.id)}, stok))["code"] == -32001


def test_list_tasks_pages_newest_first_with_opaque_tokens(a2a, pair):
    caller, callee, tok, _ = pair
    ids = [ok(send(a2a, tok, callee.id, _msg(data={"i": i}, metadata={"skillId": "echo"})))["task"]["id"] for i in range(3)]
    page = ok(rpc(a2a, "ListTasks", {"tenant": str(callee.id), "pageSize": 2}, tok))
    assert page["pageSize"] == 2 and page["totalSize"] == 3 and page["nextPageToken"]
    rest = ok(rpc(a2a, "ListTasks", {"tenant": str(callee.id), "pageSize": 2, "pageToken": page["nextPageToken"]}, tok))
    assert rest["nextPageToken"] == ""
    assert {t["id"] for t in page["tasks"] + rest["tasks"]} == set(ids)
    assert err(rpc(a2a, "ListTasks", {"tenant": str(callee.id), "pageSize": 101}, tok))["code"] == -32602
    assert err(rpc(a2a, "ListTasks", {"tenant": str(callee.id), "pageToken": "bogus"}, tok))["code"] == -32602
    only = ok(rpc(a2a, "ListTasks", {"tenant": str(callee.id), "status": "TASK_STATE_COMPLETED"}, tok))
    assert only["tasks"] == [] and only["totalSize"] == 0


# ── cancellation money invariants ───────────────────────────────────────


def test_cancel_before_start_refunds_exactly_once_and_is_idempotent(a2a, pair, db):
    caller, callee, tok, ctok = pair
    task = ok(send(a2a, tok, callee.id, _msg(text="x", metadata={"skillId": "summarize", ECON: {"maxBudget": 10}}), ext=ECON))["task"]
    sid = _session_for(db, task["id"])
    assert _wallet(db, caller).reserved_credits == 10
    assert err(rpc(a2a, "CancelTask", {"id": task["id"], "tenant": str(callee.id)}, ctok))["code"] == -32002  # callee cannot
    c1 = ok(rpc(a2a, "CancelTask", {"id": task["id"], "tenant": str(callee.id)}, tok))
    c2 = ok(rpc(a2a, "CancelTask", {"id": task["id"], "tenant": str(callee.id)}, tok))
    assert c1["status"]["state"] == c2["status"]["state"] == "TASK_STATE_CANCELED"
    w = _wallet(db, caller)
    assert w.reserved_credits == 0 and w.balance_credits == 100
    tx = db.query(Transaction).filter(Transaction.task_session_id == sid).one()
    assert tx.status == TransactionStatus.CANCELLED
    ts = db.get(TaskSession, sid)
    assert ts.status == TaskStatus.FAILED and ts.error_message == "canceled_by_caller"
    # the callee can no longer start it
    assert _callee(a2a, ctok, sid, "start").status_code == 400


def test_cancel_after_start_is_refused_and_moves_no_money(a2a, pair, db):
    caller, callee, tok, ctok = pair
    task = ok(send(a2a, tok, callee.id, _msg(text="x", metadata={"skillId": "summarize", ECON: {"maxBudget": 10}}), ext=ECON))["task"]
    sid = _session_for(db, task["id"])
    _callee(a2a, ctok, sid, "start")
    assert err(rpc(a2a, "CancelTask", {"id": task["id"], "tenant": str(callee.id)}, tok))["code"] == -32002
    assert _wallet(db, caller).reserved_credits == 10
    _callee(a2a, ctok, sid, "confirm", output={"ok": True})
    assert err(rpc(a2a, "CancelTask", {"id": task["id"], "tenant": str(callee.id)}, tok))["code"] == -32002


def test_cancel_racing_start_has_exactly_one_outcome(pair, db_factory, db):
    from services.registry.app.task_service import (
        EscrowError,
        cancel_task_with_refund,
        create_task_with_escrow,
        start_task,
    )

    caller, callee, _, _ = pair
    for _ in range(6):
        s = db_factory()
        ts, _tx = create_task_with_escrow(db=s, caller_agent=s.merge(caller), callee_agent_id=callee.id, capability_name="summarize", input_data={"text": "r"}, max_budget=10)
        outcomes = {}
        barrier = threading.Barrier(2)

        def do_cancel():
            s1 = db_factory()
            barrier.wait()
            try:
                cancel_task_with_refund(db=s1, task_id=ts.id, caller_agent_id=caller.id)
                outcomes["cancel"] = True
            except EscrowError:
                s1.rollback()
                outcomes["cancel"] = False

        def do_start():
            s2 = db_factory()
            barrier.wait()
            try:
                start_task(db=s2, task_id=ts.id, callee_agent=s2.merge(callee))
                outcomes["start"] = True
            except EscrowError:
                s2.rollback()
                outcomes["start"] = False

        threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_start)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        assert outcomes["cancel"] != outcomes["start"], outcomes
        db.expire_all()
        final = db.get(TaskSession, ts.id)
        assert final.status == (TaskStatus.FAILED if outcomes["cancel"] else TaskStatus.IN_PROGRESS)
        if final.status == TaskStatus.IN_PROGRESS:
            from services.registry.app.task_service import fail_task_with_refund

            fail_task_with_refund(db=db, task_id=ts.id, error_message="cleanup")
    assert _wallet(db, caller).reserved_credits == 0 and _wallet(db, caller).balance_credits == 100


def test_cancel_path_guards(pair, db):
    from services.registry.app.task_service import EscrowError, cancel_task_with_refund, create_task_with_escrow, fail_task_with_refund

    caller, callee, _, _ = pair
    ts, _ = create_task_with_escrow(db=db, caller_agent=caller, callee_agent_id=callee.id, capability_name="summarize", input_data={"text": "g"}, max_budget=10)
    with pytest.raises(EscrowError):
        cancel_task_with_refund(db=db, task_id=ts.id, caller_agent_id=callee.id)  # not the caller
    db.rollback()
    with pytest.raises(EscrowError):
        fail_task_with_refund(db=db, task_id=ts.id, error_message="canceled_by_caller", callee_agent_id=callee.id)  # reserved
    db.rollback()
    cancel_task_with_refund(db=db, task_id=ts.id, caller_agent_id=caller.id)
    cancel_task_with_refund(db=db, task_id=ts.id, caller_agent_id=caller.id)  # idempotent
    assert _wallet(db, caller).reserved_credits == 0


# ── streaming ───────────────────────────────────────────────────────────


def _sse_events(response):
    events = []
    for line in response.iter_lines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


def test_streaming_send_replays_durable_events_until_terminal(a2a, pair, db, SessionLocal):
    caller, callee, tok, ctok = pair
    from services.registry.app.task_service import confirm_task_completion, start_task

    def callee_work():
        import time

        s = SessionLocal()
        try:
            from services.registry.app.a2a.orm import A2ATask

            for _ in range(100):
                row = s.query(A2ATask).filter(A2ATask.caller_agent_id == caller.id).first()
                if row is not None and row.task_session_id:
                    break
                s.rollback()
                time.sleep(0.05)
            start_task(db=s, task_id=row.task_session_id, callee_agent=s.merge(callee))
            time.sleep(0.3)
            confirm_task_completion(db=s, callee_agent=s.merge(callee), task_id=row.task_session_id, output={"text": "streamed"})
        finally:
            s.close()

    worker = threading.Thread(target=callee_work)
    worker.start()
    body = {"jsonrpc": "2.0", "id": 3, "method": "SendStreamingMessage", "params": {"tenant": str(callee.id), "message": _msg(data={}, metadata={"skillId": "echo"})}}
    with a2a.stream("POST", "/a2a", json=body, headers={"A2A-Version": "1.0", **auth(tok)}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(r)
    worker.join(30)
    results = [e["result"] for e in events]
    # the stream opens with a Task snapshot (its state depends on how fast the callee is)
    assert "task" in results[0] and results[0]["task"]["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")
    states = [results[0]["task"]["status"]["state"]] + [e["statusUpdate"]["status"]["state"] for e in results[1:] if "statusUpdate" in e]
    assert states[-1] == "TASK_STATE_COMPLETED" and "TASK_STATE_WORKING" in states
    assert len(states) == len(set(states)), states  # every transition exactly once
    assert any("artifactUpdate" in e for e in results)

    task_id = results[0]["task"]["id"]
    e = err(rpc(a2a, "SubscribeToTask", {"id": task_id, "tenant": str(callee.id)}, tok))
    assert e["code"] == -32004  # terminal task


def test_stream_limits_are_enforced(a2a, pair, monkeypatch):
    from services.registry.app.a2a import routes as a2a_routes

    caller, callee, tok, _ = pair
    monkeypatch.setenv("A2A_MAX_STREAMS_PER_PRINCIPAL", "1")
    key = f"agent:{caller.id}"
    assert a2a_routes.runtime.limiter.acquire(key, None, per_principal=1, per_task=1)
    try:
        body = {"jsonrpc": "2.0", "id": 3, "method": "SendStreamingMessage", "params": {"tenant": str(callee.id), "message": _msg(data={}, metadata={"skillId": "echo"})}}
        # refused before the first event, so the SDK answers with a plain JSON-RPC error
        e = err(a2a.post("/a2a", json=body, headers={"A2A-Version": "1.0", **auth(tok)}))
        assert e["code"] == -32600 and "streams" in e["message"]
    finally:
        a2a_routes.runtime.limiter.release(key, None)


# ── HTTP+JSON binding ───────────────────────────────────────────────────


def test_http_json_binding_with_tenant_path(a2a, pair, db):
    caller, callee, tok, _ = pair
    h = {"A2A-Version": "1.0", "Content-Type": "application/a2a+json", **auth(tok)}
    body = {"message": _msg(data={"x": 1}, metadata={"skillId": "echo"}), "configuration": {"returnImmediately": True}}
    r = a2a.post(f"/a2a/http/{callee.id}/message:send", content=json.dumps(body), headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/a2a+json") and r.headers["cache-control"] == "no-store"
    task = r.json()["task"]
    r = a2a.get(f"/a2a/http/{callee.id}/tasks/{task['id']}", headers=h)
    assert r.status_code == 200 and r.json()["id"] == task["id"]
    r = a2a.get(f"/a2a/http/{uuid.uuid4()}/tasks/{task['id']}", headers=h)
    assert r.status_code == 404 and r.json()["error"]["details"][0]["reason"] == "TASK_NOT_FOUND"
    r = a2a.get(f"/a2a/http/{callee.id}/tasks", headers=h, params={"pageSize": 5})
    assert r.status_code == 200 and r.json()["tasks"][0]["id"] == task["id"]
    r = a2a.post(f"/a2a/http/{callee.id}/tasks/{task['id']}:cancel", headers=h)
    assert r.status_code == 200 and r.json()["status"]["state"] == "TASK_STATE_CANCELED"
    r = a2a.post(f"/a2a/http/{callee.id}/tasks/{task['id']}:cancel", headers=h)
    assert r.status_code == 200  # idempotent


# ── explicitly unsupported (and advertised as such) ─────────────────────


def test_push_and_extended_card_are_refused_with_spec_errors(a2a, pair):
    _, callee, tok, _ = pair
    assert err(rpc(a2a, "CreateTaskPushNotificationConfig", {"taskId": "t", "url": "https://x.example/cb"}, tok))["code"] == -32003
    assert err(rpc(a2a, "ListTaskPushNotificationConfigs", {"taskId": "t"}, tok))["code"] == -32003
    assert err(rpc(a2a, "GetExtendedAgentCard", {}, tok))["code"] == -32007
    params = {"tenant": str(callee.id), "message": _msg(data={}, metadata={"skillId": "echo"}), "configuration": {"taskPushNotificationConfig": {"url": "https://x.example/cb"}}}
    assert err(rpc(a2a, "SendMessage", params, tok))["code"] == -32003


def test_inbound_bounds_and_media_types(a2a, pair):
    _, callee, tok, _ = pair
    t = str(callee.id)
    assert err(send(a2a, tok, t, {"messageId": "m", "role": "ROLE_USER", "parts": [{"url": "https://x.example/f"}]}))["code"] == -32005
    assert err(send(a2a, tok, t, {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "a", "mediaType": "image/png"}]}))["code"] == -32005
    assert err(send(a2a, tok, t, {"messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": "a"}]}))["code"] == -32602
    assert err(send(a2a, tok, t, _msg(text="a", data={"b": 1}, metadata={"skillId": "echo"})))["code"] == -32602
    assert err(send(a2a, tok, t, _msg(data={}, metadata={"skillId": "nope"})))["code"] == -32602
    assert err(send(a2a, tok, t, _msg(data={})))["code"] == -32602  # two skills: must choose
    assert err(send(a2a, tok, "not-a-tenant", _msg(data={})))["code"] == -32602
    e = err(send(a2a, tok, t, _msg(data={}, metadata={"skillId": "echo", FED: {"depth": 2}})))
    assert e["code"] == -32600 and "depth" in e["message"]
    assert "not-a-json" not in json.dumps(err(rpc(a2a, "SendMessage", {"message": {"bogus": 1}}, tok)))


def test_follow_up_messages_to_a_task_are_unsupported(a2a, pair):
    _, callee, tok, _ = pair
    task = ok(send(a2a, tok, callee.id, _msg(data={}, metadata={"skillId": "echo"})))["task"]
    e = err(send(a2a, tok, callee.id, _msg(data={}, metadata={"skillId": "echo"}, task_id=task["id"])))
    assert e["code"] == -32004


def test_internal_errors_never_echo_exception_text(a2a, pair, monkeypatch):
    from services.registry.app.a2a import service

    _, callee, tok, _ = pair

    def boom(*a, **k):
        raise RuntimeError("SECRET-INTERNAL-DETAIL postgres://user:pw@host")

    monkeypatch.setattr(service, "list_tasks", boom)
    r = rpc(a2a, "ListTasks", {"tenant": str(callee.id)}, tok)
    assert err(r)["code"] == -32603 and "SECRET" not in r.text


# ── observability ───────────────────────────────────────────────────────


def test_metrics_and_audit_hold_no_ids_urls_or_content(a2a, pair, db, monkeypatch):
    from prometheus_client import generate_latest

    from services.registry.app.a2a.orm import A2AAuditLog

    caller, callee, tok, _ = pair
    task = ok(send(a2a, tok, callee.id, _msg(text="PRIVATE-CONTENT", metadata={"skillId": "summarize", ECON: {"maxBudget": 10}}), ext=ECON))["task"]
    ok(rpc(a2a, "GetTask", {"id": task["id"], "tenant": str(callee.id)}, tok))
    text = generate_latest().decode()
    assert "agentnet_a2a_requests_total" in text
    for leak in (task["id"], str(callee.id), str(caller.id), "PRIVATE-CONTENT", tok):
        assert leak not in text
    rows = db.query(A2AAuditLog).all()
    assert {r.result for r in rows} >= {"created", "escrow_reserved"}
    assert all("PRIVATE" not in json.dumps(r.detail) for r in rows)
    with pytest.raises(Exception):
        db.execute(__import__("sqlalchemy").text("DELETE FROM a2a_audit_log"))
    db.rollback()


def test_the_a2a_package_never_touches_wallets():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "services" / "registry" / "app" / "a2a"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import Wallet" not in text and " Wallet," not in text and "Wallet)" not in text, path
        assert "reserved_credits" not in text and "balance_credits" not in text, path
