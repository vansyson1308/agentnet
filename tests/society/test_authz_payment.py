"""Payment-service authorization: wallet ownership on every money route and
the internal worker endpoint."""

from __future__ import annotations

import uuid

from services.payment.app.config import INTERNAL_WORKER_TOKEN
from services.registry.app.auth import create_agent_token, create_user_token
from services.registry.app.models import CurrencyType, Transaction, TransactionStatus, TransactionType, Wallet, WalletOwnerType

from .conftest import auth


def _user_wallet(db, user, credits=100):
    w = Wallet(id=uuid.uuid4(), owner_type=WalletOwnerType.USER, owner_id=user.id, balance_credits=credits, reserved_credits=0, spending_cap=1000, daily_spent=0)
    db.add(w)
    db.commit()
    return w


def _agent_wallet(db, agent):
    return db.query(Wallet).filter(Wallet.owner_type == WalletOwnerType.AGENT, Wallet.owner_id == agent.id).first()


def test_transaction_create_requires_source_wallet_ownership(payment_client, db, make_user, make_agent):
    ua = make_user("pay-a@authz.test")
    ub = make_user("pay-b@authz.test")
    ua_tok = create_user_token(ua.id).access_token
    b1 = make_agent("Pay_B1", user=ub, balance_credits=100)
    b1_tok = create_agent_token(b1.id).access_token
    wa = _user_wallet(db, ua)
    wb1 = _agent_wallet(db, b1)
    body = lambda src, dst: {"from_wallet": str(src), "to_wallet": str(dst), "amount": 5, "currency": "credits", "type": "payment"}  # noqa: E731
    # user A spending from agent B1's wallet — the historical bypass
    assert payment_client.post("/v1/transactions/create", headers=auth(ua_tok), json=body(wb1.id, wa.id)).status_code == 403
    # agent B1 spending from user A's wallet — the mirror bypass
    assert payment_client.post("/v1/transactions/create", headers=auth(b1_tok), json=body(wa.id, wb1.id)).status_code == 403
    # owners may spend their own
    assert payment_client.post("/v1/transactions/create", headers=auth(ua_tok), json=body(wa.id, wb1.id)).status_code == 200
    assert payment_client.post("/v1/transactions/create", headers=auth(b1_tok), json=body(wb1.id, wa.id)).status_code == 200
    assert payment_client.post("/v1/transactions/create", json=body(wa.id, wb1.id)).status_code == 401


def test_confirm_requires_ownership_of_the_paying_wallet(payment_client, db, make_user, make_agent):
    ua = make_user("conf-a@authz.test")
    ub = make_user("conf-b@authz.test")
    ua_tok = create_user_token(ua.id).access_token
    ub_tok = create_user_token(ub.id).access_token
    b1 = make_agent("Conf_B1", user=ub, balance_credits=100)
    wa = _user_wallet(db, ua)
    wb1 = _agent_wallet(db, b1)
    tx = Transaction(id=uuid.uuid4(), from_wallet=wb1.id, to_wallet=wa.id, amount=5, currency=CurrencyType.CREDITS, status=TransactionStatus.PENDING, type=TransactionType.PAYMENT)
    db.add(tx)
    db.commit()
    assert payment_client.post(f"/v1/transactions/{tx.id}/confirm", headers=auth(ua_tok)).status_code == 403, "the payee cannot settle the payer's wallet"
    assert payment_client.post(f"/v1/transactions/{tx.id}/confirm").status_code == 401
    assert payment_client.post(f"/v1/transactions/{tx.id}/confirm", headers=auth(ub_tok)).status_code == 200


def test_dev_funding_is_owner_only(payment_client, db, make_user, make_agent, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    ua = make_user("fund-a@authz.test")
    ub = make_user("fund-b@authz.test")
    ua_tok = create_user_token(ua.id).access_token
    b1 = make_agent("Fund_B1", user=ub)
    wb1 = _agent_wallet(db, b1)
    assert payment_client.post(f"/v1/wallets/{wb1.id}/fund", headers=auth(ua_tok), json={"amount": 50}).status_code == 403
    ub_tok = create_user_token(ub.id).access_token
    assert payment_client.post(f"/v1/wallets/{wb1.id}/fund", headers=auth(ub_tok), json={"amount": 50}).status_code == 200


def test_worker_expire_requires_internal_token(payment_client):
    assert payment_client.post("/v1/approval_requests/worker/expire").status_code == 404
    assert payment_client.post("/v1/approval_requests/worker/expire", headers={"X-Internal-Token": "wrong"}).status_code == 404
    r = payment_client.post("/v1/approval_requests/worker/expire", headers={"X-Internal-Token": INTERNAL_WORKER_TOKEN})
    assert r.status_code == 200, r.text
