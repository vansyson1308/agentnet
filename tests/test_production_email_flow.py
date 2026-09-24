"""The production email/account-flow validator must be safe to point at production.

These tests do not run the validator against anything. They pin the properties
that make its OUTPUT trustworthy and its EXECUTION harmless: it must not print
the credentials it handles, it must not mutate production, and the assertions
that matter must actually be made. A validator that quietly stopped checking
single-use tokens would still print PASS.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "production" / "validate_email_flow.py"
SOURCE = SCRIPT.read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("validate_email_flow", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_token_is_only_ever_interpolated_into_the_verification_url():
    """Every use of the raw token must be the endpoint call itself.

    The token activates the account. Printing it -- into a log line, a detail
    string or a recorded field -- would put a live credential somewhere it can
    be read later, which is exactly what the `log` email provider is refused in
    production for.
    """
    offenders = [
        line.strip()
        for line in SOURCE.splitlines()
        if "{token}" in line and "verify-email?token=" not in line
    ]
    assert offenders == [], f"raw token interpolated outside the verify URL: {offenders}"
    assert "fingerprint(token)" in SOURCE, "the token must be recorded by fingerprint, not value"
    assert not re.search(r"record\(\s*[\"'][^\"']*token[\"']\s*,\s*token\b", SOURCE)


def test_password_and_jwt_are_never_printed():
    assert not re.search(r"\{password\}", SOURCE), "the canary password must never be printed"
    assert not re.search(r"\{jwt\}", SOURCE), "the access token must never be printed"


def test_the_fingerprint_identifies_without_revealing():
    module = _load()
    token = "a" * 43
    fp = module.fingerprint(token)
    assert len(fp) == 16 and re.fullmatch(r"[0-9a-f]{16}", fp)
    assert token not in fp
    assert module.fingerprint(token) == fp, "must be deterministic, or comparison is meaningless"
    assert module.fingerprint("b" * 43) != fp


def test_the_canary_password_is_generated_and_meets_the_registry_policy():
    module = _load()
    first, second = module.canary_password(), module.canary_password()
    assert first != second, "a fixed password would be a credential committed to git"
    for pw in (first, second):
        assert len(pw) >= 12
        assert any(c.isupper() for c in pw) and any(c.islower() for c in pw)
        assert any(c.isdigit() for c in pw)


def test_it_mutates_nothing_it_was_not_asked_to():
    """The only writes are the account the registration API itself creates."""
    for verb in ('"DELETE"', '"PATCH"', '"PUT"'):
        assert f"_http({verb}" not in SOURCE, f"{verb} has no place in a production validator"
    posts = re.findall(r'_http\(\s*"POST",\s*f"\{(?:registry|payment)\}([^"]+)"', SOURCE)
    assert sorted(set(posts)) == ["/v1/auth/user/login", "/v1/auth/user/register"], posts


def test_the_database_is_only_read():
    sql = SOURCE[SOURCE.index("cur.execute("):SOURCE.index("row = cur.fetchone()")]
    for write in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE"):
        assert write not in sql.upper(), f"{write} in a validator query"
    assert "SELECT" in sql.upper()


def test_the_assertions_that_matter_are_actually_made():
    module = _load()
    # Replay must be expected to FAIL, otherwise single-use is not being tested.
    replay = SOURCE[SOURCE.index('"E05"'):SOURCE.index('"E06"')]
    assert "status == 400" in replay
    # Login before verification must be expected to be refused.
    before = SOURCE[SOURCE.index('"E02"'):SOURCE.index('"E03"')]
    assert "status == 403" in before
    # And the flow must actually be wired into main().
    main_src = SOURCE[SOURCE.index("def main("):]
    assert "check_email_flow(" in main_src
    assert callable(module.check_email_flow)


def test_registration_probe_outlasts_the_registrys_smtp_path():
    """A validator must outlast what it measures.

    The first live run reported `E01 -> 0` for what was actually an SMTP
    failure: registration blocks on delivery, that path took ~29s, and the
    probe gave up at its 20s default. The status code is the evidence, so the
    probe has to still be there when it arrives.
    """
    module = _load()
    assert module.REGISTRATION_TIMEOUT_SECONDS >= 60
    register = SOURCE[SOURCE.index('"POST", f"{registry}/v1/auth/user/register"'):]
    register = register[:register.index("report.check")]
    assert "timeout=REGISTRATION_TIMEOUT_SECONDS" in register


def test_every_run_registers_a_fresh_sink_address(monkeypatch):
    """An address registers once. A fixed default made every run after the
    first fail at E01 on the validator's own leftover account."""
    module = _load()
    monkeypatch.delenv("CANARY_EMAIL", raising=False)
    first, second = module.fresh_canary_email(), module.fresh_canary_email()
    assert first != second
    for address in (first, second):
        assert re.fullmatch(r"delivered\+prod-email-[0-9a-f]{10}@resend\.dev", address), address


def test_the_default_address_is_the_fresh_one_not_the_bare_sink():
    assert "default=os.getenv(\"CANARY_EMAIL\", DEFAULT_CANARY_EMAIL)" not in SOURCE
    assert "args.email = fresh_canary_email()" in SOURCE


def test_live_delivery_never_mails_a_null_mx_domain(monkeypatch):
    """With delivery live, a canary on example.com (null MX) is a guaranteed
    bounce against the sending domain's reputation. Disabled delivery sends
    nothing, so example.com stays the right canary there."""
    from deploy.production.validate import canary_email

    monkeypatch.delenv("CANARY_EMAIL_SINK", raising=False)
    assert canary_email("disabled", "abc123") == "prod-canary-abc123@example.com"
    live = canary_email("smtp", "abc123")
    assert live == "delivered+prod-canary-abc123@resend.dev"
    assert not live.endswith("@example.com")
    monkeypatch.setenv("CANARY_EMAIL_SINK", "sink@provider.test")
    assert canary_email("smtp", "abc123") == "sink+prod-canary-abc123@provider.test"


def test_the_live_sink_address_is_acceptable_to_the_registration_api():
    """A canary the API rejects on SYNTAX proves nothing (the Phase 7 lesson)."""
    from pydantic import BaseModel, EmailStr

    from deploy.production.validate import canary_email

    class _Addr(BaseModel):
        email: EmailStr

    for delivery in ("disabled", "smtp"):
        address = canary_email(delivery, "abc123")
        assert _Addr(email=address).email == address
