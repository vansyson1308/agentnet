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
