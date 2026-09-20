"""Verification-email delivery — a provider-neutral boundary.

Before this module, registration generated a verification token and then only
*logged* that it had done so. Outside development the token was never delivered
and was (correctly) never logged, so the sequence was:

    register -> token generated -> token NOT delivered -> user can never log in

and the API still answered "User registered successfully". The account existed,
occupied the unique email, and was unusable. That is a real public-launch gap,
not a configuration detail, so the fix is a delivery contract the API is held
to — not a vendor integration.

No vendor-specific code and no commercial provider is chosen here: that is an
owner decision. `smtp` speaks to whatever host the operator configures.

Providers
---------
``disabled``  the honest default outside development. Refuses to claim delivery,
              so registration fails atomically instead of creating an account
              nobody can reach.
``log``       development only. Logs the verification link, which is how local
              work and the test suite have always verified an address. Refuses
              to be constructed outside development, because logging a live
              credential is exactly what must never happen in production.
``smtp``      real delivery through operator-configured SMTP.

The token is a credential. It appears in a log line only under ``log``, which
cannot exist outside development.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional, Protocol

from .config import IS_DEV

logger = logging.getLogger(__name__)

#: How long a verification link stays valid. Mirrored by the caller when it
#: writes ``EmailVerificationToken.expires_at``.
VERIFICATION_TTL_HOURS = 24


class EmailDeliveryError(Exception):
    """Delivery failed. Never carries the token or the SMTP password."""


class EmailDeliveryUnavailable(EmailDeliveryError):
    """Delivery is not possible right now: unconfigured, refused, or timed out.

    Distinct from a programming error: the caller turns this into a 503 and
    rolls back, so a retry later can succeed.
    """


class EmailDeliveryProvider(Protocol):
    name: str

    #: Whether this provider can deliver at all, answerable WITHOUT a
    #: recipient. A caller that must not reveal whether an address exists has
    #: to decide "is delivery possible" before it looks the address up --
    #: construction alone cannot answer that, because a provider that is
    #: configured to deliver nothing constructs perfectly well.
    available: bool

    def send_verification(self, *, to: str, verify_url: str) -> None:  # pragma: no cover - protocol
        ...


class DisabledEmailProvider:
    """Delivers nothing and says so.

    The important property is the one it does NOT have: it never returns
    successfully. A provider that silently swallowed the message would let the
    API keep reporting a success that did not happen.
    """

    name = "disabled"
    available = False

    def send_verification(self, *, to: str, verify_url: str) -> None:
        raise EmailDeliveryUnavailable(
            "email delivery is disabled (EMAIL_DELIVERY_PROVIDER=disabled); "
            "no verification message can be sent"
        )


class LogEmailProvider:
    """Development only: the link goes to the log, as it always has."""

    name = "log"
    available = True

    def __init__(self) -> None:
        if not IS_DEV:
            # Not a warning, a refusal. This provider writes a live credential
            # to the log; outside development that is a leak, not a fallback.
            raise EmailDeliveryError(
                "EMAIL_DELIVERY_PROVIDER=log is refused outside development: "
                "it writes the verification link to the log"
            )

    def send_verification(self, *, to: str, verify_url: str) -> None:
        logger.info("DEV ONLY verification link for %s: %s", to, verify_url)


class SMTPEmailProvider:
    """Deliver through an operator-configured SMTP host.

    Nothing here is specific to a vendor. ``starttls`` upgrades a plaintext
    connection; ``tls`` connects over TLS from the start (implicit TLS, usually
    port 465). Credentials come from the environment and are never logged.
    """

    # Static configuration is validated in __init__ (host and sender are
    # required, TLS modes are exclusive), so a constructed SMTP provider is
    # always statically capable of delivering. Whether the host is reachable
    # RIGHT NOW is a different question, and deliberately not asked here: see
    # the note on the public verification endpoint in api/routes/auth.py.
    name = "smtp"
    available = True

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        sender: str = "",
        use_starttls: bool = True,
        use_tls: bool = False,
        timeout: float = 15.0,
    ) -> None:
        if not host:
            raise EmailDeliveryError("SMTP_HOST is required when EMAIL_DELIVERY_PROVIDER=smtp")
        if not sender:
            raise EmailDeliveryError("SMTP_FROM is required when EMAIL_DELIVERY_PROVIDER=smtp")
        if use_tls and use_starttls:
            raise EmailDeliveryError("SMTP_TLS and SMTP_STARTTLS are mutually exclusive")
        self.host, self.port, self.sender = host, port, sender
        self.username, self.password = username, password
        self.use_starttls, self.use_tls, self.timeout = use_starttls, use_tls, timeout

    def _build(self, to: str, verify_url: str) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = "Verify your AgentNet email address"
        msg["From"] = self.sender
        msg["To"] = to
        msg.set_content(
            "Welcome to AgentNet.\n\n"
            "Confirm this address to activate your account:\n\n"
            f"{verify_url}\n\n"
            f"The link expires in {VERIFICATION_TTL_HOURS} hours. "
            "If you did not create an AgentNet account, ignore this message.\n"
        )
        return msg

    def send_verification(self, *, to: str, verify_url: str) -> None:
        msg = self._build(to, verify_url)
        try:
            if self.use_tls:
                client = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout, context=ssl.create_default_context()
                )
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with client:
                if self.use_starttls:
                    client.starttls(context=ssl.create_default_context())
                if self.username:
                    client.login(self.username, self.password)
                client.send_message(msg)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            # The exception type and host are useful; the body, the token and
            # the password are not, and must not reach a log or an API response.
            raise EmailDeliveryUnavailable(
                f"SMTP delivery failed via {self.host}:{self.port} ({type(exc).__name__})"
            ) from None


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_email_provider(name: Optional[str] = None) -> EmailDeliveryProvider:
    """Construct the configured provider.

    The default is ``log`` in development (what local work and the test suite
    have always relied on) and ``disabled`` everywhere else — so a host that
    forgets to configure delivery fails loudly at registration rather than
    quietly creating unusable accounts.
    """
    chosen = (name if name is not None else os.getenv("EMAIL_DELIVERY_PROVIDER", "")).strip().lower()
    if not chosen:
        chosen = "log" if IS_DEV else "disabled"
    if chosen == "disabled":
        return DisabledEmailProvider()
    if chosen == "log":
        return LogEmailProvider()
    if chosen == "smtp":
        return SMTPEmailProvider(
            host=os.getenv("SMTP_HOST", "").strip(),
            port=int(os.getenv("SMTP_PORT", "587") or "587"),
            username=os.getenv("SMTP_USERNAME", ""),
            password=os.getenv("SMTP_PASSWORD", ""),
            sender=os.getenv("SMTP_FROM", "").strip(),
            use_starttls=_flag("SMTP_STARTTLS", True),
            use_tls=_flag("SMTP_TLS", False),
            timeout=float(os.getenv("SMTP_TIMEOUT_SECONDS", "15") or "15"),
        )
    raise EmailDeliveryError(
        f"unknown EMAIL_DELIVERY_PROVIDER={chosen!r} (expected: disabled | log | smtp)"
    )


__all__ = [
    "EmailDeliveryError",
    "EmailDeliveryUnavailable",
    "EmailDeliveryProvider",
    "DisabledEmailProvider",
    "LogEmailProvider",
    "SMTPEmailProvider",
    "build_email_provider",
    "VERIFICATION_TTL_HOURS",
]
