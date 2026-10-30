"""Idempotency keys belong to ONE caller (REST, WebSocket, task_service).

``transactions.idempotency_key`` is globally UNIQUE. Before this fix REST and WS
stored the raw client key, so caller B sending the key caller A had used got
A's task back (the replay lookup matched on the key alone), and B's own work was
never created. A2A already derived its key from the caller's identity.

Money invariant: a key reserves escrow at most once per caller; a replay never
returns, or reserves against, another caller's task or wallet.
"""

from __future__ import annotations

import pathlib
import threading
import uuid

import pytest

from services.registry.app.auth import create_agent_token
from services.registry.app.models import TaskSession, Transaction, Wallet, WalletOwnerType
from services.registry.app.task_service import EscrowError, create_task_with_escrow, scoped_idempotency_key

from .conftest import auth

REPO = pathlib.Path(__file__).resolve().parents[2]
CAP = [{"name": "summarize", "version": "1.0", "description": "d", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 10}]


def _reserved(db, agent):
    db.expire_all()
    return db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == agent.id).first().reserved_credits


def _post(client, agent, callee, key, q="same"):
    body = {"caller_agent_id": str(agent.id), "callee_agent_id": str(callee.id), "capability": "summarize", "input": {"q": q}, "max_budget": 10}
    return client.post("/v1/tasks/", headers={**auth(create_agent_token(agent.id).access_token), "Idempotency-Key": key}, json=body)


def test_two_callers_with_the_same_rest_key_get_their_own_tasks(api_client, db, make_agent):
    a = make_agent("Idem_A", balance_credits=100)
    b = make_agent("Idem_B", balance_credits=100)
    callee = make_agent("Idem_Callee", capabilities=CAP)
    ra = _post(api_client, a, callee, "order-1")
    rb = _post(api_client, b, callee, "order-1")
    assert ra.status_code in (200, 201) and rb.status_code in (200, 201), (ra.text, rb.text)
    assert ra.json()["task_session_id"] != rb.json()["task_session_id"], "B must never receive A's task"
    assert (_reserved(db, a), _reserved(db, b)) == (10, 10)
    # a replay by the same caller is still deduped
    again = _post(api_client, a, callee, "order-1")
    assert again.status_code in (200, 201) and again.json()["task_session_id"] == ra.json()["task_session_id"]
    assert _reserved(db, a) == 10
    assert db.query(TaskSession).count() == 2
    assert db.query(Transaction).filter(Transaction.idempotency_key == "order-1").count() == 0, "a raw client key is never stored"


def test_a_key_used_by_another_caller_is_refused_not_returned(db, make_agent):
    a = make_agent("Own_A", balance_credits=100)
    b = make_agent("Own_B", balance_credits=100)
    callee = make_agent("Own_Callee", capabilities=CAP)
    create_task_with_escrow(db=db, caller_agent=a, callee_agent_id=callee.id, capability_name="summarize",
                            input_data={"q": "x"}, max_budget=10, idempotency_key="shared-internal-key")
    with pytest.raises(EscrowError, match="another caller"):
        create_task_with_escrow(db=db, caller_agent=b, callee_agent_id=callee.id, capability_name="summarize",
                                input_data={"q": "x"}, max_budget=10, idempotency_key="shared-internal-key")
    db.rollback()
    assert _reserved(db, b) == 0


def test_a_raced_foreign_key_never_hands_over_the_winners_task(db, SessionLocal, make_agent):
    """The IntegrityError path (two inserts raced on one key) applies the same owner check."""
    a = make_agent("Race_A", balance_credits=100)
    b = make_agent("Race_B", balance_credits=100)
    callee = make_agent("Race_Callee", capabilities=CAP)
    key = f"k-{uuid.uuid4().hex[:20]}"
    callers = [a, b] * 3
    barrier = threading.Barrier(len(callers))
    out = [None] * len(callers)

    def _run(i):
        s = SessionLocal()
        try:
            c = s.get(type(callers[i]), callers[i].id)
            barrier.wait(timeout=10)
            task, _ = create_task_with_escrow(db=s, caller_agent=c, callee_agent_id=callee.id, capability_name="summarize",
                                              input_data={"q": "x"}, max_budget=10, idempotency_key=key)
            out[i] = ("ok", str(task.caller_agent_id))
        except EscrowError as exc:
            out[i] = ("err", str(exc))
        finally:
            s.close()

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(len(callers))]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert all(o is not None for o in out)
    for i, (kind, val) in enumerate(out):
        if kind == "ok":
            assert val == str(callers[i].id), "a caller only ever gets back its OWN task"
    assert sorted((_reserved(db, a), _reserved(db, b))) == [0, 10], "the key reserved escrow exactly once"


def test_scoped_key_is_per_caller_deterministic_and_fits_the_column():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert scoped_idempotency_key(a, "k") == scoped_idempotency_key(a, "k")
    assert scoped_idempotency_key(a, "k") != scoped_idempotency_key(b, "k")
    assert scoped_idempotency_key(a, "k" * 500).startswith("c:") and len(scoped_idempotency_key(a, "k" * 500)) <= 64
    assert scoped_idempotency_key(a, None) is None and scoped_idempotency_key(a, "") is None


def test_rest_and_ws_scope_client_keys_the_same_way():
    """Parity: both entry points pass the client key through scoped_idempotency_key."""
    rest = (REPO / "services/registry/app/api/routes/tasks.py").read_text()
    ws = (REPO / "services/registry/app/websocket_manager.py").read_text()
    assert "idempotency_key=scoped_idempotency_key(current_agent.id, idempotency_key)" in rest
    assert "idempotency_key=scoped_idempotency_key(agent.id, idem_key)" in ws
