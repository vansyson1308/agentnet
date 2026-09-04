"""Live-service end-to-end proof (class EXTERNAL-SERVICE in docs/TEST_MATRIX.md).

Runs against a REAL registry + payment + worker (REGISTRY_URL / PAYMENT_URL)
and the database behind them (POSTGRES_*). It is executed by the
fresh-install harness (tests/fresh_install/run_fresh_install.py) against the
services it boots, and can be pointed at a local `docker compose up` stack.

Every step asserts — nothing here "prints and moves on" — and the only skip
is an explicit "no live registry reachable" at module import.

Flow proven (money invariants included):
  register → login refused until e-mail verified → verify via the real
  link route → login → register two agents (Ed25519 key for the callee) →
  dev-fund the caller wallet through the payment service → create a task
  (escrow RESERVED, not spent) → callee agent logs in by signature, starts
  and confirms → trigger moves the money exactly once, spans are queryable
  → a 1-second task is refunded by the auto-refund worker → another tenant
  cannot spend the first tenant's wallet → ledger-wide invariants hold.
"""

from __future__ import annotations

import base64
import os
import time
import uuid

import httpx
import pytest

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:8000")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://localhost:8001")
PG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": int(os.getenv("POSTGRES_PORT", "5432")),
    "user": os.getenv("POSTGRES_USER", "agentnet"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
    "dbname": os.getenv("POSTGRES_DB", "agentnet"),
}
PASSWORD = "Integration-Pass-123"  # registry policy: 12+ chars, upper, lower, digit
PRICE = 10
RUN = uuid.uuid4().hex[:8]


def _live(url: str) -> bool:
    try:
        return httpx.get(f"{url}/healthz", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


if not _live(REGISTRY_URL):
    pytest.skip(
        f"no live registry at {REGISTRY_URL} (REGISTRY_URL) — run through tests/fresh_install/run_fresh_install.py "
        "or against `docker compose up`",
        allow_module_level=True,
    )


def query(sql: str, params=()):
    import psycopg2

    conn = psycopg2.connect(**PG)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def wallet_of(agent_id: str):
    rows = query("SELECT id, balance_credits, reserved_credits FROM wallets WHERE owner_type = 'agent' AND owner_id = %s", (agent_id,))
    assert rows, f"no wallet for agent {agent_id}"
    return {"id": str(rows[0][0]), "balance": int(rows[0][1]), "reserved": int(rows[0][2])}


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def reg():
    return httpx.Client(base_url=REGISTRY_URL, timeout=30.0)


@pytest.fixture(scope="module")
def pay():
    return httpx.Client(base_url=PAYMENT_URL, timeout=30.0)


def make_verified_user(reg, label: str) -> dict:
    email = f"it-{RUN}-{label}@example.com"
    r = reg.post("/v1/auth/user/register", json={"email": email, "password": PASSWORD, "phone": "+15550000000"})
    assert r.status_code == 201, f"register: {r.status_code} {r.text}"
    r = reg.post("/v1/auth/user/login", data={"username": email, "password": PASSWORD})
    assert r.status_code == 403, f"login before verification must be refused: {r.status_code} {r.text}"
    # Complete verification the way a clicked link does: the pending token is
    # read from the database (no mailbox here) and exchanged via the REAL route.
    rows = query(
        "SELECT t.token FROM email_verification_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE u.email = %s AND t.consumed_at IS NULL ORDER BY t.created_at DESC LIMIT 1",
        (email,),
    )
    assert rows, "no verification token issued"
    r = reg.get("/v1/auth/verify-email", params={"token": rows[0][0]})
    assert r.status_code == 200, f"verify-email: {r.status_code} {r.text}"
    assert reg.get("/v1/auth/verify-email", params={"token": rows[0][0]}).status_code == 400, "tokens are single-use"
    r = reg.post("/v1/auth/user/login", data={"username": email, "password": PASSWORD})
    assert r.status_code == 200, f"login: {r.status_code} {r.text}"
    return {"email": email, "token": r.json()["access_token"]}


@pytest.fixture(scope="module")
def user(reg):
    return make_verified_user(reg, "owner")


@pytest.fixture(scope="module")
def other_user(reg):
    return make_verified_user(reg, "other")


def register_agent(reg, token: str, name: str, capabilities: list, public_key: str) -> dict:
    r = reg.post(
        "/v1/agents/",
        json={"name": name, "description": "integration", "capabilities": capabilities, "endpoint": "https://agent.example.invalid/hook", "public_key": public_key},
        headers=auth(token),
    )
    assert r.status_code == 201, f"agent register: {r.status_code} {r.text}"
    return r.json()


@pytest.fixture(scope="module")
def agents(reg, pay, user):
    import ed25519

    sk, vk = ed25519.create_keypair()
    caller = register_agent(reg, user["token"], f"it-caller-{RUN}", [], "caller-has-no-key")
    callee = register_agent(
        reg,
        user["token"],
        f"it-callee-{RUN}",
        [{"name": "summarize", "version": "1.0", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": PRICE}],
        base64.b64encode(vk.to_bytes()).decode(),
    )
    caller_wallet = wallet_of(caller["id"])
    r = pay.post(f"/v1/wallets/{caller_wallet['id']}/fund", json={"amount": 100, "currency": "credits"}, headers=auth(user["token"]))
    assert r.status_code == 200, f"dev funding: {r.status_code} {r.text}"
    assert wallet_of(caller["id"])["balance"] == 100
    return {"caller": caller, "callee": callee, "callee_sk": sk}


def agent_token(reg, agent_id: str, sk) -> str:
    ts = str(int(time.time()))
    sig = base64.b64encode(sk.sign(f"{agent_id}:{ts}".encode())).decode()
    r = reg.post("/v1/auth/agent/login", json={"agent_id": agent_id, "signature": sig, "timestamp": ts})
    assert r.status_code == 200, f"agent login: {r.status_code} {r.text}"
    return r.json()["access_token"]


def create_task(reg, token: str, agents: dict, *, timeout_seconds: int, idem: str | None = None) -> dict:
    headers = auth(token)
    if idem:
        headers["Idempotency-Key"] = idem
    r = reg.post(
        "/v1/tasks/",
        json={
            "caller_agent_id": agents["caller"]["id"],
            "callee_agent_id": agents["callee"]["id"],
            "capability": "summarize",
            "input": {"text": "hello"},
            "max_budget": PRICE,
            "currency": "credits",
            "timeout_seconds": timeout_seconds,
        },
        headers=headers,
    )
    assert r.status_code == 201, f"task create: {r.status_code} {r.text}"
    return r.json()


def task_row(task_id: str):
    rows = query("SELECT status, escrow_amount FROM task_sessions WHERE id = %s", (task_id,))
    assert rows, "task row missing"
    return {"status": rows[0][0], "escrow": int(rows[0][1])}


# ── tests ──────────────────────────────────────────────────────────────


def test_01_services_ready(reg, pay):
    for client in (reg, pay):
        r = client.get("/readyz")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ready"


def test_02_verified_user_has_token(user):
    assert user["token"]


def test_03_agents_have_empty_wallets_until_funded(agents):
    callee = wallet_of(agents["callee"]["id"])
    assert callee == {"id": callee["id"], "balance": 0, "reserved": 0}
    assert wallet_of(agents["caller"]["id"])["balance"] == 100


def test_04_escrow_lifecycle_moves_money_exactly_once(reg, user, agents):
    caller, callee = agents["caller"]["id"], agents["callee"]["id"]
    key = f"it-{RUN}-lifecycle"
    created = create_task(reg, user["token"], agents, timeout_seconds=300, idem=key)
    task_id = created["task_session_id"]
    assert task_row(task_id) == {"status": "initiated", "escrow": PRICE}
    assert wallet_of(caller) == {**wallet_of(caller), "balance": 100, "reserved": PRICE}, "escrow is reserved, not spent"

    # Same Idempotency-Key + same payload → the SAME task, no second reservation
    again = create_task(reg, user["token"], agents, timeout_seconds=300, idem=key)
    assert again["task_session_id"] == task_id
    assert wallet_of(caller)["reserved"] == PRICE

    tok = agent_token(reg, callee, agents["callee_sk"])
    r = reg.put(f"/v1/tasks/{task_id}/start", headers=auth(tok))
    assert r.status_code == 200, r.text
    assert task_row(task_id)["status"] == "in_progress"

    r = reg.put(f"/v1/tasks/{task_id}/confirm", json={"summary": "done"}, headers=auth(tok))
    assert r.status_code == 200, r.text
    assert task_row(task_id)["status"] == "completed"
    cw, kw = wallet_of(caller), wallet_of(callee)
    assert cw["reserved"] == 0 and cw["balance"] == 100 - PRICE, cw
    assert 0 < kw["balance"] <= PRICE, kw  # platform fee may apply; never more than the escrow
    tx = query("SELECT status, from_wallet, to_wallet FROM transactions WHERE task_session_id = %s", (task_id,))
    assert len(tx) == 1 and tx[0][0] == "completed" and tx[0][1] is not None and tx[0][2] is not None

    # confirming twice is idempotent: no second payout
    r = reg.put(f"/v1/tasks/{task_id}/confirm", json={"summary": "done"}, headers=auth(tok))
    assert r.status_code == 200, r.text
    assert wallet_of(callee)["balance"] == kw["balance"]

    # spans are persisted and queryable by the owner
    r = reg.get(f"/v1/tasks/traces/{created['trace_id']}", headers=auth(user["token"]))
    assert r.status_code == 200, r.text
    events = {s.get("event") for s in r.json().get("spans", r.json() if isinstance(r.json(), list) else [])}
    assert {"task_created", "task_completed"} <= events, events


def test_05_worker_refunds_a_timed_out_task(reg, user, agents):
    caller = agents["caller"]["id"]
    before = wallet_of(caller)
    # 3 s: long enough to observe the reservation before the worker (2 s poll
    # in the harness) can possibly refund it, short enough to wait for.
    created = create_task(reg, user["token"], agents, timeout_seconds=3)
    task_id = created["task_session_id"]
    assert wallet_of(caller)["reserved"] == before["reserved"] + PRICE
    deadline = time.time() + 90  # worker polls every WORKER_POLL_INTERVAL_SEC (30s default, 2s in the harness)
    status = None
    while time.time() < deadline:
        status = task_row(task_id)["status"]
        if status in ("timeout", "refunded", "failed"):
            break
        time.sleep(1)
    assert status in ("timeout", "refunded", "failed"), f"worker never refunded the task (status={status})"
    after = wallet_of(caller)
    assert after["reserved"] == before["reserved"], "the reservation was released"
    assert after["balance"] == before["balance"], "a refund never changes the balance"
    tx = query("SELECT status FROM transactions WHERE task_session_id = %s", (task_id,))
    assert tx and tx[0][0] == "cancelled", tx


def test_06_other_tenant_cannot_spend_this_wallet(reg, other_user, agents):
    r = reg.post(
        "/v1/tasks/",
        json={
            "caller_agent_id": agents["caller"]["id"],
            "callee_agent_id": agents["callee"]["id"],
            "capability": "summarize",
            "input": {},
            "max_budget": PRICE,
            "currency": "credits",
            "timeout_seconds": 60,
        },
        headers=auth(other_user["token"]),
    )
    assert r.status_code in (403, 404), f"cross-tenant task creation must be refused: {r.status_code} {r.text}"
    assert reg.get(f"/v1/agents/{agents['caller']['id']}", headers=auth(other_user["token"])).json().get("user_id") is None, "public views hide the owner"


def test_07_ledger_invariants_hold():
    assert query("SELECT count(*) FROM wallets WHERE reserved_credits < 0 OR reserved_usdc < 0")[0][0] == 0
    assert query("SELECT count(*) FROM wallets WHERE reserved_credits > balance_credits")[0][0] == 0
    assert query("SELECT count(*) FROM transactions WHERE status = 'completed' AND (from_wallet IS NULL OR to_wallet IS NULL)")[0][0] == 0
