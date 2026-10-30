"""Direct payment-service transactions (``/v1/transactions/create`` + ``/confirm``).

Two defects a reviewer found in this path, each proven here in isolation:

1. NEGATIVE amounts. ``TransactionCreate.amount`` was a bare ``int``; the balance
   trigger subtracts ``amount`` from the payer and adds it to the payee, so a
   payer who created and confirmed ``amount=-50`` PULLED 50 credits out of the
   payee's wallet.
2. ESCROWED funds. The balance check compared against the TOTAL balance, not
   the balance net of ``reserved_credits``, so credits locked as escrow for a
   task could be spent directly (the task's settlement then had nothing left).

Money invariant (all tests): for every wallet ``balance >= reserved >= 0``; a
direct transfer moves ``amount > 0`` from payer to payee and never touches
another wallet's escrow; the payer's daily spend + reservations never exceed
its cap; the sum of balances never grows; concurrent confirms serialize on the
payer's wallet lock so the available balance is never spent twice.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from services.registry.app.auth import create_agent_token, create_user_token
from services.registry.app.models import CurrencyType, Transaction, TransactionStatus, TransactionType, Wallet, WalletOwnerType

from .conftest import auth


def _wallet(db, agent):
    db.expire_all()
    return db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == agent.id).first()


def _body(src, dst, amount, **extra):
    return {"from_wallet": str(src), "to_wallet": str(dst), "amount": amount, "currency": "credits", "type": "payment", **extra}


def _pay(client, token, src, dst, amount):
    r = client.post("/v1/transactions/create", headers=auth(token), json=_body(src, dst, amount))
    return r


def _owner_tok(db, agent):
    """Settling is a user action: the payer agent's owner confirms."""
    return create_user_token(agent.user_id).access_token


def _sane(w):
    assert w.reserved_credits >= 0
    assert w.balance_credits >= w.reserved_credits, f"reserved exceeds balance: {w.reserved_credits} > {w.balance_credits}"


@pytest.mark.parametrize("amount", [-50, 0])
def test_non_positive_amount_is_refused(payment_client, db, make_agent, amount):
    payer = make_agent("Neg_Payer", balance_credits=100)
    victim = make_agent("Neg_Victim", balance_credits=100)
    tok = create_agent_token(payer.id).access_token
    r = _pay(payment_client, tok, _wallet(db, payer).id, _wallet(db, victim).id, amount)
    assert r.status_code == 422, r.text
    assert db.query(Transaction).count() == 0
    assert _wallet(db, victim).balance_credits == 100, "the payee's balance must never move on a refused transfer"


def test_a_legacy_negative_pending_row_cannot_be_confirmed(payment_client, db, make_agent):
    """A negative PENDING row written before the fix (or by any other path) must
    never settle: the confirm route refuses it AND the DB constraint refuses it."""
    payer = make_agent("Legacy_Payer", balance_credits=100)
    victim = make_agent("Legacy_Victim", balance_credits=100)
    wp, wv = _wallet(db, payer), _wallet(db, victim)
    tx_id = uuid.uuid4()
    # The amount CHECK is NOT VALID, so it binds new rows; simulate a legacy row
    # by inserting it with the constraint dropped for this transaction only.
    db.execute(text("ALTER TABLE transactions DROP CONSTRAINT chk_transaction_amount_nonneg"))
    db.add(Transaction(id=tx_id, from_wallet=wp.id, to_wallet=wv.id, amount=-50, currency=CurrencyType.CREDITS,
                       status=TransactionStatus.PENDING, type=TransactionType.PAYMENT))
    db.flush()
    db.execute(text("ALTER TABLE transactions ADD CONSTRAINT chk_transaction_amount_nonneg CHECK (amount >= 0) NOT VALID"))
    db.commit()
    r = payment_client.post(f"/v1/transactions/{tx_id}/confirm", headers=auth(_owner_tok(db, payer)))
    assert r.status_code == 400, r.text
    assert _wallet(db, victim).balance_credits == 100
    assert _wallet(db, payer).balance_credits == 100
    # and below the route: the DB itself refuses to complete it
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE transactions SET status = 'completed' WHERE id = :i"), {"i": tx_id})
    db.rollback()
    assert _wallet(db, victim).balance_credits == 100


def test_db_refuses_a_negative_transaction_insert(db, make_agent):
    payer = make_agent("Db_Payer", balance_credits=100)
    victim = make_agent("Db_Victim", balance_credits=100)
    db.add(Transaction(id=uuid.uuid4(), from_wallet=_wallet(db, payer).id, to_wallet=_wallet(db, victim).id, amount=-1,
                       currency=CurrencyType.CREDITS, status=TransactionStatus.PENDING, type=TransactionType.PAYMENT))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_escrowed_credits_cannot_be_spent_directly(payment_client, db, make_agent):
    payer = make_agent("Esc_Payer", balance_credits=100)
    payee = make_agent("Esc_Payee")
    wp = _wallet(db, payer)
    wp.reserved_credits = 80  # 80 credits are locked as escrow for another task
    db.commit()
    tok = create_agent_token(payer.id).access_token
    wpe = _wallet(db, payee)
    r = _pay(payment_client, tok, wp.id, wpe.id, 21)
    assert r.status_code == 400, r.text
    assert "available" in r.json()["detail"]
    # the 20 unreserved credits are spendable
    r = _pay(payment_client, tok, wp.id, wpe.id, 20)
    assert r.status_code == 200, r.text
    tx_id = r.json()["transaction_id"]
    assert payment_client.post(f"/v1/transactions/{tx_id}/confirm", headers=auth(_owner_tok(db, payer))).status_code == 200
    wp = _wallet(db, payer)
    assert (wp.balance_credits, wp.reserved_credits) == (80, 80)
    _sane(wp)
    assert _wallet(db, payee).balance_credits == 19, "20 minus the 1-credit platform fee"


def test_confirm_rechecks_available_balance_after_escrow_was_locked(payment_client, db, make_agent):
    """Create passes, then a task reserves the funds, then confirm must refuse:
    the create-time check is advisory, the confirm-time check under the wallet
    lock is the one that moves money."""
    payer = make_agent("Late_Payer", balance_credits=100)
    payee = make_agent("Late_Payee")
    tok = create_agent_token(payer.id).access_token
    r = _pay(payment_client, tok, _wallet(db, payer).id, _wallet(db, payee).id, 60)
    assert r.status_code == 200, r.text
    wp = _wallet(db, payer)
    wp.reserved_credits = 50
    db.commit()
    r = payment_client.post(f"/v1/transactions/{r.json()['transaction_id']}/confirm", headers=auth(_owner_tok(db, payer)))
    assert r.status_code == 400, r.text
    wp = _wallet(db, payer)
    assert (wp.balance_credits, wp.reserved_credits) == (100, 50)
    assert _wallet(db, payee).balance_credits == 0


def test_spending_cap_counts_reservations(payment_client, db, make_agent):
    """Same rule as task_service.create_task_with_escrow: spent + reserved + amount <= cap."""
    payer = make_agent("Cap_Payer", balance_credits=1000, spending_cap=100)
    payee = make_agent("Cap_Payee")
    wp = _wallet(db, payer)
    wp.reserved_credits = 70
    db.commit()
    tok = create_agent_token(payer.id).access_token
    assert _pay(payment_client, tok, wp.id, _wallet(db, payee).id, 31).status_code == 400
    assert _pay(payment_client, tok, wp.id, _wallet(db, payee).id, 30).status_code == 200


@pytest.mark.parametrize("field,value", [("currency", "gold"), ("type", "refund"), ("type", "deposit"), ("amount", 1.5)])
def test_only_plain_payments_in_known_currencies(payment_client, db, make_agent, field, value):
    """Refund/deposit/withdraw/referral rows are written by the system, never by a caller."""
    payer = make_agent("Typ_Payer", balance_credits=100)
    payee = make_agent("Typ_Payee")
    tok = create_agent_token(payer.id).access_token
    body = _body(_wallet(db, payer).id, _wallet(db, payee).id, 5)
    body[field] = value
    r = payment_client.post("/v1/transactions/create", headers=auth(tok), json=body)
    assert r.status_code == 422, r.text


def test_approval_request_amount_must_be_positive(payment_client, db, make_user, make_agent):
    from services.registry.app.auth import create_user_token

    owner = make_user("appr-neg@money.test")
    agent = make_agent("Appr_Neg", user=owner)
    tok = create_user_token(owner.id).access_token
    r = payment_client.post("/v1/approval_requests/", headers=auth(tok),
                            json={"agent_id": str(agent.id), "amount": -5, "description": "neg"})
    assert r.status_code == 422, r.text
    # a positive request is accepted (it used to 500 on CurrencyType("CREDITS"))
    r = payment_client.post("/v1/approval_requests/", headers=auth(tok),
                            json={"agent_id": str(agent.id), "amount": 5, "description": "ok"})
    assert r.status_code == 200, r.text


def _race(n, fn):
    barrier = threading.Barrier(n)
    results = [None] * n

    def _run(i):
        barrier.wait(timeout=10)
        try:
            results[i] = fn(i)
        except BaseException as exc:  # noqa: BLE001
            results[i] = exc

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(r is not None for r in results), "a racing thread never finished"
    return results


def test_concurrent_confirms_never_overspend(payment_client, db, make_agent):
    """Four pending 40-credit transfers from a wallet holding 100 (30 reserved):
    confirmed at once, at most one may settle (70 available)."""
    from services.payment.app.main import app as payment_app

    payer = make_agent("Race_Payer", balance_credits=100)
    payee = make_agent("Race_Payee")
    tok = create_agent_token(payer.id).access_token
    wp, wpe = _wallet(db, payer), _wallet(db, payee)
    ids = []
    for _ in range(4):
        r = _pay(payment_client, tok, wp.id, wpe.id, 40)
        assert r.status_code == 200, r.text
        ids.append(r.json()["transaction_id"])
    wp = _wallet(db, payer)
    wp.reserved_credits = 30
    db.commit()

    owner = _owner_tok(db, payer)
    clients = [TestClient(payment_app) for _ in ids]
    codes = _race(len(ids), lambda i: clients[i].post(f"/v1/transactions/{ids[i]}/confirm", headers=auth(owner)).status_code)
    assert sorted(codes) == [200, 400, 400, 400], codes
    wp, wpe = _wallet(db, payer), _wallet(db, payee)
    assert (wp.balance_credits, wp.reserved_credits, wpe.balance_credits) == (60, 30, 39)  # 40 - 1 fee
    _sane(wp)


def test_concurrent_confirm_of_the_same_transaction_moves_money_once(payment_client, db, make_agent):
    from services.payment.app.main import app as payment_app

    payer = make_agent("Dup_Payer", balance_credits=100)
    payee = make_agent("Dup_Payee")
    tok = create_agent_token(payer.id).access_token
    r = _pay(payment_client, tok, _wallet(db, payer).id, _wallet(db, payee).id, 10)
    tx_id = r.json()["transaction_id"]
    owner = _owner_tok(db, payer)
    clients = [TestClient(payment_app) for _ in range(4)]
    codes = _race(4, lambda i: clients[i].post(f"/v1/transactions/{tx_id}/confirm", headers=auth(owner)).status_code)
    assert sorted(codes) == [200, 400, 400, 400], codes
    assert _wallet(db, payer).balance_credits == 90
    assert _wallet(db, payee).balance_credits == 9  # 10 - 1 fee


def test_opposite_direction_confirms_do_not_deadlock(payment_client, db, make_agent):
    """A->B and B->A confirmed at once: wallets are locked in id order, so neither
    request dies on a deadlock and both settle."""
    from services.payment.app.main import app as payment_app

    a = make_agent("Dl_A", balance_credits=100)
    b = make_agent("Dl_B", balance_credits=100)
    ta, tb = create_agent_token(a.id).access_token, create_agent_token(b.id).access_token
    wa, wb = _wallet(db, a), _wallet(db, b)
    pairs = []
    for _ in range(3):
        pairs.append((_owner_tok(db, a), _pay(payment_client, ta, wa.id, wb.id, 5).json()["transaction_id"]))
        pairs.append((_owner_tok(db, b), _pay(payment_client, tb, wb.id, wa.id, 5).json()["transaction_id"]))
    clients = [TestClient(payment_app) for _ in pairs]
    codes = _race(len(pairs), lambda i: clients[i].post(f"/v1/transactions/{pairs[i][1]}/confirm", headers=auth(pairs[i][0])).status_code)
    assert codes == [200] * len(pairs), codes
    wa, wb = _wallet(db, a), _wallet(db, b)
    assert wa.balance_credits + wb.balance_credits == 200 - 6, "six transfers, one fee credit each"
