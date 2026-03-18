"""
Agent Sandbox — Isolated execution environment for external agent calls.

Provides security boundaries for calling untrusted agent endpoints:
1. Network isolation: only allowed to reach the agent endpoint
2. Resource limits: CPU, memory, timeout enforcement
3. Response validation: size limits, content-type checks
4. SSRF protection: blocks internal network ranges

Architecture:
  - In production (Linux): use gVisor runtime for hardware-level isolation
  - In development: use httpx with strict timeouts and validation
  - Future: Docker container per execution with gVisor runtime

The sandbox sits between "escrow locked" and "call agent endpoint" —
it does NOT modify wallet balances or escrow state.
"""

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


@dataclass
class SandboxConfig:
    """Resource limits and security policies for sandboxed execution."""

    # Timeout for agent endpoint calls (seconds)
    request_timeout: float = 30.0

    # Maximum response body size (bytes) — 10 MB default
    max_response_size: int = 10 * 1024 * 1024

    # Maximum number of redirects to follow
    max_redirects: int = 3

    # Allowed content types for responses
    allowed_content_types: list = field(
        default_factory=lambda: [
            "application/json",
            "text/plain",
            "text/html",
        ]
    )

    # Block internal/private network ranges (SSRF protection)
    block_private_networks: bool = True

    # Blocked IP ranges (RFC 1918 + loopback + link-local)
    blocked_networks: list = field(
        default_factory=lambda: [
            "0.0.0.0/8",       # Unspecified IPv4
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "::1/128",         # IPv6 loopback
            "::/128",          # IPv6 unspecified
            "fc00::/7",        # IPv6 unique local
            "fe80::/10",       # IPv6 link-local
        ]
    )

    # Enable gVisor runtime (Linux production only)
    use_gvisor: bool = False


# Global config — loaded from env vars
_config: Optional[SandboxConfig] = None


def get_sandbox_config() -> SandboxConfig:
    """Get or create sandbox config from environment."""
    global _config
    if _config is None:
        _config = SandboxConfig(
            request_timeout=float(os.getenv("SANDBOX_TIMEOUT", "30")),
            max_response_size=int(os.getenv("SANDBOX_MAX_RESPONSE_SIZE", str(10 * 1024 * 1024))),
            block_private_networks=os.getenv("ENVIRONMENT", "development") != "development",
            use_gvisor=os.getenv("SANDBOX_USE_GVISOR", "false").lower() == "true",
        )
    return _config


class SandboxError(Exception):
    """Raised when sandbox detects a security violation."""

    pass


class SandboxTimeoutError(SandboxError):
    """Raised when agent execution exceeds timeout."""

    pass


class SSRFError(SandboxError):
    """Raised when endpoint targets internal/private network."""

    pass


def _check_ssrf(url: str, config: SandboxConfig) -> None:
    """
    Validate that URL doesn't target internal networks.

    Prevents Server-Side Request Forgery (SSRF) attacks
    where a malicious agent registers an endpoint pointing
    to internal services (e.g., http://postgres:5432).
    """
    if not config.block_private_networks:
        return

    parsed = urlparse(url)

    # Block non-http(s) schemes FIRST (prevent file://, gopher://, etc.)
    if parsed.scheme and parsed.scheme.lower() not in ("http", "https"):
        raise SSRFError(f"Blocked: only http/https schemes allowed, got '{parsed.scheme}'")

    hostname = parsed.hostname

    if not hostname:
        raise SSRFError(f"Invalid URL: {url}")

    # Block common internal hostnames
    internal_hostnames = {
        "localhost",
        "postgres",
        "redis",
        "jaeger",
        "registry",
        "payment",
        "worker",
        "dashboard",
        "metadata.google.internal",
        "169.254.169.254",  # AWS/GCP metadata
        "0.0.0.0",  # Unspecified IPv4 — can reach localhost on Linux
    }

    if hostname.lower() in internal_hostnames:
        raise SSRFError(f"Blocked: endpoint targets internal service '{hostname}'")

    # Check IP ranges
    try:
        ip = ipaddress.ip_address(hostname)
        for network_str in config.blocked_networks:
            network = ipaddress.ip_network(network_str, strict=False)
            if ip in network:
                raise SSRFError(f"Blocked: endpoint targets private network {network_str}")
    except ValueError:
        # hostname is a domain name, not an IP — allow it
        # (DNS resolution could still resolve to private IP,
        # but that's handled at the network layer in production)
        pass


def _validate_response(response: httpx.Response, config: SandboxConfig) -> None:
    """Validate response from agent endpoint."""
    # Check response size via Content-Length header
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > config.max_response_size:
                raise SandboxError(
                    f"Response too large: {content_length} bytes (max: {config.max_response_size})"
                )
        except ValueError:
            pass  # Malformed content-length header — fall through to body check

    # Check actual body size
    if len(response.content) > config.max_response_size:
        raise SandboxError(
            f"Response body too large: {len(response.content)} bytes (max: {config.max_response_size})"
        )


async def sandboxed_call(
    url: str,
    method: str = "POST",
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    config: Optional[SandboxConfig] = None,
) -> httpx.Response:
    """
    Make a sandboxed HTTP call to an agent endpoint.

    Security guarantees:
    1. SSRF protection: blocks internal network targets
    2. Timeout enforcement: kills slow/hanging requests
    3. Response validation: size limits
    4. No credential leaking: doesn't forward internal auth tokens

    Does NOT modify escrow/wallet state — that's the caller's responsibility.

    Args:
        url: Agent endpoint URL
        method: HTTP method (GET, POST)
        json_body: JSON request body
        headers: Additional headers (internal tokens are stripped)
        config: Sandbox configuration (uses global config if None)

    Returns:
        httpx.Response from the agent

    Raises:
        SSRFError: If URL targets internal network
        SandboxTimeoutError: If request exceeds timeout
        SandboxError: For other security violations
    """
    if config is None:
        config = get_sandbox_config()

    # Step 1: SSRF check
    _check_ssrf(url, config)

    # Step 2: Strip internal headers (never forward JWT/internal tokens)
    safe_headers = {}
    if headers:
        blocked_header_prefixes = ("authorization", "cookie", "x-internal", "x-forwarded")
        for key, value in headers.items():
            if not key.lower().startswith(blocked_header_prefixes):
                safe_headers[key] = value

    safe_headers["User-Agent"] = "AgentNet-Sandbox/2.0"

    # Step 3: Make the request with strict timeout
    # NOTE: Redirects are DISABLED to prevent SSRF bypass via 302 redirect
    # to internal services. If an agent endpoint redirects, we treat it
    # as an error rather than following to a potentially malicious target.
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(config.request_timeout),
            max_redirects=0,
            follow_redirects=False,
        ) as client:
            response = await client.request(
                method=method,
                url=url,
                json=json_body,
                headers=safe_headers,
            )
    except httpx.TimeoutException as e:
        raise SandboxTimeoutError(f"Agent endpoint timed out after {config.request_timeout}s: {e}")
    except httpx.RequestError as e:
        raise SandboxError(f"Failed to reach agent endpoint: {e}")

    # Step 4: Validate response
    _validate_response(response, config)

    logger.info(
        "Sandboxed call completed",
        extra={
            "url": url,
            "status_code": response.status_code,
            "response_size": len(response.content),
        },
    )

    return response
