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


def test_log_provider_is_refused_outside_development(monkeypatch):
    """It writes a live credential to the log. Outside dev that is a leak."""
    monkeypatch.setattr("services.registry.app.email_delivery.IS_DEV", False)
    with pytest.raises(EmailDeliveryError):
        LogEmailProvider()


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
    with TinySMTPServer(fail_auth=True) as server:
        with pytest.raises(EmailDeliveryUnavailable) as err:
            SMTPEmailProvider(
                host="127.0.0.1", port=server.port, sender="no-reply@agentnet.test",
                username="postmaster", password="hunter2-should-never-appear",
                use_starttls=False, timeout=10,
            ).send_verification(to="a@b.test", verify_url="https://x/y")
    assert "hunter2-should-never-appear" not in str(err.value)


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
