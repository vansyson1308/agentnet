"""GitHub credential boundary for the Promotion Controller (Phase 3.1).

Everything that can hold a GitHub secret lives in THIS module and nowhere
else in the society package (``tests/society/test_secret_boundary.py``):

* ``DisabledGitHubCredentialProvider`` — the default; every request fails
  closed with :class:`CredentialUnavailable`, so the GitHub promotion
  provider is inert.
* ``StaticTokenCredentialProvider`` — reads ``SOCIETY_GITHUB_TOKEN`` from the
  controller process environment at call time (tests, or a temporary
  operator-supplied installation token). Nothing is cached.
* ``GitHubAppCredentialProvider`` — the future production path. It signs a
  short-lived RS256 JWT with the App private key (a platform-mounted PEM
  file, or an env value only where a mount is impossible), exchanges it for
  an installation access token scoped to exactly this repository and the
  minimum permissions, caches the token in process memory, reuses it while
  it is safely valid, refreshes it before expiry, invalidates it on a 401,
  and never assumes a token length or prefix (GitHub is rolling out the
  stateless ``ghs_APPID_JWT`` format, 2026). Refreshes are single-flight.

Nothing here is ever serialised: the credential object redacts itself in
``repr``/``str``, exceptions carry status codes and never response bodies,
and the model, the context builder, events, runs and memory never import
this module. The private key is read at signing time and dropped afterwards;
it is never stored on an object.

Official sources (docs/adr/0005-predeploy-boundary-closure.md): GitHub
"Generating a JSON web token (JWT) for a GitHub App" (RS256; ``iat`` 60 s in
the past; ``exp`` at most 10 min), "Generating an installation access token"
(1 h lifetime; ``repositories`` / ``permissions`` downscoping; 401 on
expiry), "Managing private keys for GitHub Apps" (key vault or environment;
never hard-coded), gitcredentials(7) (``GIT_ASKPASS``).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from .config import SocietySettings

logger = logging.getLogger(__name__)

TOKEN_ENV = "SOCIETY_GITHUB_TOKEN"                       # static provider only
PRIVATE_KEY_PEM_ENV = "SOCIETY_GITHUB_APP_PRIVATE_KEY_PEM"  # app provider, fallback to a file mount
GIT_USERNAME = "x-access-token"

# Exactly what the Society Promotion App needs (docs/GITHUB_PROMOTION.md);
# the installation token is downscoped to these even if the App has more.
MINIMUM_PERMISSIONS: Dict[str, str] = {"contents": "write", "pull_requests": "write", "checks": "read", "metadata": "read"}

_JWT_SKEW_SECONDS = 60          # GitHub recommendation: iat 60 s in the past
_JWT_LIFETIME_SECONDS = 9 * 60  # GitHub maximum is 10 min; stay conservative
_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class CredentialError(Exception):
    """Base class. Messages never contain secret material."""


class CredentialUnavailable(CredentialError):
    """Provider inert / not configured (fail closed)."""


class CredentialRefused(CredentialError):
    """GitHub refused to mint a token (401/403/404/422)."""


class CredentialTransient(CredentialError):
    """Timeout, 429 or 5xx while minting; safe to retry later."""


@dataclass
class Credential:
    token: str
    expires_at: Optional[datetime]
    source: str
    username: str = GIT_USERNAME

    def __repr__(self) -> str:  # never leak
        exp = self.expires_at.isoformat() if self.expires_at else "unknown"
        return f"Credential(source={self.source!r}, username={self.username!r}, token=<redacted>, expires_at={exp})"

    __str__ = __repr__


class GitHubCredentialProvider(Protocol):
    name: str

    def get(self) -> Credential: ...  # pragma: no cover - protocol

    def invalidate(self) -> None: ...  # pragma: no cover - protocol


class DisabledGitHubCredentialProvider:
    name = "disabled"

    def get(self) -> Credential:
        raise CredentialUnavailable("no GitHub credential provider configured (SOCIETY_GITHUB_CREDENTIAL_PROVIDER=disabled)")

    def invalidate(self) -> None:
        return None

    def __repr__(self) -> str:
        return "DisabledGitHubCredentialProvider()"


class StaticTokenCredentialProvider:
    """Reads the token from the environment on every call; nothing cached."""

    name = "static"

    def get(self) -> Credential:
        tok = os.getenv(TOKEN_ENV, "").strip()
        if not tok:
            raise CredentialUnavailable(f"{TOKEN_ENV} is not set; the GitHub promotion provider is inert")
        return Credential(token=tok, expires_at=None, source="static")

    def invalidate(self) -> None:
        return None

    def __repr__(self) -> str:
        return "StaticTokenCredentialProvider(token=<from environment at call time>)"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_rs256(pem: bytes, message: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CredentialRefused("the App private key is not an RSA key (GitHub Apps require RS256)")
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


def _parse_expiry(value: Any, now: datetime) -> datetime:
    """GitHub returns ISO-8601 ``expires_at`` (``...Z``). A missing or
    unparseable value is treated as ALREADY short-lived so the token is
    refreshed at the next opportunity instead of trusted for an hour."""
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    return now + timedelta(minutes=5)


def _default_transport(method: str, url: str, headers: Dict[str, str], body: Optional[Dict[str, Any]], timeout: float) -> Tuple[int, Dict[str, Any]]:
    import httpx

    with httpx.Client(timeout=timeout) as client:
        resp = client.request(method, url, json=body, headers=headers)
    data: Dict[str, Any] = {}
    if resp.content:
        try:
            data = resp.json()
        except ValueError:
            data = {}
    return resp.status_code, data if isinstance(data, dict) else {}


@dataclass
class _Cache:
    credential: Optional[Credential] = None
    minted: int = 0
    reused: int = 0
    invalidations: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class GitHubAppCredentialProvider:
    """App JWT -> installation access token, cached and refreshed in memory.

    ``transport`` (tests) mimics ``httpx``: ``(method, url, headers, json,
    timeout) -> (status, body_dict)``. ``clock`` returns the current UTC time.
    """

    name = "app"

    def __init__(
        self,
        settings: SocietySettings,
        *,
        transport: Optional[Callable[..., Tuple[int, Dict[str, Any]]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        refresh_margin_seconds: Optional[int] = None,
    ):
        if settings.github_credential_provider != "app":
            raise CredentialUnavailable("SOCIETY_GITHUB_CREDENTIAL_PROVIDER is not 'app'")
        if not settings.github_app_id or not settings.github_installation_id:
            raise CredentialUnavailable("SOCIETY_GITHUB_APP_ID and SOCIETY_GITHUB_INSTALLATION_ID are required for the app credential provider")
        self._app_id = settings.github_app_id
        self._installation_id = settings.github_installation_id
        self._key_file = settings.github_app_private_key_file
        self._api = settings.github_api_url.rstrip("/")
        self._repository = settings.github_repository.strip()
        self._timeout = float(settings.model_timeout_seconds)
        self._margin = int(refresh_margin_seconds if refresh_margin_seconds is not None else settings.github_token_refresh_margin_seconds)
        self._transport = transport or _default_transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache = _Cache()

    # ── observability without secrets ──
    @property
    def stats(self) -> Dict[str, int]:
        c = self._cache
        return {"minted": c.minted, "reused": c.reused, "invalidations": c.invalidations, "cached": int(c.credential is not None)}

    def __repr__(self) -> str:
        return f"GitHubAppCredentialProvider(app_id={self._app_id!r}, installation_id={self._installation_id!r}, key=<{'file' if self._key_file else 'env'}:redacted>, cached={self._cache.credential is not None})"

    # ── private key: read at signing time, never stored ──
    def _private_key_pem(self) -> bytes:
        if self._key_file:
            path = pathlib.Path(self._key_file)
            try:
                pem = path.read_bytes()
            except OSError as exc:
                raise CredentialUnavailable(f"App private key file is not readable ({type(exc).__name__})") from None
        else:
            pem = os.getenv(PRIVATE_KEY_PEM_ENV, "").encode("utf-8")
        if b"PRIVATE KEY" not in pem:
            raise CredentialUnavailable("App private key material is missing or not PEM-encoded")
        return pem.replace(b"\\n", b"\n")

    def _mint_jwt(self, now: datetime) -> str:
        iat = int(now.timestamp()) - _JWT_SKEW_SECONDS
        payload = {"iat": iat, "exp": iat + _JWT_SKEW_SECONDS + _JWT_LIFETIME_SECONDS, "iss": self._app_id}
        header = {"alg": "RS256", "typ": "JWT"}
        signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}.{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
        pem = self._private_key_pem()
        try:
            signature = _sign_rs256(pem, signing_input.encode("ascii"))
        except CredentialError:
            raise
        except Exception as exc:  # noqa: BLE001 — never include the key or the error text
            raise CredentialRefused(f"could not sign the App JWT ({type(exc).__name__})") from None
        finally:
            del pem
        return f"{signing_input}.{_b64url(signature)}"

    def _exchange(self, now: datetime) -> Credential:
        jwt = self._mint_jwt(now)
        url = f"{self._api}/app/installations/{self._installation_id}/access_tokens"
        headers = {"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        body: Dict[str, Any] = {"permissions": dict(MINIMUM_PERMISSIONS)}
        if self._repository and "/" in self._repository:
            body["repositories"] = [self._repository.split("/", 1)[1]]
        try:
            status, data = self._transport("POST", url, headers, body, self._timeout)
        except Exception as exc:  # noqa: BLE001 — the request carried the JWT; never echo it
            raise CredentialTransient(f"token endpoint transport error ({type(exc).__name__})") from None
        finally:
            del jwt, headers
        if status in _RETRYABLE:
            raise CredentialTransient(f"token endpoint returned {status}")
        if status in (401, 403, 404, 422):
            raise CredentialRefused(f"token endpoint refused the App JWT ({status})")
        if status != 201 or not isinstance(data, dict) or not data.get("token"):
            raise CredentialRefused(f"token endpoint returned {status} without a token")
        expires_at = _parse_expiry(data.get("expires_at"), now)
        return Credential(token=str(data["token"]), expires_at=expires_at, source="app")

    def _fresh_enough(self, cred: Optional[Credential], now: datetime) -> bool:
        if cred is None or cred.expires_at is None:
            return False
        return now + timedelta(seconds=self._margin) < cred.expires_at

    def get(self) -> Credential:
        cache = self._cache
        now = self._clock()
        with cache.lock:  # single-flight: concurrent callers wait, then reuse
            if self._fresh_enough(cache.credential, now):
                cache.reused += 1
                return cache.credential  # type: ignore[return-value]
            cred = self._exchange(now)
            cache.credential = cred
            cache.minted += 1
            logger.info("github credentials: minted installation token (app=%s, installation=%s, expires_at=%s)", self._app_id, self._installation_id, cred.expires_at.isoformat() if cred.expires_at else "unknown")
            return cred

    def invalidate(self) -> None:
        cache = self._cache
        with cache.lock:
            if cache.credential is not None:
                cache.credential = None
                cache.invalidations += 1


def get_credential_provider(settings: SocietySettings, *, override: Optional[GitHubCredentialProvider] = None, **kwargs: Any) -> GitHubCredentialProvider:
    if override is not None:
        return override
    kind = settings.github_credential_provider
    if kind == "static":
        return StaticTokenCredentialProvider()
    if kind == "app":
        return GitHubAppCredentialProvider(settings, **kwargs)
    return DisabledGitHubCredentialProvider()


def redact(text: str, *secrets: str) -> str:
    """Remove every non-empty secret from a diagnostic string (defence in depth)."""
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


__all__ = [
    "Credential",
    "CredentialError",
    "CredentialRefused",
    "CredentialTransient",
    "CredentialUnavailable",
    "DisabledGitHubCredentialProvider",
    "GIT_USERNAME",
    "GitHubAppCredentialProvider",
    "GitHubCredentialProvider",
    "MINIMUM_PERMISSIONS",
    "PRIVATE_KEY_PEM_ENV",
    "StaticTokenCredentialProvider",
    "TOKEN_ENV",
    "get_credential_provider",
    "redact",
]
