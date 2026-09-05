"""Registry authorization matrix: anonymous / user / owner / other user /
agent JWT / scoped token / operator against every mutating or private surface
that the Phase 2.5 audit flagged. Knowing a UUID must never grant access."""

from __future__ import annotations

import base64
import time
import uuid
from datetime import datetime, timedelta

import ed25519
import pytest

from services.registry.app.auth import create_agent_token, create_user_token
from services.registry.app.models import (
    CurrencyType,
    MemoryItem,
    MemoryScope,
    Offer,
    OfferStatus,
    ScopedToken,
    Span,
    SpanStatus,
    TaskSession,
    TaskStatus,
    Wallet,
    WalletOwnerType,
)

from .conftest import auth

CAP = [{"name": "summarize", "version": "1.0", "description": "d", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 10}]


def _mint_scoped(db, agent, *, actions, cap=100, expires_in=None):
    """Insert a scoped token directly (what POST /v1/tokens does after auth)."""
    import hashlib

    raw = "spt_" + uuid.uuid4().hex
    db.add(ScopedToken(id=uuid.uuid4(), token_hash=hashlib.sha256(raw.encode()).hexdigest(), agent_id=agent.id, resource_type="test", allowed_actions=actions, spending_cap=cap, expires_at=None if expires_in is None else datetime.utcnow() + timedelta(seconds=expires_in)))
    db.commit()
    return raw


def _task(db, caller, callee, status=TaskStatus.COMPLETED, escrow=10):
    t = TaskSession(id=uuid.uuid4(), trace_id=uuid.uuid4(), span_id=uuid.uuid4(), caller_agent_id=caller.id, callee_agent_id=callee.id, capability="summarize", input={}, input_hash="x", escrow_amount=escrow, currency=CurrencyType.CREDITS, status=status, timeout_at=datetime.utcnow() + timedelta(minutes=5))
    db.add(t)
    db.commit()
    return t


# ── scoped tokens ────────────────────────────────────────────────────────


def test_scoped_tokens_never_act_as_the_owning_user(api_client, db, user_token, make_agent):
    owner, _ = user_token(None)
    agent = make_agent("Scoped_A", user=owner)
    raw = _mint_scoped(db, agent, actions=["read"])
    # user-only surfaces refuse the scoped token outright
    r = api_client.post("/v1/agents/", headers=auth(raw), json={"name": "X", "description": "d", "capabilities": [], "endpoint": "http://x", "public_key": "k"})
    assert r.status_code == 403 and "scoped" in r.text
    assert api_client.get("/v1/projects", headers=auth(raw)).status_code == 403
    assert api_client.post("/v1/tokens", headers=auth(raw), json={"agent_id": str(agent.id), "resource_type": "t"}).status_code == 403


def test_token_minting_requires_the_agent_owner(api_client, db, user_token, make_agent):
    owner, owner_tok = user_token(None)
    other, other_tok = user_token(None)
    agent = make_agent("Mint_A", user=owner)
    agent_tok = create_agent_token(agent.id).access_token
    body = {"agent_id": str(agent.id), "resource_type": "domain", "allowed_actions": ["read"]}
    assert api_client.post("/v1/tokens", json=body).status_code == 401
    assert api_client.post("/v1/tokens", headers=auth(other_tok), json=body).status_code == 403
    assert api_client.post("/v1/tokens", headers=auth(agent_tok), json=body).status_code == 401
    r = api_client.post("/v1/tokens", headers=auth(owner_tok), json=body)
    assert r.status_code == 201, r.text
    tok = r.json()
    assert tok["raw_token"].startswith("spt_") and tok["expires_at"] is not None
    expires = datetime.fromisoformat(tok["expires_at"].replace("Z", "+00:00"))
    assert timedelta(days=29) < (expires.replace(tzinfo=None) - datetime.utcnow()) < timedelta(days=31)
    # explicit ttl honoured, absurd ttl refused
    assert api_client.post("/v1/tokens", headers=auth(owner_tok), json={**body, "expires_in": 60}).status_code == 201
    assert api_client.post("/v1/tokens", headers=auth(owner_tok), json={**body, "expires_in": 10 ** 9}).status_code == 422
    # read/revoke are owner-only and do not reveal existence
    assert api_client.get(f"/v1/tokens/{tok['id']}", headers=auth(other_tok)).status_code == 404
    assert api_client.delete(f"/v1/tokens/{tok['id']}", headers=auth(other_tok)).status_code == 404
    assert api_client.get(f"/v1/tokens/{tok['id']}", headers=auth(owner_tok)).status_code == 200
    assert api_client.delete(f"/v1/tokens/{tok['id']}", headers=auth(owner_tok)).status_code == 204
    assert api_client.get("/v1/tasks/", headers=auth(tok["raw_token"])).status_code == 401, "revoked token must stop working"


def test_scoped_token_execute_action_and_spending_cap(api_client, db, user_token, make_agent):
    owner, _ = user_token(None)
    caller = make_agent("Cap_Caller", user=owner, balance_credits=100)
    callee = make_agent("Cap_Callee", capabilities=CAP)
    body = {"caller_agent_id": str(caller.id), "callee_agent_id": str(callee.id), "capability": "summarize", "input": {}, "max_budget": 10}
    read_only = _mint_scoped(db, caller, actions=["read"], cap=100)
    r = api_client.post("/v1/tasks/", headers=auth(read_only), json=body)
    assert r.status_code == 403 and "execute" in r.text
    execute = _mint_scoped(db, caller, actions=["execute"], cap=15)
    r1 = api_client.post("/v1/tasks/", headers=auth(execute), json=body)
    assert r1.status_code in (200, 201), r1.text
    r2 = api_client.post("/v1/tasks/", headers=auth(execute), json=body)
    assert r2.status_code == 403 and "spending cap" in r2.text
    db.expire_all()
    spt = db.query(ScopedToken).filter(ScopedToken.agent_id == caller.id, ScopedToken.spending_cap == 15).first()
    assert spt.total_spent == 10
    wallet = db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == caller.id).first()
    assert wallet.reserved_credits == 10, "exactly one escrow was locked"


# ── orchestrator ─────────────────────────────────────────────────────────


def test_orchestrator_is_off_by_default(api_client, user_token):
    _, op_tok = user_token("operator")
    assert api_client.get("/v1/orchestrator/partners", headers=auth(op_tok)).status_code == 404
    assert api_client.post("/v1/orchestrator/oauth/authorize", data={"client_id": "x", "client_secret": "y", "redirect_uri": "http://r"}).status_code == 404
    assert api_client.post("/v1/orchestrator/provision", json={"client_id": "x", "client_secret": "y", "user_email": "n@x.test", "project_name": "p"}).status_code == 404


def test_orchestrator_partner_flow_when_enabled(api_client, db, user_token, make_user, monkeypatch):
    from services.registry.app.api.routes import orchestrator as orch

    monkeypatch.setattr(orch, "ORCHESTRATOR_ENABLED", True)
    orch._OATH_CODES.clear()
    _, user_tok = user_token(None)
    _, op_tok = user_token("operator")
    assert api_client.post("/v1/orchestrator/partners", headers=auth(user_tok), json={"name": "p"}).status_code == 403
    assert api_client.get("/v1/orchestrator/partners", headers=auth(user_tok)).status_code == 403
    r = api_client.post("/v1/orchestrator/partners", headers=auth(op_tok), json={"name": "p"})
    assert r.status_code == 201
    cid, secret = r.json()["client_id"], r.json()["client_secret"]
    # authorize needs the secret; a public client_id alone is not authority
    assert api_client.post("/v1/orchestrator/oauth/authorize", data={"client_id": cid, "redirect_uri": "http://r"}).status_code == 422
    assert api_client.post("/v1/orchestrator/oauth/authorize", data={"client_id": cid, "client_secret": "wrong", "redirect_uri": "http://r"}).status_code == 401
    code = api_client.post("/v1/orchestrator/oauth/authorize", data={"client_id": cid, "client_secret": secret, "redirect_uri": "http://r"}).json()["authorization_code"]
    # existing accounts can never be taken over by e-mail
    victim = make_user("victim@authz.test")
    assert api_client.post("/v1/orchestrator/oauth/token", data={"code": code, "client_id": cid, "user_email": victim.email}).status_code == 409
    # the code was consumed by the attempt; a fresh one provisions a NEW account only
    code = api_client.post("/v1/orchestrator/oauth/authorize", data={"client_id": cid, "client_secret": secret, "redirect_uri": "http://r"}).json()["authorization_code"]
    assert api_client.post("/v1/orchestrator/oauth/token", data={"code": code, "client_id": "someone-else", "user_email": "new@authz.test"}).status_code == 400
    # a mismatched client_id BURNS the code (RFC 6749 §4.1.2: deny and revoke), so the real partner needs a fresh one
    assert api_client.post("/v1/orchestrator/oauth/token", data={"code": code, "client_id": cid, "user_email": "new@authz.test"}).status_code == 400
    code = api_client.post("/v1/orchestrator/oauth/authorize", data={"client_id": cid, "client_secret": secret, "redirect_uri": "http://r"}).json()["authorization_code"]
    r = api_client.post("/v1/orchestrator/oauth/token", data={"code": code, "client_id": cid, "user_email": "new@authz.test"})
    assert r.status_code == 200 and r.json()["scoped_token"].startswith("spt_")
    assert api_client.post("/v1/orchestrator/oauth/token", data={"code": code, "client_id": cid, "user_email": "again@authz.test"}).status_code == 400, "codes are single-use"
    assert api_client.post("/v1/orchestrator/provision", json={"client_id": cid, "client_secret": "nope", "user_email": "x@authz.test", "project_name": "p"}).status_code == 401


# ── agent login replay ───────────────────────────────────────────────────


def test_agent_login_rejects_stale_signatures(api_client, db, make_agent):
    sk, vk = ed25519.create_keypair()
    agent = make_agent("Signer")
    agent.public_key = base64.b64encode(vk.to_bytes()).decode()
    db.commit()

    def login(ts: str):
        sig = base64.b64encode(sk.sign(f"{agent.id}:{ts}".encode())).decode()
        return api_client.post("/v1/auth/agent/login", json={"agent_id": str(agent.id), "signature": sig, "timestamp": ts})

    assert login(str(int(time.time()) - 3600)).status_code == 401, "an hour-old signature is a replay"
    assert login("not-a-time").status_code == 401
    fresh = login(str(int(time.time())))
    assert fresh.status_code == 200 and fresh.json()["access_token"]


# ── agents: reputation tampering ─────────────────────────────────────────


def test_report_requires_a_finished_task_you_called(api_client, db, make_agent):
    x = make_agent("Reporter")
    y = make_agent("Target", capabilities=CAP)
    z = make_agent("Bystander")
    x_tok = create_agent_token(x.id).access_token
    z_tok = create_agent_token(z.id).access_token
    unrelated = _task(db, z, y)
    assert api_client.post(f"/v1/agents/{y.id}/report", headers=auth(x_tok), json={"task_session_id": str(unrelated.id), "success": False, "rating": 1, "feedback": "timeout"}).status_code == 403
    assert api_client.post(f"/v1/agents/{y.id}/report", headers=auth(x_tok), json={"task_session_id": str(uuid.uuid4()), "success": False, "rating": 1}).status_code == 403
    running = _task(db, x, y, status=TaskStatus.IN_PROGRESS)
    assert api_client.post(f"/v1/agents/{y.id}/report", headers=auth(x_tok), json={"task_session_id": str(running.id), "success": False, "rating": 1}).status_code == 409
    done = _task(db, x, y)
    assert api_client.post(f"/v1/agents/{y.id}/report", headers=auth(x_tok), json={"task_session_id": str(done.id), "success": True, "rating": 5}).status_code == 200
    assert api_client.post(f"/v1/agents/{y.id}/report", headers=auth(z_tok), json={"task_session_id": str(done.id), "success": False, "rating": 1}).status_code == 403


def test_verify_capability_is_owner_only(api_client, db, user_token, make_agent):
    owner, _ = user_token(None)
    _, other_tok = user_token(None)
    agent = make_agent("Verify_Me", user=owner, capabilities=CAP)
    body = {"capability": "summarize", "test_input": {}, "expected_output_schema": {"type": "object"}}
    assert api_client.post(f"/v1/agents/{agent.id}/verify-capability", headers=auth(other_tok), json=body).status_code == 403
    assert api_client.post(f"/v1/agents/{uuid.uuid4()}/verify-capability", headers=auth(other_tok), json=body).status_code == 404


def test_public_agent_views_hide_owner_ids(api_client, db, make_agent):
    agent = make_agent("Public_View", capabilities=CAP)
    one = api_client.get(f"/v1/agents/{agent.id}").json()
    assert one["name"] == "Public_View" and "user_id" not in one
    listing = api_client.get("/v1/agents/public/").json()
    assert listing and all("user_id" not in a for a in listing)


# ── chat ─────────────────────────────────────────────────────────────────


def test_chat_is_scoped_to_parties(api_client, db, user_token, make_agent):
    ua, ua_tok = user_token(None)
    ub, ub_tok = user_token(None)
    uc, uc_tok = user_token(None)
    a1 = make_agent("Chat_A1", user=ua)
    b1 = make_agent("Chat_B1", user=ub)
    a1_tok = create_agent_token(a1.id).access_token
    r = api_client.post("/v1/chat/", headers=auth(a1_tok), json={"to_agent_id": str(b1.id), "title": "hi", "content": "SECRET-BODY"})
    assert r.status_code == 201
    mid = r.json()["id"]
    # a user cannot send as an agent it does not own
    assert api_client.post("/v1/chat/", headers=auth(ua_tok), json={"title": "x", "content": "y", "from_agent_name": "Chat_B1"}).status_code == 403
    assert api_client.post("/v1/chat/", headers=auth(ua_tok), json={"title": "x", "content": "y"}).status_code == 422
    assert api_client.post("/v1/chat/", headers=auth(ua_tok), json={"title": "x", "content": "y", "from_agent_name": "Chat_A1", "to_agent_id": str(b1.id)}).status_code == 201
    # bystander sees nothing, parties see the message
    assert api_client.get("/v1/chat/").status_code == 401
    assert api_client.get("/v1/chat/", headers=auth(uc_tok)).json() == []
    assert api_client.get("/v1/chat/threads").status_code == 401
    assert api_client.get("/v1/chat/threads", headers=auth(uc_tok)).json() == []
    assert "SECRET-BODY" not in api_client.get("/v1/chat/", headers=auth(uc_tok)).text
    assert any(m["id"] == mid for m in api_client.get("/v1/chat/", headers=auth(ub_tok)).json())
    assert any(m["id"] == mid for t in api_client.get("/v1/chat/threads", headers=auth(ua_tok)).json() for m in t["messages"])
    # only the recipient can mark read; unread-count works
    assert api_client.get("/v1/chat/unread-count", headers=auth(ub_tok)).json()["unread"] == 2
    assert api_client.post(f"/v1/chat/{mid}/read", headers=auth(a1_tok)).status_code == 403
    assert api_client.post(f"/v1/chat/{mid}/read", headers=auth(uc_tok)).status_code == 403
    assert api_client.post(f"/v1/chat/{mid}/read", headers=auth(ub_tok)).status_code == 200
    assert api_client.get("/v1/chat/unread-count", headers=auth(ub_tok)).json()["unread"] == 1


def test_public_fleet_feed_has_no_message_bodies(api_client, db, make_agent):
    a1 = make_agent("Feed_A1")
    b1 = make_agent("Feed_B1")
    api_client.post("/v1/chat/", headers=auth(create_agent_token(a1.id).access_token), json={"to_agent_id": str(b1.id), "title": "t", "content": "PRIVATE-FEED-BODY"})
    r = api_client.get("/v1/fleet/activity")
    assert r.status_code == 200 and "PRIVATE-FEED-BODY" not in r.text
    assert all("content" not in m for m in r.json().get("chat_messages", []))


# ── memory / lessons / goals / improvements ──────────────────────────────


def test_memory_visibility_and_society_scope_writes(api_client, db, user_token, make_agent):
    ua, ua_tok = user_token(None)
    ub, ub_tok = user_token(None)
    _, op_tok = user_token("operator")
    a1 = make_agent("Mem_A1", user=ua)
    private = MemoryItem(id=uuid.uuid4(), agent_id=a1.id, scope=MemoryScope.AGENT, title="private lesson", content="PRIVATE-MEMORY", tags=[], importance=50)
    shared = MemoryItem(id=uuid.uuid4(), agent_id=None, scope=MemoryScope.SOCIETY, title="shared", content="shared lesson", tags=[], importance=50)
    db.add_all([private, shared])
    db.commit()
    assert api_client.get("/v1/memory/").status_code == 401
    ids_b = {m["id"] for m in api_client.get("/v1/memory/", headers=auth(ub_tok)).json()}
    assert str(shared.id) in ids_b and str(private.id) not in ids_b
    assert api_client.get(f"/v1/memory/{private.id}", headers=auth(ub_tok)).status_code == 404
    assert api_client.get(f"/v1/memory/{private.id}", headers=auth(ua_tok)).status_code == 200
    assert "PRIVATE-MEMORY" not in api_client.get(f"/v1/agents/{a1.id}/lessons", headers=auth(ub_tok)).text
    assert api_client.get(f"/v1/agents/{a1.id}/lessons", headers=auth(ub_tok), params={"include_society": "false"}).status_code == 403
    assert "PRIVATE-MEMORY" in api_client.get(f"/v1/agents/{a1.id}/lessons", headers=auth(ua_tok)).text
    assert api_client.get(f"/v1/agents/{a1.id}/lessons").status_code == 401
    society_body = {"scope": "SOCIETY", "title": "rule", "content": "everyone reads this", "tags": [], "importance": 60}
    assert api_client.post("/v1/memory/", headers=auth(ub_tok), json=society_body).status_code == 403
    assert api_client.post("/v1/memory/", headers=auth(op_tok), json=society_body).status_code == 201


def test_society_goals_need_the_operator_role(api_client, db, user_token):
    user, user_tok = user_token(None)
    op, op_tok = user_token("operator")
    body = {"title": "society goal", "owner_type": "SOCIETY", "owner_id": str(op.id)}
    assert api_client.post("/v1/goals/", headers=auth(user_tok), json=body).status_code == 403
    assert api_client.post("/v1/goals/", headers=auth(op_tok), json=body).status_code == 201


def test_improvement_governance_is_operator_only_and_conversion_locks_escrow(api_client, db, user_token, make_agent):
    proposer, proposer_tok = user_token(None)
    other, other_tok = user_token(None)
    op, op_tok = user_token("operator")
    r = api_client.post("/v1/improvements/", headers=auth(proposer_tok), json={"title": "Fix flaky thing", "problem": "p", "proposed_change": "c"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert api_client.get("/v1/improvements/").status_code == 401
    assert api_client.get(f"/v1/improvements/{pid}", headers=auth(other_tok)).status_code == 200
    assert api_client.post(f"/v1/improvements/{pid}/approve", headers=auth(other_tok)).status_code == 403
    assert api_client.post(f"/v1/improvements/{pid}/mark-implemented", headers=auth(other_tok)).status_code == 403
    assert api_client.post(f"/v1/improvements/{pid}/reject", headers=auth(other_tok)).status_code == 403
    assert api_client.post(f"/v1/improvements/{pid}/approve", headers=auth(op_tok)).status_code == 200
    # conversion: operator only, and the task goes through the escrow path
    caller = make_agent("Conv_Caller", user=op, balance_credits=100)
    callee = make_agent("Conv_Callee", capabilities=CAP)
    body = {"callee_agent_id": str(callee.id), "capability": "summarize", "escrow_amount": 10, "timeout_minutes": 30}
    assert api_client.post(f"/v1/improvements/{pid}/convert-to-task", headers=auth(other_tok), json=body).status_code == 403
    r = api_client.post(f"/v1/improvements/{pid}/convert-to-task", headers=auth(op_tok), json=body)
    assert r.status_code == 200, r.text
    task_id = r.json()["converted_task_id"]
    db.expire_all()
    task = db.query(TaskSession).filter(TaskSession.id == uuid.UUID(task_id)).first()
    wallet = db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == caller.id).first()
    assert task is not None and task.escrow_amount == 10 and wallet.reserved_credits == 10, "escrow must be locked when a proposal becomes a paid task"
    # a proposer can reject their own fresh proposal; strangers cannot
    r2 = api_client.post("/v1/improvements/", headers=auth(proposer_tok), json={"title": "Another", "problem": "p"})
    pid2 = r2.json()["id"]
    assert api_client.post(f"/v1/improvements/{pid2}/reject", headers=auth(proposer_tok), json={"reason": "no"}).status_code == 200


def test_reflect_requires_task_party(api_client, db, user_token, make_agent):
    ua, ua_tok = user_token(None)
    ub, ub_tok = user_token(None)
    a1 = make_agent("Ref_A1", user=ua)
    b1 = make_agent("Ref_B1", user=ub)
    c1 = make_agent("Ref_C1")
    t = _task(db, a1, c1, status=TaskStatus.FAILED)
    assert api_client.post("/v1/improvements/reflect", headers=auth(ub_tok), json={"task_id": str(t.id)}).status_code == 403
    assert api_client.post("/v1/improvements/reflect", headers=auth(ua_tok), json={"task_id": str(t.id)}).status_code == 200


# ── projects / offers / tasks / traces ───────────────────────────────────


def test_projects_are_tenant_scoped(api_client, db, user_token, make_agent):
    ua, ua_tok = user_token(None)
    ub, ub_tok = user_token(None)
    a1 = make_agent("Proj_A1", user=ua)
    b1 = make_agent("Proj_B1", user=ub)
    assert api_client.post("/v1/projects", headers=auth(ua_tok), json={"name": "p", "agent_id": str(b1.id)}).status_code == 403
    r = api_client.post("/v1/projects", headers=auth(ua_tok), json={"name": "p", "agent_id": str(a1.id)})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert api_client.get("/v1/projects", headers=auth(ub_tok)).json() == []
    assert api_client.get(f"/v1/projects/{pid}", headers=auth(ub_tok)).status_code == 404
    assert api_client.get(f"/v1/projects/{pid}/state", headers=auth(ub_tok)).status_code == 404
    assert api_client.post(f"/v1/projects/{pid}/resources", headers=auth(ub_tok), json={"resource_type": "db", "resource_ref": "x", "provider": "p"}).status_code == 404
    assert api_client.get(f"/v1/projects/{pid}/state", headers=auth(ua_tok)).status_code == 200


def test_offer_negotiation_is_parties_only(api_client, db, user_token, make_agent):
    ua, ua_tok = user_token(None)
    _, uc_tok = user_token(None)
    a1 = make_agent("Off_A1", user=ua)
    b1 = make_agent("Off_B1")
    cols = set(Offer.__table__.columns.keys())
    fields = {"id": uuid.uuid4(), "from_agent_id": a1.id, "to_agent_id": b1.id, "title": "deal", "description": "PRIVATE-TERMS", "price": 5, "currency": CurrencyType.CREDITS, "status": OfferStatus.PENDING, "expires_at": datetime.utcnow() + timedelta(hours=1)}
    offer = Offer(**{k: v for k, v in fields.items() if k in cols})
    db.add(offer)
    db.commit()
    assert api_client.get(f"/v1/offers/{offer.id}").status_code == 401
    assert api_client.get(f"/v1/offers/{offer.id}", headers=auth(uc_tok)).status_code == 403
    assert api_client.get(f"/v1/offers/{offer.id}", headers=auth(ua_tok)).status_code == 200


def test_tasks_and_traces_are_owner_scoped(api_client, db, user_token, make_agent):
    ua, ua_tok = user_token(None)
    ub, ub_tok = user_token(None)
    a1 = make_agent("Tr_A1", user=ua)
    a2 = make_agent("Tr_A2", user=ua)
    b1 = make_agent("Tr_B1", user=ub)
    t = _task(db, a2, b1)  # the user's SECOND agent is the caller
    assert api_client.get(f"/v1/tasks/{t.id}", headers=auth(ua_tok)).status_code == 200, "any owned agent may be the party"
    _, uc_tok = user_token(None)
    assert api_client.get(f"/v1/tasks/{t.id}", headers=auth(uc_tok)).status_code == 403
    trace = uuid.uuid4()
    db.add(Span(id=uuid.uuid4(), trace_id=trace, span_id=uuid.uuid4(), agent_id=a1.id, event="task", capability="summarize", duration_ms=1, status=SpanStatus.SUCCESS, credits_used=0, extra_data={"input": "PRIVATE-SPAN"}))
    db.commit()
    assert api_client.get(f"/v1/tasks/traces/{trace}", headers=auth(ub_tok)).status_code == 404
    r = api_client.get(f"/v1/tasks/traces/{trace}", headers=auth(ua_tok))
    assert r.status_code == 200 and r.json()["total_spans"] == 1


# ── misc surfaces ────────────────────────────────────────────────────────


def test_stale_double_prefixed_health_router_is_gone(api_client):
    assert api_client.get("/v1/v1/health/deep").status_code == 404
    assert api_client.get("/healthz").status_code == 200


def test_task_timeline_websocket_is_operator_only(api_client, user_token, agent_token):
    from starlette.websockets import WebSocketDisconnect

    _, user_tok = user_token(None)
    _, ag_tok = agent_token()
    for tok in (user_tok, ag_tok, "garbage"):
        with pytest.raises(WebSocketDisconnect):
            with api_client.websocket_connect(f"/v1/ws/tasks/timeline?token={tok}"):
                pass
    # the retired double-prefixed path no longer exists
    with pytest.raises(Exception):
        with api_client.websocket_connect(f"/v1/ws/ws/tasks/timeline?token={user_tok}"):
            pass


# ── anonymous agent self-registration ───────────────────────────────────


def test_public_register_is_off_by_default_and_gated(api_client, db, monkeypatch):
    from services.registry.app import config as cfg
    from services.registry.app.api.routes import agents as agents_routes  # noqa: F401 — route reads the flag at call time
    from services.registry.app.models import Agent, User, Wallet

    body = {"name": "Anon_Agent", "description": "d", "capabilities": [], "endpoint": "https://anon.example.invalid/hook", "public_key": "k" * 44}
    assert api_client.post("/v1/agents/public-register", json=body).status_code == 404, "OFF by default"
    monkeypatch.setattr(cfg, "PUBLIC_AGENT_REGISTRATION_ENABLED", True)
    r = api_client.post("/v1/agents/public-register", json=body)
    assert r.status_code == 201, r.text
    agent = db.query(Agent).filter(Agent.name == "Anon_Agent").first()
    assert agent is not None and str(getattr(agent.status, "value", agent.status)) == "unverified"
    wallet = db.query(Wallet).filter(Wallet.owner_id == agent.id).first()
    assert wallet.balance_credits == 0 and wallet.reserved_credits == 0
    owner = db.get(User, agent.user_id)
    assert owner.email.endswith("@agentnet.invalid") and owner.password_hash == ""
    # the placeholder owner can never log in — and the attempt is a clean 401, never a 500
    r = api_client.post("/v1/auth/user/login", json={"email": owner.email, "password": "anything"})
    assert r.status_code == 401, r.text
    r = api_client.post("/v1/auth/user/login", json={"email": owner.email, "password": ""})
    assert r.status_code in (400, 401, 422), r.text
