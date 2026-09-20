"""The GitHub App preflight proves the credential works and never leaks it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from services.registry.app.society import github_preflight as gp
from services.registry.app.society.config import SocietySettings

TOKEN = "ghs_" + "T" * 36
REPO = "vansyson1308/agentnet"


def _settings(**over):
    base = dict(
        github_credential_provider="app",
        github_app_id="123456",
        github_installation_id="7654321",
        github_repository=REPO,
        promotion_provider="github",
        auto_merge_enabled=False,
        # A PATH, never key material — config.py refuses a file value that
        # starts with "-----", and the stand-in provider never reads it.
        github_app_private_key_file="/run/secrets/society-github-app.pem",
    )
    base.update(over)
    return SocietySettings(**base)


def _transport(*, repos=None, secrets_status=403, token=TOKEN, mint_status=201):
    """Mimics httpx: (method, url, headers, json, timeout) -> (status, body)."""
    seen = []
    repos = [{"full_name": REPO}] if repos is None else repos

    def tx(method, url, headers, body=None, timeout=None):
        seen.append((method, url, headers.get("Authorization", "")[:6]))
        if url.endswith("/access_tokens"):
            exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            return mint_status, ({"token": token, "expires_at": exp} if mint_status == 201 else {})
        if url.endswith("/installation/repositories"):
            return 200, {"total_count": len(repos), "repositories": repos}
        if url.endswith("/actions/secrets"):
            return secrets_status, {}
        return 404, {}

    tx.seen = seen
    return tx


class _Prov:
    """Stands in for GitHubAppCredentialProvider without touching a key."""

    def __init__(self, token=TOKEN, expires_in=3600):
        from services.registry.app.society.github_credentials import Credential

        self._cred = Credential(
            token=token,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            source="app",
        )
        self._cached = None
        self.minted = 0
        self.reused = 0

    def get(self):
        if self._cached is None:
            self._cached = self._cred
            self.minted += 1
        else:
            self.reused += 1
        return self._cached

    def invalidate(self):
        self._cached = None

    @property
    def stats(self):
        return {"minted": self.minted, "reused": self.reused, "invalidations": 0, "cached": int(self._cached is not None)}


def test_a_correctly_scoped_app_is_ready():
    verdict, report = gp.run(settings=_settings(), transport=_transport(), provider=_Prov())
    assert verdict == "GITHUB APP READY", report
    codes = {c["code"]: c["ok"] for c in report["checks"]}
    assert all(codes.values()), report["checks"]
    assert {"G01", "G02", "G03", "G04", "G05", "G06", "G07", "G08", "G09"} <= set(codes)


def test_the_report_never_contains_the_token():
    verdict, report = gp.run(settings=_settings(), transport=_transport(), provider=_Prov())
    blob = json.dumps(report, default=str)
    assert TOKEN not in blob
    assert "ghs_" not in blob
    assert any(c["code"] == "G08" and c["ok"] for c in report["checks"])


def test_an_installation_on_extra_repositories_is_blocked():
    """§12: the App must be installed on exactly one repository."""
    tx = _transport(repos=[{"full_name": REPO}, {"full_name": "vansyson1308/other"}])
    verdict, report = gp.run(settings=_settings(), transport=tx, provider=_Prov())
    assert verdict.startswith("GITHUB APP BLOCKED")
    assert "G06" in verdict
    g06 = next(c for c in report["checks"] if c["code"] == "G06")
    assert "other" in g06["note"]


def test_an_app_that_can_read_actions_secrets_is_blocked():
    """Over-permissioning is a finding, not a convenience."""
    verdict, report = gp.run(settings=_settings(), transport=_transport(secrets_status=200), provider=_Prov())
    assert verdict.startswith("GITHUB APP BLOCKED")
    assert "G07" in verdict


def test_a_disabled_provider_is_blocked_before_any_network_call():
    tx = _transport()
    verdict, _ = gp.run(settings=_settings(github_credential_provider="disabled"), transport=tx, provider=_Prov())
    assert verdict.startswith("GITHUB APP BLOCKED") and "G02" in verdict
    assert tx.seen == [], "a disabled provider must not reach the network"


def test_a_token_served_from_cache_is_required():
    class _AlwaysMints(_Prov):
        def get(self):
            self.minted += 1
            return self._cred

    verdict, _ = gp.run(settings=_settings(), transport=_transport(), provider=_AlwaysMints())
    assert verdict.startswith("GITHUB APP BLOCKED") and "G05" in verdict


def test_a_mint_failure_is_reported_without_key_material():
    from services.registry.app.society.github_credentials import CredentialRefused

    class _Refuses(_Prov):
        def get(self):
            raise CredentialRefused("token endpoint refused the App JWT (401)")

    verdict, report = gp.run(settings=_settings(), transport=_transport(), provider=_Refuses())
    assert verdict.startswith("GITHUB APP BLOCKED") and "G04" in verdict
    blob = json.dumps(report, default=str)
    assert "BEGIN" not in blob and "PRIVATE KEY" not in blob


def test_facts_carry_identifiers_but_never_key_material():
    _, report = gp.run(settings=_settings(), transport=_transport(), provider=_Prov())
    facts = report["facts"]
    assert facts["app_id"] == "123456" and facts["installation_id"] == "7654321"
    assert facts["key_source"] in ("file", "env-pem")
    assert facts["installation_repositories"] == [REPO]
    assert facts["auto_merge_enabled"] is False
    assert "private_key" not in json.dumps(facts).lower()
