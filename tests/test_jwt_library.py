"""PyJWT replaced python-jose (CVE-2026-85394, ADR-0003): the same HS256 tokens verify, and
every service still refuses alg=none, another algorithm, an expired token and a wrong key."""

import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi import HTTPException

from services.payment.app import auth as payment_auth
from services.registry.app import auth as registry_auth
from services.registry.app.config import JWT_SECRET_KEY
from services.simulation.app import auth as simulation_auth

VERIFIERS = [payment_auth.verify_token, simulation_auth.verify_token, lambda t: registry_auth.verify_token(t)]


def _b64(obj) -> str:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _token(claims, *, alg="HS256", key=JWT_SECRET_KEY) -> str:
    """A compact JWS built without any JWT library (the format python-jose issued)."""
    head = f"{_b64({'alg': alg, 'typ': 'JWT'})}.{_b64(claims)}"
    digest = {"HS256": hashlib.sha256, "HS512": hashlib.sha512}.get(alg)
    sig = _b64(hmac.new(key.encode(), head.encode(), digest).digest()) if digest else ""
    return f"{head}.{sig}"


@pytest.mark.parametrize("verify", VERIFIERS)
def test_a_library_free_hs256_token_still_verifies(verify):
    uid = uuid.uuid4()
    data = verify(_token({"sub": str(uid), "type": "user", "exp": int(time.time()) + 300}))
    assert data.user_id == uid


@pytest.mark.parametrize("verify", VERIFIERS)
@pytest.mark.parametrize("token", [
    _token({"sub": str(uuid.uuid4()), "type": "user", "exp": int(time.time()) + 300}, alg="none"),
    _token({"sub": str(uuid.uuid4()), "type": "user", "exp": int(time.time()) + 300}, alg="HS512"),
    _token({"sub": str(uuid.uuid4()), "type": "user", "exp": int(time.time()) - 60}),
    _token({"sub": str(uuid.uuid4()), "type": "user", "exp": int(time.time()) + 300}, key="another-key-" + "x" * 40),
    "not-a-jwt",
], ids=["alg-none", "hs512", "expired", "wrong-key", "garbage"])
def test_forged_or_expired_tokens_are_refused_with_401(verify, token):
    with pytest.raises(HTTPException) as exc:
        verify(token)
    assert exc.value.status_code == 401


def test_issued_tokens_round_trip_across_services():
    uid = uuid.uuid4()
    tok = registry_auth.create_agent_token(uid).access_token
    assert payment_auth.verify_token(tok).agent_id == uid == simulation_auth.verify_token(tok).agent_id
