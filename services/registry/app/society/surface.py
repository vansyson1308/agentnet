"""Public-surface contract checks: deterministic HTTP, never a model.

``public_surface_contract.json`` (next to this file) is the ONE machine-readable
description of the public product surface. Three consumers share this module:

* the Society's synthetic monitor (``surface_monitor.py``, staging society-worker),
* ``deploy/public_surface_validate.py`` (local / staging / production), and
* CI, which drives the dashboard through ``httpx.WSGITransport``.

Everything an :class:`Observation` holds is STRUCTURAL: statuses, the final
path after redirects, hop counts, latency, which of the contract's own
markers were missing, and a failure class chosen by this code. Page text is
never copied into an observation -- public web content is untrusted, and the
Society reads these observations. A link found on a page is reported only if
its path matches a strict character set; anything else is counted, not
quoted.

This file and the contract are trusted evaluation criteria (risk.py treats
them as meta paths): a candidate may not change a route and the expectation
that judges it in one change.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

CONTRACT_PATH = Path(__file__).with_name("public_surface_contract.json")

SEVERITIES = ("minor", "major", "critical")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# Failure classes (chosen by code; never derived from page text).
UNREACHABLE = "unreachable"
TIMEOUT = "timeout"
SERVER_ERROR = "server_error"
CLIENT_ERROR = "client_error"
UNEXPECTED_STATUS = "unexpected_status"
MASKED_BY_LANDING = "masked_by_landing_redirect"
WRONG_FINAL_PATH = "wrong_final_path"
MARKER_MISSING = "marker_missing"
REDIRECT_LOOP = "redirect_loop"
OFFSITE_REDIRECT = "offsite_redirect"
TOO_LARGE = "response_too_large"
PLACEHOLDER_LINK = "placeholder_link"
EMPTY_ASSET = "empty_asset"
FAILURE_CLASSES = (
    UNREACHABLE, TIMEOUT, SERVER_ERROR, CLIENT_ERROR, UNEXPECTED_STATUS, MASKED_BY_LANDING,
    WRONG_FINAL_PATH, MARKER_MISSING, REDIRECT_LOOP, OFFSITE_REDIRECT, TOO_LARGE, PLACEHOLDER_LINK,
    EMPTY_ASSET,
)
#: Classes that mean "the product is not answering", as opposed to a wrong page.
AVAILABILITY_FAILURES = frozenset({UNREACHABLE, TIMEOUT, SERVER_ERROR})

MAX_HOPS = 5
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_BYTES = 1_048_576
USER_AGENT = "AgentNet-SurfaceMonitor/1.0 (+https://agentnet.io.vn)"
#: A path found on a page is reported only when it looks like this.
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~/\-]{0,127}$")
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class ContractError(ValueError):
    """The contract file itself is malformed (a trusted-file defect)."""


@dataclass(frozen=True)
class SurfaceItem:
    name: str
    origin: str
    path: str
    initial_status: Tuple[int, ...]
    final_status: int
    final_path: str
    markers: Tuple[str, ...]
    severity: str
    monitor: bool
    auth: str = "public"
    intent: str = ""


@dataclass(frozen=True)
class CrawlRule:
    sources: Tuple[str, ...]
    limit: int
    severity: str
    monitor: bool


@dataclass(frozen=True)
class Contract:
    version: int
    origins: Dict[str, str]
    login_path: str
    landing_path: str
    items: Tuple[SurfaceItem, ...]
    navigation: CrawlRule
    assets: CrawlRule
    product_source: str = ""
    verification_tests: Tuple[str, ...] = ()

    def item(self, name: str) -> SurfaceItem:
        for it in self.items:
            if it.name == name:
                return it
        raise KeyError(name)

    def declared_final_path(self, origin: str, path: str) -> Optional[str]:
        for it in self.items:
            if it.origin == origin and it.path == path:
                return it.final_path
        return None


def _rule(raw: Mapping, names: Iterable[str], key: str) -> CrawlRule:
    sources = tuple(raw.get("from") or ())
    unknown = [s for s in sources if s not in names]
    if unknown:
        raise ContractError(f"{key}.from names unknown items: {unknown}")
    severity = raw.get("severity", "major")
    if severity not in SEVERITIES:
        raise ContractError(f"{key}.severity must be one of {SEVERITIES}")
    limit = int(raw.get("max_links") or raw.get("max_assets") or 0)
    if not 0 < limit <= 100:
        raise ContractError(f"{key} limit must be 1..100")
    return CrawlRule(sources=sources, limit=limit, severity=severity, monitor=bool(raw.get("monitor", True)))


def load_contract(path: Path = CONTRACT_PATH) -> Contract:
    """Load and strictly validate the contract. Unknown origins, severities,
    duplicate names or a non-absolute path are contract defects."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"cannot read contract: {exc}") from exc
    origins = raw.get("origins") or {}
    if set(origins) != {"ui", "api"}:
        raise ContractError("origins must name exactly 'ui' and 'api'")
    items: List[SurfaceItem] = []
    seen = set()
    for entry in raw.get("items") or []:
        name = entry.get("name", "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", name or "") or name in seen:
            raise ContractError(f"bad or duplicate item name: {name!r}")
        seen.add(name)
        if entry.get("origin") not in origins:
            raise ContractError(f"{name}: unknown origin")
        p = entry.get("path", "")
        if not p.startswith("/") or not SAFE_PATH.match(p):
            raise ContractError(f"{name}: path must be an absolute, plain path")
        if entry.get("severity") not in SEVERITIES:
            raise ContractError(f"{name}: severity must be one of {SEVERITIES}")
        markers = tuple(entry.get("markers") or ())
        if not markers:
            raise ContractError(f"{name}: at least one content marker is required")
        items.append(
            SurfaceItem(
                name=name,
                origin=entry["origin"],
                path=p,
                initial_status=tuple(int(s) for s in entry.get("initial_status") or (200,)),
                final_status=int(entry.get("final_status", 200)),
                final_path=entry.get("final_path") or p,
                markers=markers,
                severity=entry["severity"],
                monitor=bool(entry.get("monitor", True)),
                auth=entry.get("auth", "public"),
                intent=str(entry.get("intent", ""))[:200],
            )
        )
    if not items:
        raise ContractError("contract has no items")
    names = [i.name for i in items]
    return Contract(
        version=int(raw.get("version", 0)),
        origins=dict(origins),
        login_path=raw.get("login_path", "/login"),
        landing_path=raw.get("landing_path", "/landing"),
        items=tuple(items),
        navigation=_rule(raw.get("navigation") or {}, names, "navigation"),
        assets=_rule(raw.get("assets") or {}, names, "assets"),
        product_source=str(raw.get("product_source", ""))[:200],
        verification_tests=tuple(str(t)[:255] for t in raw.get("verification_tests") or ()),
    )


@dataclass
class Observation:
    """One structural fact about the public surface."""

    name: str
    kind: str  # item | link | placeholder | asset
    origin: str
    path: Optional[str]
    severity: str
    failure: Optional[str] = None
    initial_status: Optional[int] = None
    final_status: Optional[int] = None
    final_path: Optional[str] = None
    hops: int = 0
    latency_ms: int = 0
    expected_final_path: Optional[str] = None
    markers_missing: Tuple[str, ...] = ()
    detail: str = ""
    source: Optional[str] = None  # the contract item whose page linked here

    @property
    def ok(self) -> bool:
        return self.failure is None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["markers_missing"] = list(self.markers_missing)
        d["ok"] = self.ok
        return d


@dataclass
class SurfaceReport:
    checked_at: str
    origins: Dict[str, str]
    observations: List[Observation] = field(default_factory=list)
    unsafe_links: int = 0

    def failures(self, min_severity: str = "minor") -> List[Observation]:
        floor = _SEV_RANK[min_severity]
        return [o for o in self.observations if not o.ok and _SEV_RANK[o.severity] >= floor]

    def summary(self) -> dict:
        fails = self.failures()
        return {
            "checked": len(self.observations),
            "failing": len(fails),
            "failing_by_severity": {s: sum(1 for o in fails if o.severity == s) for s in SEVERITIES},
            "unsafe_links": self.unsafe_links,
        }

    def to_dict(self) -> dict:
        return {
            "checked_at": self.checked_at,
            "origins": dict(self.origins),
            "summary": self.summary(),
            "observations": [o.to_dict() for o in self.observations],
        }


@dataclass
class _Fetched:
    initial_status: Optional[int]
    final_status: Optional[int]
    final_url: str
    hops: int
    body: bytes
    latency_ms: int
    failure: Optional[str] = None
    detail: str = ""


def _origin_of(url: str) -> str:
    s = urlsplit(url)
    return f"{s.scheme}://{s.netloc}".lower()


def _fetch(client: httpx.Client, url: str, allowed_origins: Sequence[str], *, max_bytes: int) -> _Fetched:
    """GET ``url`` following at most MAX_HOPS same-product redirects, reading
    at most ``max_bytes``. Never raises for network trouble: it is recorded."""
    allowed = {o.lower().rstrip("/") for o in allowed_origins}
    seen: List[str] = []
    initial: Optional[int] = None
    started = time.monotonic()
    current = url
    for hop in range(MAX_HOPS + 1):
        if current in seen:
            return _Fetched(initial, None, current, hop, b"", _ms(started), REDIRECT_LOOP, "redirect loop")
        seen.append(current)
        try:
            with client.stream("GET", current) as resp:
                status = resp.status_code
                if initial is None:
                    initial = status
                if status in _REDIRECTS:
                    location = resp.headers.get("location", "")
                    nxt = urljoin(current, location)
                    if _origin_of(nxt) not in allowed:
                        return _Fetched(initial, status, current, hop + 1, b"", _ms(started), OFFSITE_REDIRECT, "redirect left the product origins")
                    current = nxt
                    continue
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        return _Fetched(initial, status, current, hop, b"", _ms(started), TOO_LARGE, f"body over {max_bytes} bytes")
                return _Fetched(initial, status, current, hop, bytes(body), _ms(started))
        except httpx.TimeoutException:
            return _Fetched(initial, None, current, hop, b"", _ms(started), TIMEOUT, "timed out")
        except httpx.HTTPError as exc:
            return _Fetched(initial, None, current, hop, b"", _ms(started), UNREACHABLE, type(exc).__name__)
    return _Fetched(initial, None, current, MAX_HOPS, b"", _ms(started), REDIRECT_LOOP, f"more than {MAX_HOPS} redirects")


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _path_of(url: str) -> str:
    return urlsplit(url).path or "/"


def _classify_status(status: Optional[int]) -> Optional[str]:
    if status is None:
        return None
    if status >= 500:
        return SERVER_ERROR
    if status >= 400:
        return CLIENT_ERROR
    return None


def check_item(client: httpx.Client, contract: Contract, item: SurfaceItem, origins: Mapping[str, str], *, max_bytes: int = DEFAULT_MAX_BYTES) -> Tuple[Observation, bytes]:
    """Check one contract item. Returns the observation and the body (the
    body is for link extraction by this module only; callers must not store
    or forward it)."""
    base = origins[item.origin].rstrip("/")
    f = _fetch(client, base + item.path, list(origins.values()), max_bytes=max_bytes)
    obs = Observation(
        name=item.name, kind="item", origin=item.origin, path=item.path, severity=item.severity,
        initial_status=f.initial_status, final_status=f.final_status, final_path=_path_of(f.final_url),
        hops=f.hops, latency_ms=f.latency_ms, expected_final_path=item.final_path,
    )
    if f.failure:
        obs.failure, obs.detail = f.failure, f.detail
        return obs, b""
    bad = _classify_status(f.final_status)
    if bad:
        obs.failure, obs.detail = bad, f"final status {f.final_status}"
        return obs, b""
    if f.initial_status not in item.initial_status:
        # A page that should answer itself but redirects to the landing page
        # is a missing page wearing a 200 -- the failure mode this contract exists for.
        if obs.final_path == contract.landing_path and item.final_path != contract.landing_path:
            obs.failure, obs.detail = MASKED_BY_LANDING, f"{item.path} redirected to {contract.landing_path}"
        else:
            obs.failure, obs.detail = UNEXPECTED_STATUS, f"initial status {f.initial_status}, expected {list(item.initial_status)}"
        return obs, f.body
    if f.final_status != item.final_status:
        obs.failure, obs.detail = UNEXPECTED_STATUS, f"final status {f.final_status}, expected {item.final_status}"
        return obs, f.body
    if obs.final_path != item.final_path:
        cls = MASKED_BY_LANDING if obs.final_path == contract.landing_path else WRONG_FINAL_PATH
        obs.failure, obs.detail = cls, f"ended at {obs.final_path if SAFE_PATH.match(obs.final_path or '') else '(unsafe path)'}, expected {item.final_path}"
        return obs, f.body
    text = f.body.decode("utf-8", errors="replace")
    missing = tuple(m for m in item.markers if m not in text)
    if missing:
        obs.failure, obs.markers_missing, obs.detail = MARKER_MISSING, missing, f"{len(missing)} expected marker(s) absent"
    return obs, f.body


class _LinkParser(HTMLParser):
    """Collects href/action/src values. Structural only; text is ignored."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: List[str] = []
        self.forms: List[str] = []
        self.assets: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and "href" in a:
            self.links.append(a["href"].strip())
        elif tag == "form":
            self.forms.append(a.get("action", "").strip())
        elif tag == "link" and "stylesheet" in a.get("rel", "").lower() and a.get("href"):
            self.assets.append(a["href"].strip())
        elif tag in ("script", "img") and a.get("src"):
            self.assets.append(a["src"].strip())


def _is_placeholder(value: str, *, form: bool) -> bool:
    v = value.strip().lower()
    if form:
        return v == "#" or v.startswith("javascript:")
    return v in ("", "#") or v.startswith("javascript:")


def extract(body: bytes) -> _LinkParser:
    p = _LinkParser()
    try:
        p.feed(body.decode("utf-8", errors="replace"))
        p.close()
    except Exception:  # noqa: BLE001 -- malformed HTML yields what was parsed so far
        pass
    return p


def _same_origin_path(base_url: str, value: str) -> Optional[str]:
    """Resolve ``value`` against ``base_url``; the path if same-origin, else None."""
    if value.startswith(("mailto:", "tel:", "data:")):
        return None
    target = urljoin(base_url, value)
    if _origin_of(target) != _origin_of(base_url):
        return None
    return urlsplit(target).path or "/"


def run_contract(
    origins: Optional[Mapping[str, str]] = None,
    *,
    contract: Optional[Contract] = None,
    client: Optional[httpx.Client] = None,
    only_monitored: bool = False,
    include_api: bool = True,
    crawl: bool = True,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> SurfaceReport:
    """Check every contract item, then crawl the core pages' same-origin links,
    form actions and local assets (bounded by the contract)."""
    contract = contract or load_contract()
    origins = {k: v.rstrip("/") for k, v in (origins or contract.origins).items()}
    own_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT})
    report = SurfaceReport(checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), origins=dict(origins))
    bodies: Dict[str, bytes] = {}
    try:
        for item in contract.items:
            if only_monitored and not item.monitor:
                continue
            if not include_api and item.origin == "api":
                continue
            obs, body = check_item(client, contract, item, origins, max_bytes=max_bytes)
            report.observations.append(obs)
            if obs.ok or obs.failure == MARKER_MISSING:
                bodies[item.name] = body
        if crawl:
            _crawl(client, contract, origins, bodies, report, max_bytes=max_bytes, only_monitored=only_monitored)
    finally:
        if own_client:
            client.close()
    return report


def _crawl(client: httpx.Client, contract: Contract, origins: Mapping[str, str], bodies: Mapping[str, bytes], report: SurfaceReport, *, max_bytes: int, only_monitored: bool) -> None:
    checked_paths = {it.path for it in contract.items if it.origin == "ui"}
    nav, assets = contract.navigation, contract.assets
    link_targets: Dict[str, str] = {}
    form_targets: Dict[str, str] = {}
    asset_targets: Dict[str, str] = {}
    for src in nav.sources + tuple(s for s in assets.sources if s not in nav.sources):
        body = bodies.get(src)
        if body is None:
            continue
        item = contract.item(src)
        base = origins[item.origin] + item.path
        parsed = extract(body)
        if src in nav.sources and (nav.monitor or not only_monitored):
            dead = sum(1 for v in parsed.links if _is_placeholder(v, form=False)) + sum(1 for v in parsed.forms if _is_placeholder(v, form=True))
            if dead:
                report.observations.append(Observation(
                    name=f"{src}:placeholders", kind="placeholder", origin=item.origin, path=item.path,
                    severity=nav.severity, failure=PLACEHOLDER_LINK, source=src,
                    detail=f"{dead} dead link(s) ('#' or 'javascript:') on {item.path}",
                ))
            for kind_map, values in ((link_targets, parsed.links), (form_targets, parsed.forms)):
                for v in values:
                    if _is_placeholder(v, form=kind_map is form_targets) or v.startswith("#"):
                        continue
                    if kind_map is form_targets and v == "":
                        continue  # an empty action posts back to the page itself
                    p = _same_origin_path(base, v)
                    if p is None or p.startswith("/static/"):
                        continue
                    if not SAFE_PATH.match(p):
                        report.unsafe_links += 1
                        continue
                    kind_map.setdefault(p, src)
        if src in assets.sources and (assets.monitor or not only_monitored):
            for v in parsed.assets:
                p = _same_origin_path(base, v)
                if p is None:
                    continue
                if not SAFE_PATH.match(p):
                    report.unsafe_links += 1
                    continue
                asset_targets.setdefault(p, src)
    ui = origins["ui"]
    for path, src in list(link_targets.items())[: nav.limit]:
        if path in checked_paths:
            continue
        report.observations.append(_check_link(client, contract, origins, path, src, nav.severity, max_bytes=max_bytes, form=False))
    for path, src in list(form_targets.items())[: nav.limit]:
        report.observations.append(_check_link(client, contract, origins, path, src, nav.severity, max_bytes=max_bytes, form=True))
    for path, src in list(asset_targets.items())[: assets.limit]:
        f = _fetch(client, ui + path, list(origins.values()), max_bytes=max_bytes)
        obs = Observation(name=f"asset:{path}", kind="asset", origin="ui", path=path, severity=assets.severity,
                          initial_status=f.initial_status, final_status=f.final_status, final_path=_path_of(f.final_url),
                          hops=f.hops, latency_ms=f.latency_ms, source=src)
        if f.failure:
            obs.failure, obs.detail = f.failure, f.detail
        elif _classify_status(f.final_status):
            obs.failure, obs.detail = _classify_status(f.final_status), f"status {f.final_status}"
        elif f.final_status == 204 or not f.body:
            obs.failure, obs.detail = EMPTY_ASSET, "empty response for a referenced asset"
        report.observations.append(obs)


def _check_link(client: httpx.Client, contract: Contract, origins: Mapping[str, str], path: str, src: str, severity: str, *, max_bytes: int, form: bool) -> Observation:
    f = _fetch(client, origins["ui"] + path, list(origins.values()), max_bytes=max_bytes)
    final_path = _path_of(f.final_url)
    obs = Observation(name=f"{'form' if form else 'link'}:{path}", kind="link", origin="ui", path=path, severity=severity,
                      initial_status=f.initial_status, final_status=f.final_status, final_path=final_path,
                      hops=f.hops, latency_ms=f.latency_ms, source=src)
    declared = contract.declared_final_path("ui", path)
    obs.expected_final_path = declared or path
    if f.failure:
        obs.failure, obs.detail = f.failure, f.detail
        return obs
    if form and f.final_status == 405:
        return obs  # a POST-only handler exists
    bad = _classify_status(f.final_status)
    if bad:
        obs.failure, obs.detail = bad, f"status {f.final_status}"
        return obs
    if final_path in (path, declared, contract.login_path):
        return obs
    if final_path == contract.landing_path:
        obs.failure, obs.detail = MASKED_BY_LANDING, f"{path} redirected to {contract.landing_path}"
    else:
        obs.failure, obs.detail = WRONG_FINAL_PATH, f"{path} ended elsewhere"
    return obs


def worst_severity(observations: Iterable[Observation]) -> Optional[str]:
    worst = None
    for o in observations:
        if o.ok:
            continue
        if worst is None or _SEV_RANK[o.severity] > _SEV_RANK[worst]:
            worst = o.severity
    return worst


__all__ = [
    "CONTRACT_PATH", "SEVERITIES", "FAILURE_CLASSES", "AVAILABILITY_FAILURES", "ContractError",
    "SurfaceItem", "CrawlRule", "Contract", "Observation", "SurfaceReport", "load_contract",
    "check_item", "extract", "run_contract", "worst_severity", "USER_AGENT",
]
