"""Structural proof that the Society's GitHub App credential works.

Run this before enabling real promotion. It answers one question — *can the
controller mint a working, correctly scoped installation token?* — and it
answers it without the operator, this process's logs, or any report ever
containing key material or the token itself.

    python -m app.society.github_preflight            # human-readable
    python -m app.society.github_preflight --json     # one JSON object

Verdict on the last line, mirroring the model canary:

    GITHUB APP READY
    GITHUB APP BLOCKED — <reason>

What is proven, and why each check earns its place:

* **G01/G02** the configuration is valid and the provider really is ``app`` —
  a ``disabled`` or ``static`` provider silently doing nothing is the failure
  mode this catches.
* **G03** the private key reaches the provider as a mounted file or an
  environment PEM, and the *source*, never the material, is reported.
* **G04** a token is actually minted and expires in the future.
* **G05** the second call is served from the in-memory cache, not re-minted —
  the token lifecycle is a cache, and a cache that never hits is a bug that
  only shows up as rate limiting later.
* **G06** the installation is scoped to exactly the configured repository.
  This is the check that proves "installed on one repository", which no
  amount of reading the App settings page can prove about the *running*
  credential.
* **G07** the token CANNOT read Actions secrets. A positive permission list
  says what was asked for; this says what was not granted.
* **G08** the report itself is scanned for the token before it is printed.
  A preflight that leaks the thing it is proving is worse than no preflight.
* **G09** ``invalidate()`` drops the cache so the next call re-mints, which is
  what a 401 mid-flight relies on.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import get_settings, validate_settings
from .github_credentials import (
    MINIMUM_PERMISSIONS,
    Credential,
    CredentialError,
    GitHubAppCredentialProvider,
    _default_transport,
)

Transport = Callable[..., Tuple[int, Dict[str, Any]]]


class Report:
    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []
        self.facts: Dict[str, Any] = {}

    def check(self, code: str, ok: bool, note: str) -> bool:
        self.checks.append({"code": code, "ok": bool(ok), "note": note})
        return bool(ok)

    @property
    def failures(self) -> List[str]:
        return [c["code"] for c in self.checks if not c["ok"]]

    def as_dict(self, verdict: str) -> Dict[str, Any]:
        return {"verdict": verdict, "checks": self.checks, "facts": self.facts}


def _probe(transport: Transport, token: str, api: str, path: str, timeout: float) -> Tuple[int, Dict[str, Any]]:
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        return transport("GET", f"{api}{path}", headers, None, timeout)
    except Exception as exc:  # noqa: BLE001 — the request carried the token; never echo it
        return 0, {"_transport_error": type(exc).__name__}
    finally:
        del headers, token


def run(
    *,
    settings: Optional[Any] = None,
    transport: Optional[Transport] = None,
    provider: Optional[Any] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Returns ``(verdict, report_dict)``. Never returns or logs the token."""
    s = settings or get_settings()
    tx: Transport = transport or _default_transport
    rep = Report()
    api = s.github_api_url.rstrip("/")
    repo = (s.github_repository or "").strip()
    rep.facts.update(
        {
            "credential_provider": s.github_credential_provider,
            "promotion_provider": s.promotion_provider,
            "auto_merge_enabled": bool(s.auto_merge_enabled),
            "repository": repo,
            "base_branch": s.github_base_branch,
            "api": api,
            "app_id": s.github_app_id,
            "installation_id": s.github_installation_id,
            "key_source": "file" if s.github_app_private_key_file else "env-pem",
            "requested_permissions": dict(MINIMUM_PERMISSIONS),
        }
    )

    problems = [p for p in validate_settings(s) if "GITHUB" in p.upper()]
    rep.check("G01", not problems, "; ".join(problems) or "github configuration valid")
    if not rep.check("G02", s.github_credential_provider == "app", f"credential provider is {s.github_credential_provider!r}"):
        return _finish(rep)
    rep.check("G03", bool(s.github_app_id and s.github_installation_id), f"app_id and installation_id present (key from {rep.facts['key_source']})")

    try:
        prov = provider or GitHubAppCredentialProvider(s, transport=tx)
    except CredentialError as exc:
        rep.check("G04", False, f"provider unavailable: {type(exc).__name__}: {exc}")
        return _finish(rep)

    try:
        cred: Credential = prov.get()
    except CredentialError as exc:
        rep.check("G04", False, f"mint failed: {type(exc).__name__}: {exc}")
        return _finish(rep)

    expires = cred.expires_at.isoformat() if cred.expires_at else None
    rep.facts["token_expires_at"] = expires
    rep.facts["token_source"] = cred.source
    rep.facts["git_username"] = cred.username
    rep.check("G04", bool(cred.token) and cred.expires_at is not None, f"installation token minted, expires_at={expires}")

    prov.get()  # second call must be served from cache
    stats = dict(getattr(prov, "stats", {}) or {})
    rep.facts["stats_after_two_gets"] = stats
    rep.check("G05", stats.get("minted") == 1 and stats.get("reused") == 1, f"token cache: {stats}")

    status, body = _probe(tx, cred.token, api, "/installation/repositories", float(s.model_timeout_seconds))
    names = sorted(str(r.get("full_name", "")) for r in (body.get("repositories") or []) if isinstance(r, dict))
    rep.facts["installation_repositories"] = names
    rep.check(
        "G06",
        status == 200 and names == [repo] if repo else False,
        f"installation scope HTTP {status}: {names or body.get('_transport_error') or 'none'} (configured: {repo!r})",
    )

    sec_status, _ = _probe(tx, cred.token, api, f"/repos/{repo}/actions/secrets", float(s.model_timeout_seconds))
    rep.facts["actions_secrets_probe_status"] = sec_status
    rep.check("G07", sec_status in (403, 404), f"Actions secrets refused to the App token (HTTP {sec_status}; 403/404 expected)")

    prov.invalidate()
    after = dict(getattr(prov, "stats", {}) or {})
    rep.facts["stats_after_invalidate"] = after
    rep.check("G09", after.get("cached") == 0, f"invalidate() dropped the cached token: {after}")

    verdict, payload = _finish(rep)
    # G08 last: the report is only safe once it is complete.
    leaked = cred.token and cred.token in json.dumps(payload, default=str)
    payload["checks"].append({"code": "G08", "ok": not leaked, "note": "report contains no token material" if not leaked else "TOKEN PRESENT IN REPORT"})
    if leaked:
        return "GITHUB APP BLOCKED — the preflight report contained token material", {"verdict": "blocked", "checks": payload["checks"], "facts": {}}
    return verdict, payload


def _finish(rep: Report) -> Tuple[str, Dict[str, Any]]:
    if rep.failures:
        verdict = f"GITHUB APP BLOCKED — failed {','.join(rep.failures)}"
    else:
        verdict = "GITHUB APP READY"
    return verdict, rep.as_dict(verdict)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m app.society.github_preflight", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="emit one JSON object instead of lines")
    args = parser.parse_args(argv)

    verdict, payload = run()
    if args.json:
        print(json.dumps(payload, default=str, sort_keys=True))
    else:
        for key in sorted(payload.get("facts", {})):
            print(f"GITHUB-PREFLIGHT fact {key}={payload['facts'][key]}")
        for c in payload["checks"]:
            print(f"GITHUB-PREFLIGHT {c['code']} {'PASS' if c['ok'] else 'FAIL'} {c['note']}")
    print(verdict)
    return 0 if verdict.startswith("GITHUB APP READY") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
