"""A2A feature flags, limits and protocol constants (ADR-0009 D17).

Every value is read at call time: an operator flips a flag with a restart,
and tests can set the environment per test. Every flag defaults to
``false`` so a missing variable fails closed.
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: The only protocol line AgentNet advertises and accepts (docs/A2A_BASELINE.md §1).
PROTOCOL_VERSION = "1.0"
BINDING_JSONRPC = "JSONRPC"
BINDING_HTTP_JSON = "HTTP+JSON"
JSONRPC_PATH = "/a2a"
HTTP_JSON_PATH = "/a2a/http"
A2A_MEDIA_TYPE = "application/a2a+json"

ECONOMICS_EXTENSION_URI = "https://agentnet.io.vn/a2a/extensions/economics/v1"
FEDERATION_EXTENSION_URI = "https://agentnet.io.vn/a2a/extensions/federation/v1"
SUPPORTED_EXTENSIONS = frozenset({ECONOMICS_EXTENSION_URI, FEDERATION_EXTENSION_URI})

#: Network-tenant skills (the registry itself, no tenant) — free and immediate.
SKILL_MARKETPLACE_SEARCH = "agentnet.marketplace.search"
SKILL_AGENT_CARD = "agentnet.agents.card"

_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in _TRUE


def _bounded_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        value = default
    return max(lo, min(hi, value))


def _production() -> bool:
    return os.getenv("ENVIRONMENT", "development").strip().lower() == "production"


def server_enabled() -> bool:
    """Inbound A2A gateway, streams, reconciler and cards."""
    return _flag("A2A_SERVER_ENABLED")


def federation_enabled() -> bool:
    """Remote card fetches, catalog writes and the outbound client."""
    return _flag("A2A_FEDERATION_ENABLED")


def society_client_enabled() -> bool:
    """Society A2A intents (still subject to grants and policy)."""
    return _flag("A2A_SOCIETY_CLIENT_ENABLED")


def v03_compat_requested() -> bool:
    """A2A 0.3 compatibility is not implemented (ADR-0009 D3). The flag is
    reserved so nobody enables the SDK adapter by editing code: a ``true``
    value is refused at startup and changes nothing."""
    return _flag("A2A_V03_COMPAT_ENABLED")


def public_base_url() -> Optional[str]:
    """The public origin advertised in Agent Cards (``https://api.agentnet.io.vn``).

    Never derived from request headers: a card built from ``Host`` or
    ``X-Forwarded-Host`` would let any client make a cached card advertise its
    own host. Production requires an ``https`` origin with no path, query or
    credentials; anything else disables the cards and the gateway."""
    raw = os.getenv("A2A_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return None if _production() else "http://localhost:8000"
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        return None
    if _production() and parts.scheme != "https":
        return None
    return f"{parts.scheme}://{parts.netloc}"


def gateway_ready() -> bool:
    """The gateway serves only when enabled AND it can describe itself truthfully."""
    return server_enabled() and public_base_url() is not None


def blocking_wait_seconds() -> int:
    """Upper bound for a blocking SendMessage (below Cloudflare's 100 s proxy timeout)."""
    return _bounded_int("A2A_BLOCKING_WAIT_SECONDS", 60, 0, 90)


def max_streams_per_principal() -> int:
    return _bounded_int("A2A_MAX_STREAMS_PER_PRINCIPAL", 4, 1, 32)


def max_subscribers_per_task() -> int:
    return _bounded_int("A2A_MAX_SUBSCRIBERS_PER_TASK", 16, 1, 128)


def stream_max_seconds() -> int:
    return _bounded_int("A2A_STREAM_MAX_SECONDS", 900, 30, 3600)


def max_request_bytes() -> int:
    return _bounded_int("A2A_MAX_REQUEST_BYTES", 262_144, 1_024, 4_194_304)


def max_federation_depth() -> int:
    return _bounded_int("A2A_MAX_FEDERATION_DEPTH", 2, 0, 5)


def reconcile_interval_seconds() -> int:
    return _bounded_int("A2A_RECONCILE_INTERVAL_SECONDS", 2, 1, 60)


#: Message bounds (ADR-0009 D13): enforced before anything is stored.
MAX_PARTS = 16
MAX_TEXT_CHARS = 32_000
MAX_ID_CHARS = 128
MAX_DATA_DEPTH = 16
MAX_HISTORY = 100
LIST_PAGE_SIZE_DEFAULT = 50
LIST_PAGE_SIZE_MAX = 100
#: A SUBMITTED task with no TaskSession older than this was interrupted before escrow.
ORPHAN_AFTER_SECONDS = 300
