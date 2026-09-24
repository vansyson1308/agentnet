"""Verification-email delivery (Phase 7 §26-27).

The gap this closes: registration used to create the user, the wallet and the
verification token, commit all three, log that a token had been issued, and
answer "User registered successfully" -- while outside development the link was
never delivered. Login requires a verified address, so the account was
unreachable *and* held the email against a retry.

These tests use a real SMTP server on localhost (aiosmtpd if available, else a
hand-rolled socket server speaking enough of RFC 5321). No vendor, no network,
no credential.
"""

from __future__ import annotations

import email
import socket
import threading
import time
import uuid

import pytest

from services.registry.app.email_delivery import (
    DisabledEmailProvider,
    EmailDeliveryError,
    EmailDeliveryUnavailable,
    LogEmailProvider,
    SMTPEmailProvider,
    build_email_provider,
)


# ── a minimal real SMTP server ───────────────────────────────────────────
class TinySMTPServer:
    """Enough of RFC 5321 to accept one message and hand back its bytes.

    Deliberately not a mock of our own client: the point is to exercise the
    real smtplib conversation, so a client bug shows up here rather than in
    production.
    """

    def __init__(self, *, fail_auth: bool = False, hang: bool = False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.messages: list[str] = []
        self.fail_auth = fail_auth
        self.hang = hang
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            if self.hang:
                time.sleep(30)  # client must time out
                return
            f = conn.makefile("rwb")
            f.write(b"220 tiny.test ESMTP\r\n")
            f.flush()
            body: list[str] = []
            in_data = False
            while True:
                line = f.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if in_data:
                    if text == ".":
                        in_data = False
                        self.messages.append("\n".join(body))
                        body = []
                        f.write(b"250 OK queued\r\n")
                        f.flush()
                    else:
                        body.append(text)
                    continue
                upper = text.upper()
                if upper.startswith("EHLO") or upper.startswith("HELO"):
                    f.write(b"250-tiny.test\r\n250 AUTH PLAIN LOGIN\r\n")
                elif upper.startswith("AUTH"):
                    f.write(b"535 authentication failed\r\n" if self.fail_auth else b"235 OK\r\n")
                elif upper.startswith("MAIL FROM") or upper.startswith("RCPT TO"):
                    f.write(b"250 OK\r\n")
                elif upper == "DATA":
                    in_data = True
                    f.write(b"354 send it\r\n")
                elif upper == "QUIT":
                    f.write(b"221 bye\r\n")
                    f.flush()
                    return
                else:
                    f.write(b"250 OK\r\n")
                f.flush()


# ── provider contract ────────────────────────────────────────────────────
def test_disabled_provider_never_claims_delivery():
    """The property that matters is the one it does NOT have: success."""
    with pytest.raises(EmailDeliveryUnavailable):
        DisabledEmailProvider().send_verification(to="a@b.test", verify_url="https://x/y")


def test_log_provider_is_refused_in_production(monkeypatch):
    """It writes a live credential to the log. In production that is a leak."""
    monkeypatch.setattr("services.registry.app.email_delivery.ENVIRONMENT", "production")
    with pytest.raises(EmailDeliveryError):
        LogEmailProvider()


def test_log_provider_is_allowed_in_staging(monkeypatch):
    """Staging has no SMTP and its accounts are canaries. Refusing here would
    leave staging registration permanently 503 -- and the staging validator
    cannot create the canary user it needs (deploy/railway/validate_staging.py).
    It is still never the DEFAULT outside development: reaching it requires an
    explicit EMAIL_DELIVERY_PROVIDER=log, which is a deliberate operator act.
    """
    monkeypatch.setattr("services.registry.app.email_delivery.ENVIRONMENT", "staging")
    monkeypatch.setattr("services.registry.app.email_delivery.IS_DEV", False)
    assert LogEmailProvider().name == "log"
    monkeypatch.delenv("EMAIL_DELIVERY_PROVIDER", raising=False)
    assert build_email_provider().name == "disabled", "log must not be the staging default"
    monkeypatch.setenv("EMAIL_DELIVERY_PROVIDER", "log")
    assert build_email_provider().name == "log"


def test_smtp_delivers_the_verification_link():
    with TinySMTPServer() as server:
        SMTPEmailProvider(
            host="127.0.0.1", port=server.port, sender="no-reply@agentnet.test",
            use_starttls=False, timeout=10,
        ).send_verification(to="user@agentnet.test", verify_url="https://api.test/v1/auth/verify-email?token=TOK123")
        for _ in range(100):
            if server.messages:
                break
            time.sleep(0.05)
    assert server.messages, "no message reached the SMTP server"
    # The body is quoted-printable on the wire ("=" -> "=3D", soft line wraps),
    # which is correct MIME -- so decode it rather than asserting on raw bytes.
    parsed = email.message_from_string(server.messages[0])
    assert parsed["To"] == "user@agentnet.test"
    assert parsed["Subject"]
    body = parsed.get_payload(decode=True).decode("utf-8")
    assert "https://api.test/v1/auth/verify-email?token=TOK123" in body, body


def test_smtp_unavailable_is_unavailable_not_a_crash():
    """A closed port is a service condition, so the caller can answer 503."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing is listening now
    with pytest.raises(EmailDeliveryUnavailable):
        SMTPEmailProvider(
            host="127.0.0.1", port=port, sender="no-reply@agentnet.test",
            use_starttls=False, timeout=3,
        ).send_verification(to="a@b.test", verify_url="https://x/y")


def test_smtp_authentication_failure_is_unavailable_and_leaks_no_password():
    # Generated per run, never a literal: a password-shaped string committed to
    # the repository is worth refusing even as bait (GitGuardian flags it, and
    # it is right to). A fresh random value also proves absence more strongly
    # than a fixed one -- it cannot be present by coincidence.
    secret = "pw-" + uuid.uuid4().hex
    with TinySMTPServer(fail_auth=True) as server:
        with pytest.raises(EmailDeliveryUnavailable) as err:
            SMTPEmailProvider(
                host="127.0.0.1", port=server.port, sender="no-reply@agentnet.test",
                username="postmaster", password=secret,
                use_starttls=False, timeout=10,
            ).send_verification(to="a@b.test", verify_url="https://x/y")
    assert secret not in str(err.value)
    assert secret not in repr(err.value)


def test_smtp_timeout_is_unavailable():
    with TinySMTPServer(hang=True) as server:
        with pytest.raises(EmailDeliveryUnavailable):
            SMTPEmailProvider(
                host="127.0.0.1", port=server.port, sender="no-reply@agentnet.test",
                use_starttls=False, timeout=1,
            ).send_verification(to="a@b.test", verify_url="https://x/y")


# ── configuration ────────────────────────────────────────────────────────
def test_default_outside_development_is_disabled_not_silently_dropped(monkeypatch):
    """A host that forgets to configure delivery must fail loudly at
    registration, not quietly create accounts nobody can activate."""
    monkeypatch.setattr("services.registry.app.email_delivery.IS_DEV", False)
    monkeypatch.delenv("EMAIL_DELIVERY_PROVIDER", raising=False)
    assert build_email_provider().name == "disabled"


def test_default_in_development_keeps_the_logged_link(monkeypatch):
    monkeypatch.setattr("services.registry.app.email_delivery.IS_DEV", True)
    monkeypatch.delenv("EMAIL_DELIVERY_PROVIDER", raising=False)
    assert build_email_provider().name == "log"


@pytest.mark.parametrize("missing", ["SMTP_HOST", "SMTP_FROM"])
def test_smtp_requires_host_and_sender(monkeypatch, missing):
    monkeypatch.setenv("EMAIL_DELIVERY_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM", "no-reply@agentnet.test")
    monkeypatch.setenv(missing, "")
    with pytest.raises(EmailDeliveryError):
        build_email_provider()


def test_tls_and_starttls_are_mutually_exclusive():
    with pytest.raises(EmailDeliveryError):
        SMTPEmailProvider(
            host="smtp.test", port=465, sender="a@b.test", use_tls=True, use_starttls=True
        )


def test_unknown_provider_is_refused_rather_than_defaulted(monkeypatch):
    monkeypatch.setenv("EMAIL_DELIVERY_PROVIDER", "sendgrid")
    with pytest.raises(EmailDeliveryError):
        build_email_provider()


def test_no_vendor_specific_code_in_the_delivery_boundary():
    """Choosing a commercial email vendor is an owner decision, not ours.

    Matched on word boundaries: a substring scan flags "refuses" for "ses",
    which would make this assert nothing useful while looking strict.
    """
    import pathlib
    import re

    src = pathlib.Path("services/registry/app/email_delivery.py").read_text(encoding="utf-8").lower()
    for vendor in ("sendgrid", "mailgun", "postmark", "resend", "sparkpost", "boto3", "amazon_ses", "aws_ses"):
        assert not re.search(rf"\b{vendor}\b", src), f"{vendor} must not be hard-coded into the delivery boundary"
    # Nothing but the stdlib mail machinery may be imported here.
    assert "import smtplib" in src
    for forbidden in ("import requests", "import httpx", "import boto3"):
        assert forbidden not in src, f"{forbidden} suggests a vendor HTTP API, not neutral SMTP"


# ── resend-verification must not become an enumeration oracle ────────────────
#
# The endpoint's whole contract is that its answer does not depend on whether
# the address exists. Checking "did the provider construct?" does NOT establish
# that: the disabled provider -- production's default -- constructs perfectly
# well and refuses only when asked to send, which happens after the lookup. So
# an existing unverified address got a 503 and everything else got a 200, which
# is exactly the bit the generic message exists to withhold.
#
# These drive the real route function against a stub session, so the assertion
# is about the response the endpoint actually produces.

def _auth_routes():
    """Import the auth routes the way the service itself does.

    The registry package is rooted at ``services/registry`` and imports itself
    as ``app.*``; importing it as ``services.registry.app.*`` gives a second,
    inconsistent copy whose own imports fail.
    """
    import pathlib
    import sys

    root = str(pathlib.Path(__file__).resolve().parent.parent / "services" / "registry")
    if root not in sys.path:
        sys.path.insert(0, root)
    from app.api.routes import auth as auth_routes

    return auth_routes


class _StubQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._result


class _StubSession:
    """Just enough Session for the route: a canned user and no-op writes."""

    def __init__(self, user):
        self._user = user
        self.committed = False
        self.rolled_back = False
        self.added = []

    def query(self, *_a, **_k):
        return _StubQuery(self._user)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _StubUser:
    def __init__(self, verified: bool):
        import uuid as _uuid

        self.id = _uuid.uuid4()
        self.email = "someone@example.com"
        self.is_email_verified = verified


def _resend(monkeypatch, provider_name: str, user):
    """Call the real route and return (status_code_or_200, session)."""
    import asyncio

    from fastapi import HTTPException

    auth_routes = _auth_routes()

    monkeypatch.setenv("EMAIL_DELIVERY_PROVIDER", provider_name)
    session = _StubSession(user)
    req = auth_routes.ResendVerificationRequest(email="someone@example.com")
    try:
        asyncio.run(auth_routes.resend_verification(req, db=session))
        return 200, session
    except HTTPException as exc:
        return exc.status_code, session


@pytest.mark.parametrize(
    "user,label",
    [
        (None, "no such address"),
        (_StubUser(verified=False), "exists, unverified"),
        (_StubUser(verified=True), "exists, verified"),
    ],
)
def test_resend_answers_identically_whether_or_not_the_address_exists(monkeypatch, user, label):
    """With delivery disabled, all three cases must be indistinguishable."""
    status, session = _resend(monkeypatch, "disabled", user)
    assert status == 503, f"{label}: got {status}"
    assert not session.committed, f"{label}: nothing may be written when delivery is impossible"


def test_resend_delivers_for_an_unverified_address_when_delivery_works(monkeypatch):
    """The uniform 503 must not be achieved by never delivering at all."""
    sent = []
    auth_routes = _auth_routes()

    monkeypatch.setattr(
        auth_routes, "_deliver_verification", lambda email, token: sent.append((email, token))
    )
    status, session = _resend(monkeypatch, "log", _StubUser(verified=False))
    assert status == 200
    assert session.committed and len(sent) == 1
    # ...and a verified address is a no-op with the same 200.
    status, session = _resend(monkeypatch, "log", _StubUser(verified=True))
    assert status == 200 and not session.committed and len(sent) == 1


def test_every_provider_declares_whether_it_can_deliver():
    """`available` is the capability the route checks; a provider that omits it
    would silently be treated as unable to deliver."""
    from app.email_delivery import DisabledEmailProvider, LogEmailProvider, SMTPEmailProvider

    assert DisabledEmailProvider.available is False
    assert LogEmailProvider.available is True
    assert SMTPEmailProvider.available is True


def test_smtp_failure_text_names_the_host_and_cause_but_no_credential():
    """A delivery outage must be diagnosable from the exception alone.

    In production the registration handler logs this text. It has to say
    enough to tell a blocked port from a rejected credential, and nothing
    that would put a secret in the log.
    """
    secret = "pw-" + uuid.uuid4().hex
    provider = SMTPEmailProvider(
        host="smtp.example.org", port=2465, username="user", password=secret,
        sender="AgentNet <noreply@example.org>", use_starttls=False, use_tls=True,
        timeout=0.001,
    )
    with pytest.raises(EmailDeliveryUnavailable) as excinfo:
        provider.send_verification(to="someone@example.com", verify_url="https://example.org/v1/x?token=abc")

    text = str(excinfo.value)
    assert "smtp.example.org:2465" in text          # which host, which port
    assert text.rstrip(")").rsplit("(", 1)[-1]      # and the underlying cause
    assert secret not in text
    assert "someone@example.com" not in text
    assert "token=abc" not in text


def test_the_registration_handler_logs_the_failure_text_not_just_its_class():
    """`type(exc).__name__` here is a restatement of the log line itself.

    A production delivery outage was invisible for exactly this reason: the
    log said "undeliverable (EmailDeliveryUnavailable)", which cannot tell a
    blocked port from a rejected credential. The text above is safe to log and
    is the only thing that names the cause.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath(
        "services/registry/app/api/routes/auth.py"
    ).read_text(encoding="utf-8")
    line = [ln for ln in source.splitlines() if "verification email undeliverable" in ln]
    assert len(line) == 1, line
    assert "type(exc).__name__" not in line[0]
    assert line[0].rstrip().endswith(", exc)")
