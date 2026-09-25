"""Outbound network guard: resolve, check EVERY address, pin, verify TLS (ADR-0009 D12).

``SafeTransport`` is an httpx transport that, for each request:

1. accepts only ``https`` (``http`` only outside production, for local tests);
2. refuses credentials in the URL and privileged ports (anything below 1024
   except 80/443);
3. resolves the host and requires every resolved address to be public: no
   loopback, private (RFC 1918), shared (RFC 6598), link-local / metadata,
   ULA, multicast, reserved or unspecified addresses, including IPv4 inside
   IPv6 (mapped, NAT64, 6to4);
4. connects to the validated address itself (the URL host is rewritten to
   the IP; ``Host`` and the TLS SNI keep the name and the certificate is
   verified against it), so DNS rebinding between check and connect cannot
   redirect the connection;
5. never follows redirects on its own and caps the response body.

It is used by the Agent Card fetcher, the outbound A2A client and the
registry's legacy webhook dispatch (``sandbox.py``) outside development.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from .. import metrics

DEFAULT_MAX_BYTES = 256 * 1024
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_6TO4 = ipaddress.ip_network("2002::/16")
_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata", "instance-data"}


class OutboundRefused(Exception):
    """The destination is not allowed. ``reason`` is a bounded class name."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _production() -> bool:
    return os.getenv("ENVIRONMENT", "development").strip().lower() == "production"


def _test_hosts() -> set:
    """Hosts allowed to resolve to private addresses — local interop tests
    only. Ignored in production, whatever the variable says."""
    if _production():
        return set()
    raw = os.getenv("A2A_FEDERATION_TEST_PRIVATE_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _embedded_v4(ip: ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address]:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _6TO4:
        return ip.sixtofour
    return None


def address_is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_v4(ip)
        if inner is not None and not address_is_public(str(inner)):
            return False
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback or ip.is_link_local)


def check_url(url: str) -> Tuple[str, str, int, str]:
    """Static checks. Returns (scheme, host, port, path_and_query)."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    allowed = {"https"} if _production() else {"https", "http"}
    if scheme not in allowed:
        raise OutboundRefused("scheme", "only https destinations are allowed")
    if parts.username or parts.password:
        raise OutboundRefused("credentials", "credentials in the URL are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise OutboundRefused("host", "the URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise OutboundRefused("port", "invalid port")
    if port < 1024 and port not in (80, 443):
        raise OutboundRefused("port", "privileged ports other than 80/443 are not allowed")
    if host in _BLOCKED_HOSTNAMES and host not in _test_hosts():
        raise OutboundRefused("host", "internal host names are not allowed")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return scheme, host, port, path


def _resolve(host: str, port: int) -> List[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OutboundRefused("resolution", f"could not resolve {host}") from exc
    addresses = []
    for info in infos:
        addr = info[4][0]
        if addr not in addresses:
            addresses.append(addr)
    if not addresses:
        raise OutboundRefused("resolution", f"could not resolve {host}")
    return addresses


async def resolve_public(host: str, port: int) -> str:
    """Resolve ``host``; EVERY address must be public. Returns the one to use."""
    loop = asyncio.get_running_loop()
    addresses = await loop.run_in_executor(None, _resolve, host, port)
    if host not in _test_hosts():
        bad = [a for a in addresses if not address_is_public(a)]
        if bad:
            raise OutboundRefused("private_address", f"{host} resolves to a non-public address")
    return addresses[0]


class _CappedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, limit: int):
        self._inner = inner
        self._limit = limit

    async def __aiter__(self):
        seen = 0
        async for chunk in self._inner:
            seen += len(chunk)
            if seen > self._limit:
                raise OutboundRefused("too_large", f"response exceeds {self._limit} bytes")
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class SafeTransport(httpx.AsyncBaseTransport):
    """See module docstring. One instance per client; not shared across event loops."""

    def __init__(self, *, max_response_bytes: int = DEFAULT_MAX_BYTES, verify: bool = True):
        self._inner = httpx.AsyncHTTPTransport(verify=verify, retries=0, http2=False)
        self._max = max_response_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            scheme, host, port, _path = check_url(str(request.url))
            address = await resolve_public(host, port)
        except OutboundRefused as exc:
            metrics.record_ssrf_rejection(exc.reason)
            raise
        request.url = request.url.copy_with(host=address)  # connect to the checked address
        request.headers["Host"] = host if port in (80, 443) else f"{host}:{port}"
        request.headers["Accept-Encoding"] = "identity"
        request.extensions = dict(request.extensions or {})
        if scheme == "https":
            request.extensions["sni_hostname"] = host
        response = await self._inner.handle_async_request(request)
        encoding = response.headers.get("content-encoding", "identity").lower()
        if encoding not in ("", "identity"):
            await response.aclose()
            raise OutboundRefused("too_large", "compressed responses are refused")
        response.stream = _CappedStream(response.stream, self._max)  # type: ignore[arg-type]
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def safe_client(*, timeout: float = 10.0, max_response_bytes: int = DEFAULT_MAX_BYTES, headers: Optional[dict] = None) -> httpx.AsyncClient:
    """An httpx client that cannot leave the public internet, never follows
    redirects, ignores proxy/credential environment variables and bounds
    every response."""
    return httpx.AsyncClient(
        transport=SafeTransport(max_response_bytes=max_response_bytes),
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "AgentNet-A2A/1.0", **(headers or {})},
    )
