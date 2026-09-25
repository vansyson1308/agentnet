"""Fetch and validate a remote Agent Card (ADR-0009 D12, D13).

Everything fetched is UNTRUSTED EXTERNAL DATA. The fetcher goes through
``netguard.safe_client`` (public addresses only, pinned connection, no
proxies, 256 KiB cap, identity encoding), follows at most three redirects
and re-validates each hop, requires a JSON content type, and parses the card
with the official SDK types before bounding every string AgentNet keeps.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from a2a.types import a2a_pb2 as pb
from google.protobuf.json_format import MessageToDict, ParseDict

from .. import metrics
from .netguard import OutboundRefused, check_url, safe_client

WELL_KNOWN = "/.well-known/agent-card.json"
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 10.0
MAX_CARD_BYTES = 256 * 1024
MAX_SKILLS = 128
SUPPORTED_BINDINGS = ("JSONRPC", "HTTP+JSON")


class CardFetchError(Exception):
    def __init__(self, result: str, message: str):
        super().__init__(message)
        self.result = result  # bounded class (metrics.FETCH_RESULTS)


@dataclass
class FetchedCard:
    card: pb.AgentCard
    summary: Dict[str, Any]
    card_hash: str
    etag: Optional[str]
    final_url: str
    not_modified: bool = False


def normalize_card_url(url: str) -> str:
    """A base URL gets the well-known path; a full card URL is kept."""
    url = url.strip()
    check_url(url)
    parts = urlsplit(url)
    if parts.path in ("", "/"):
        return f"{parts.scheme}://{parts.netloc}{WELL_KNOWN}"
    return url


def _text(value: str, limit: int) -> str:
    return (value or "").replace("\x00", "")[:limit]


def _v1_interfaces(card: pb.AgentCard) -> List[pb.AgentInterface]:
    return [
        i
        for i in card.supported_interfaces
        if i.protocol_binding in SUPPORTED_BINDINGS and (i.protocol_version or "").split(".")[0] == "1" and i.url
    ]


def validate_card(card: pb.AgentCard) -> Dict[str, Any]:
    """AgentNet's bounds on top of the protocol schema. Returns the sanitized
    summary AgentNet stores and shows (the raw card is kept only as data)."""
    if not card.name:
        raise CardFetchError("invalid_card", "the card has no name")
    interfaces = _v1_interfaces(card)
    if not interfaces:
        raise CardFetchError("invalid_card", "the card declares no A2A 1.x JSONRPC or HTTP+JSON interface")
    for iface in interfaces:
        try:
            check_url(iface.url)
        except OutboundRefused as exc:
            raise CardFetchError("invalid_card", f"interface URL refused: {exc.reason}")
    if len(card.skills) > MAX_SKILLS:
        raise CardFetchError("invalid_card", f"the card declares more than {MAX_SKILLS} skills")
    schemes = {}
    for name, scheme in card.security_schemes.items():
        kind = scheme.WhichOneof("scheme") or "unknown"
        entry = {"type": kind}
        if kind == "http_auth_security_scheme":
            entry["scheme"] = _text(scheme.http_auth_security_scheme.scheme, 32)
        schemes[_text(name, 64)] = entry
    return {
        "name": _text(card.name, 255),
        "description": _text(card.description, 4000),
        "version": _text(card.version, 64),
        "provider": {"organization": _text(card.provider.organization, 255), "url": _text(card.provider.url, 2048)},
        "interfaces": [
            {"url": _text(i.url, 2048), "binding": i.protocol_binding, "version": _text(i.protocol_version, 16), "tenant": _text(i.tenant, 255)}
            for i in interfaces
        ],
        "protocolVersions": sorted({_text(i.protocol_version, 16) for i in interfaces}),
        "bindings": sorted({i.protocol_binding for i in interfaces}),
        "skills": [
            {
                "id": _text(s.id, 128),
                "name": _text(s.name, 255),
                "description": _text(s.description, 1000),
                "tags": [_text(t, 64) for t in list(s.tags)[:16]],
                "inputModes": [_text(m, 64) for m in list(s.input_modes)[:8]],
                "outputModes": [_text(m, 64) for m in list(s.output_modes)[:8]],
            }
            for s in card.skills
        ],
        "capabilities": {
            "streaming": bool(card.capabilities.streaming),
            "pushNotifications": bool(card.capabilities.push_notifications),
            "extensions": [{"uri": _text(e.uri, 512), "required": bool(e.required)} for e in list(card.capabilities.extensions)[:32]],
        },
        "security": {"schemes": schemes, "requirements": [sorted(r.schemes.keys()) for r in card.security_requirements][:16]},
    }


def card_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def fetch_card(url: str, *, etag: Optional[str] = None, client: Optional[httpx.AsyncClient] = None) -> FetchedCard:
    """Fetch, validate and summarize a card. Raises CardFetchError (bounded ``result``)."""
    target = normalize_card_url(url)
    own = client is None
    http = client or safe_client(timeout=TIMEOUT_SECONDS, max_response_bytes=MAX_CARD_BYTES)
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            headers = {"Accept": "application/json"}
            if etag:
                headers["If-None-Match"] = etag
            try:
                response = await http.get(target, headers=headers)
            except OutboundRefused as exc:
                metrics.record_fetch("ssrf_refused" if exc.reason != "too_large" else "too_large")
                raise CardFetchError("ssrf_refused" if exc.reason != "too_large" else "too_large", f"refused: {exc.reason}")
            except httpx.TimeoutException:
                metrics.record_fetch("timeout")
                raise CardFetchError("timeout", "the card fetch timed out")
            except httpx.HTTPError as exc:
                metrics.record_fetch("http_error")
                raise CardFetchError("http_error", f"fetch failed ({type(exc).__name__})")
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise CardFetchError("http_error", "redirect without Location")
                target = urljoin(target, location)
                try:
                    check_url(target)
                except OutboundRefused as exc:
                    metrics.record_fetch("ssrf_refused")
                    metrics.record_ssrf_rejection("redirect")
                    raise CardFetchError("ssrf_refused", f"redirect refused: {exc.reason}")
                continue
            if response.status_code == 304 and etag:
                metrics.record_fetch("not_modified")
                return FetchedCard(card=pb.AgentCard(), summary={}, card_hash="", etag=etag, final_url=target, not_modified=True)
            if response.status_code != 200:
                metrics.record_fetch("http_error")
                raise CardFetchError("http_error", f"card endpoint answered HTTP {response.status_code}")
            ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if not (ctype == "application/json" or ctype.endswith("+json")):
                metrics.record_fetch("invalid_card")
                raise CardFetchError("invalid_card", "the card is not served as JSON")
            try:
                data = json.loads(response.content)
            except OutboundRefused:
                metrics.record_fetch("too_large")
                raise CardFetchError("too_large", "the card exceeds 256 KiB")
            except ValueError:
                metrics.record_fetch("invalid_card")
                raise CardFetchError("invalid_card", "the card is not valid JSON")
            if not isinstance(data, dict):
                metrics.record_fetch("invalid_card")
                raise CardFetchError("invalid_card", "the card is not a JSON object")
            try:
                card = ParseDict(data, pb.AgentCard(), ignore_unknown_fields=True)
            except Exception:  # noqa: BLE001 - any schema violation
                metrics.record_fetch("invalid_card")
                raise CardFetchError("invalid_card", "the card does not match the A2A 1.0 AgentCard schema")
            try:
                summary = validate_card(card)
            except CardFetchError:
                metrics.record_fetch("invalid_card")
                raise
            metrics.record_fetch("ok")
            canonical = MessageToDict(card)
            return FetchedCard(card=card, summary=summary, card_hash=card_hash(canonical), etag=response.headers.get("etag"), final_url=target)
        metrics.record_fetch("http_error")
        raise CardFetchError("http_error", "too many redirects")
    finally:
        if own:
            await http.aclose()
