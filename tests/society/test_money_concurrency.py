"""Economic invariants under REAL concurrency (Phase 2.5 §8).

Every test here runs several independent SQLAlchemy sessions against the
same PostgreSQL database from threads that release together on a barrier,
so row locks (``SELECT ... FOR UPDATE``), the UNIQUE index on
``transactions.idempotency_key`` and the balance trigger are exercised for
real — a mock cannot prove any of this.

Money invariant (all tests): for every wallet ``balance >= reserved >= 0``;
the sum of balances across caller+callee never exceeds the starting total
(platform fee can only remove money); a task reaches at most ONE terminal
state and moves money at most ONCE; escrow is either fully released or
fully refunded, never both and never twice.
"""

from __future__ import annotations

import pathlib
import re
import threading
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from services.registry.app.models import TaskSession, Transaction, Wallet, WalletOwnerType
from services.registry.app.task_service import (
    EscrowError,
    confirm_task_completion,
    create_task_with_escrow,
    fail_task_with_refund,
    start_task,
)

CAP = [{"name": "summarize", "version": "1.0", "description": "d", "input_schema": {"type": "object"}, "output_schema": {"type": "object"}, "price": 10}]
PRICE = 10


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _wallet(session, agent_id):
    return session.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == agent_id).first()


def _race(n, fn):
    """Run ``fn(i)`` in n threads released together; return [(ok, value|exc)]."""
    barrier = threading.Barrier(n)
    results = [None] * n

    def _run(i):
        barrier.wait(timeout=10)
        try:
            results[i] = (True, fn(i))
        except BaseException as exc:  # noqa: BLE001 — we classify below
            results[i] = (False, exc)

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(r is not None for r in results), "a racing thread never finished"
    return results


def _in_progress_task(db, SessionLocal, caller, callee):
    task, _tx = create_task_with_escrow(
        db=db, caller_agent=caller, callee_agent_id=callee.id, capability_name="summarize",
        input_data={"q": "x"}, max_budget=PRICE,
    )
    start_task(db=db, task_id=task.id, callee_agent=callee)
    db.commit()
    return task.id


def _assert_wallet_sane(w):
    assert w.reserved_credits >= 0
    assert w.balance_credits >= w.reserved_credits, f"reserved exceeds balance: {w.reserved_credits} > {w.balance_credits}"


@pytest.fixture
def pair(make_agent):
    caller = make_agent("Conc_Caller", balance_credits=100, spending_cap=1000)
    callee = make_agent("Conc_Callee", capabilities=CAP)
    return caller, callee


def test_concurrent_confirm_and_fail_reach_exactly_one_terminal_state(db, SessionLocal, pair):
    caller, callee = pair
    task_id = _in_progress_task(db, SessionLocal, caller, callee)
    start_total = _wallet(db, caller.id).balance_credits + _wallet(db, callee.id).balance_credits

    def _confirm(_):
        s = SessionLocal()
        try:
            return _ev(confirm_task_completion(db=s, callee_agent=callee, task_id=task_id, output={"ok": True}).status)
        finally:
            s.close()

    def _fail(_):
        s = SessionLocal()
        try:
            return _ev(fail_task_with_refund(db=s, task_id=task_id, error_message="boom", callee_agent_id=callee.id).status)
        finally:
            s.close()

    results = _race(2, lambda i: _confirm(i) if i == 0 else _fail(i))
    for ok, val in results:
        if not ok:
            assert isinstance(val, EscrowError), val  # the loser sees an invalid transition, never a 500-class error

    db.expire_all()
    task = db.get(TaskSession, task_id)
    assert _ev(task.status) in ("completed", "failed")
    txs = db.query(Transaction).filter(Transaction.task_session_id == task_id).all()
    assert len(txs) == 1
    cw, kw = _wallet(db, caller.id), _wallet(db, callee.id)
    _assert_wallet_sane(cw)
    _assert_wallet_sane(kw)
    assert cw.reserved_credits == 0, "escrow must be released or refunded exactly once"
    if _ev(task.status) == "completed":
        assert _ev(txs[0].status) == "completed"
        assert cw.balance_credits == 100 - PRICE
        assert 0 < kw.balance_credits <= PRICE
    else:
        assert _ev(txs[0].status) == "cancelled"
        assert cw.balance_credits == 100 and kw.balance_credits == 0
    assert cw.balance_credits + kw.balance_credits <= start_total


def test_double_confirm_moves_money_exactly_once(db, SessionLocal, pair):
    caller, callee = pair
    task_id = _in_progress_task(db, SessionLocal, caller, callee)

    def _confirm(_):
        s = SessionLocal()
        try:
            return _ev(confirm_task_completion(db=s, callee_agent=callee, task_id=task_id, output={"ok": True}).status)
        finally:
            s.close()

    results = _race(4, _confirm)
    assert all(ok and val == "completed" for ok, val in results), results  # idempotent for every racer
    db.expire_all()
    cw, kw = _wallet(db, caller.id), _wallet(db, callee.id)
    assert cw.balance_credits == 100 - PRICE and cw.reserved_credits == 0
    assert 0 < kw.balance_credits <= PRICE, "callee credited exactly once"
    assert db.query(Transaction).filter(Transaction.task_session_id == task_id).count() == 1


def test_double_fail_refunds_exactly_once(db, SessionLocal, pair):
    caller, callee = pair
    task_id = _in_progress_task(db, SessionLocal, caller, callee)

    def _fail(_):
        s = SessionLocal()
        try:
            return _ev(fail_task_with_refund(db=s, task_id=task_id, error_message="x", callee_agent_id=callee.id).status)
        finally:
            s.close()

    results = _race(4, _fail)
    assert all(ok and val == "failed" for ok, val in results), results
    db.expire_all()
    cw = _wallet(db, caller.id)
    assert cw.balance_credits == 100 and cw.reserved_credits == 0, "a refund can never over-release"


def test_same_idempotency_key_raced_creates_one_escrow(db, SessionLocal, pair):
    caller, callee = pair
    key = f"race-{uuid.uuid4()}"

    def _create(_):
        s = SessionLocal()
        try:
            c = s.get(type(caller), caller.id)
            task, tx = create_task_with_escrow(
                db=s, caller_agent=c, callee_agent_id=callee.id, capability_name="summarize",
                input_data={"q": "same"}, max_budget=PRICE, idempotency_key=key,
            )
            return str(task.id)
        finally:
            s.close()

    results = _race(6, _create)
    ids = {val for ok, val in results if ok}
    errors = [val for ok, val in results if not ok]
    assert not any(isinstance(e, IntegrityError) for e in errors), "UNIQUE violations must be absorbed, not leaked"
    assert all(isinstance(e, EscrowError) for e in errors), errors
    assert len(ids) == 1, f"one key must map to one task, got {ids}"
    db.expire_all()
    assert db.query(Transaction).filter(Transaction.idempotency_key == key).count() == 1
    assert db.query(TaskSession).count() == 1
    cw = _wallet(db, caller.id)
    assert cw.reserved_credits == PRICE and cw.balance_credits == 100


def test_same_key_different_payload_is_rejected_even_under_race(db, SessionLocal, pair):
    caller, callee = pair
    key = f"race-{uuid.uuid4()}"

    def _create(i):
        s = SessionLocal()
        try:
            c = s.get(type(caller), caller.id)
            task, _ = create_task_with_escrow(
                db=s, caller_agent=c, callee_agent_id=callee.id, capability_name="summarize",
                input_data={"q": f"payload-{i}"}, max_budget=PRICE, idempotency_key=key,
            )
            return str(task.id)
        finally:
            s.close()

    results = _race(4, _create)
    ids = {val for ok, val in results if ok}
    assert len(ids) == 1
    assert all(isinstance(val, EscrowError) and "different request payload" in str(val) for ok, val in results if not ok)
    db.expire_all()
    assert _wallet(db, caller.id).reserved_credits == PRICE


@pytest.mark.parametrize(
    "balance,cap,expected_wins",
    [
        (100, 35, 3),   # daily cap admits 30, refuses the 4th reservation
        (25, 1000, 2),  # available balance admits 20, refuses the 3rd
        (100, 1000, 8), # neither limit binds
    ],
)
def test_raced_creates_never_exceed_cap_or_balance(db, SessionLocal, make_agent, balance, cap, expected_wins):
    caller = make_agent("Cap_Caller", balance_credits=balance, spending_cap=cap)
    callee = make_agent("Cap_Callee", capabilities=CAP)

    def _create(i):
        s = SessionLocal()
        try:
            c = s.get(type(caller), caller.id)
            task, _ = create_task_with_escrow(
                db=s, caller_agent=c, callee_agent_id=callee.id, capability_name="summarize",
                input_data={"q": f"n{i}"}, max_budget=PRICE,
            )
            return str(task.id)
        finally:
            s.close()

    results = _race(8, _create)
    wins = [val for ok, val in results if ok]
    losses = [val for ok, val in results if not ok]
    assert len(wins) == expected_wins, (wins, losses)
    assert all(isinstance(e, EscrowError) for e in losses), losses
    db.expire_all()
    cw = _wallet(db, caller.id)
    _assert_wallet_sane(cw)
    assert cw.reserved_credits == PRICE * expected_wins
    assert cw.reserved_credits <= cw.balance_credits
    assert cw.daily_spent + cw.reserved_credits <= cw.spending_cap
    assert db.query(TaskSession).count() == expected_wins


def test_society_runtime_never_writes_wallet_balances():
    """Static guard for §8/§3: the Society runtime may READ wallets and may
    CREATE a zero wallet at seed time, but it must never assign balance /
    reserved / cap / spent fields on an existing wallet — that is the
    escrow layer's (task_service + DB trigger) job alone."""
    root = pathlib.Path(__file__).resolve().parents[2] / "services" / "registry" / "app" / "society"
    forbidden = re.compile(r"\.(balance_credits|balance_usdc|reserved_credits|reserved_usdc|spending_cap|daily_spent)\s*[-+*/]?=[^=]")
    offenders = []
    for path in root.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                offenders.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
        text = path.read_text(encoding="utf-8")
        if re.search(r"update\(\s*Wallet\s*\)", text) or "UPDATE wallets" in text.upper():
            offenders.append(f"{path.relative_to(root)}: bulk wallet update")
    assert not offenders, "\n".join(offenders)
